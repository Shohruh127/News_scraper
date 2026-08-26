"""Tests for LLM integration, classification, rule pre-filters, and tasks."""

import json
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from apps.digest import llm, tasks
from apps.digest.models import Analysis, Article, Maturity, Source, Topic


@pytest.fixture
def source(db):
    return Source.objects.create(
        name="test_source",
        connector=Source.Connector.RSS,
        url="https://example.com/rss",
    )


@pytest.fixture
def sample_article(db, source):
    return Article.objects.create(
        source=source,
        canonical_url="https://example.com/article-1",
        content_hash="hash1",
        title="Test Frontier Model Release",
        extracted_text="A new frontier model has been released with superior capabilities. " * 10,
    )


GW = "http://gw.test/v1"


@pytest.fixture(autouse=True)
def _gateway(settings):
    """Every stage runs on the gateway. The direct Ollama path was removed 2026-08-25."""
    settings.GATEWAY_BASE_URL = GW
    settings.GATEWAY_TOKEN = "sk-test"
    settings.GATEWAY_FAST_MODEL = "fast"
    settings.GATEWAY_SMART_MODEL = "smart"
    settings.CLASSIFIER_PROVIDER = "gateway"
    settings.LLM_PROVIDER = "gateway"
    settings.EDITORIAL_EN_PROVIDER = "gateway"
    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    return settings


def _gw_reply(payload):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


@respx.mock
def test_gateway_chat_success():
    mock_payload = {
        "primary_topic": "frontier_models",
        "maturity": "live_product",
        "novelty": 9,
        "evidence": 8,
        "production_readiness": 9,
        "reason": "New model release with public API access.",
    }
    respx.post(f"{GW}/chat/completions").mock(return_value=_gw_reply(mock_payload))

    result = llm.gateway_chat(
        model="fast",
        prompt="Classify this",
        schema=llm.CLASSIFICATION_SCHEMA,
        max_tokens=400,
    )

    assert result.payload["primary_topic"] == "frontier_models"
    assert result.payload["maturity"] == "live_product"
    assert result.payload["novelty"] == 9
    assert result.latency_ms >= 0


@respx.mock
def test_gateway_chat_retries_on_503():
    mock_payload = {
        "primary_topic": "frontier_models",
        "maturity": "live_product",
        "novelty": 8,
        "evidence": 8,
        "production_readiness": 8,
        "reason": "Retried successfully.",
    }
    route = respx.post(f"{GW}/chat/completions")
    route.side_effect = [
        httpx.Response(503, text="server busy"),
        _gw_reply(mock_payload),
    ]

    result = llm.gateway_chat(
        model="fast",
        prompt="Classify this",
        schema=llm.CLASSIFICATION_SCHEMA,
        max_tokens=400,
    )

    assert result.payload["primary_topic"] == "frontier_models"
    assert route.call_count == 2


def test_rule_prefilter_blocklist(db, source):
    art = Article(
        source=source,
        canonical_url="https://twitter.com/user/status/123",
        content_hash="h1",
        title="Tweet title",
        extracted_text="Some valid length text " * 50,
    )
    passed, reason = llm.check_rule_prefilter(art)
    assert not passed
    assert "Blocklisted" in reason


def test_rule_prefilter_short_text(db, source):
    art = Article(
        source=source,
        canonical_url="https://example.com/short",
        content_hash="h2",
        title="Short article",
        extracted_text="Too short",
    )
    passed, reason = llm.check_rule_prefilter(art)
    assert not passed
    assert "Text too short" in reason


@respx.mock
def test_classify_text_recovery_on_validation_error():
    # First response is invalid schema, second is valid
    invalid_payload = {"primary_topic": "unknown_topic", "maturity": "live_product"}
    valid_payload = {
        "primary_topic": "frontier_models",
        "maturity": "live_product",
        "novelty": 8,
        "evidence": 8,
        "production_readiness": 8,
        "reason": "Recovered valid schema.",
    }
    route = respx.post(f"{GW}/chat/completions")
    route.side_effect = [
        httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(invalid_payload)}}]}
        ),
        httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(valid_payload)}}]}
        ),
    ]

    classification, result = llm.classify_text(
        title="Test Article",
        source_name="test_source",
        text="Valid article text " * 30,
        tier=llm.TIER_DEEP,
    )

    assert classification.primary_topic == Topic.FRONTIER_MODELS
    assert classification.maturity == Maturity.LIVE_PRODUCT
    assert route.call_count == 2
    # Both attempts are billed to the article: a retry counted as one call is invisible.
    assert result.latency_ms >= 0
    assert result.payload["reason"] == "Recovered valid schema."


@respx.mock
def test_triage_article_logic_keep(db, sample_article):
    mock_payload = {"relevant": True, "reason": "Named model release"}
    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(mock_payload)}}]}
        )
    )

    passed = llm.triage_article_logic(sample_article)
    sample_article.refresh_from_db()

    assert passed is True
    assert sample_article.status == Article.Status.TRIAGED
    assert Analysis.objects.filter(article=sample_article).count() == 1


@respx.mock
def test_triage_article_logic_irrelevant_skipped(db, sample_article):
    mock_payload = {
        "primary_topic": "irrelevant",
        "maturity": "announcement_only",
        "novelty": 1,
        "evidence": 1,
        "production_readiness": 1,
        "reason": "Executive hiring announcement.",
    }
    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(mock_payload)}}]}
        )
    )

    passed = llm.triage_article_logic(sample_article)
    sample_article.refresh_from_db()

    assert passed is False
    assert sample_article.status == Article.Status.SKIPPED


@respx.mock
def test_triage_passes_paper_only_to_classify(db, sample_article):
    """Triage never rejects on maturity: since 2026-08-25 it does not decide maturity at all.

    It sees the headline and answers one question. Maturity exclusion happens in ranking,
    after the deep-tier classification pass.
    """
    mock_payload = {"relevant": True, "reason": "Research method, worth classifying"}
    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(mock_payload)}}]}
        )
    )

    passed = llm.triage_article_logic(sample_article)
    sample_article.refresh_from_db()

    assert passed is True
    assert sample_article.status == Article.Status.TRIAGED


@respx.mock
def test_classify_article_logic(db, sample_article):
    mock_payload = {
        "primary_topic": "ai_agents",
        "maturity": "live_product",
        "novelty": 8,
        "evidence": 9,
        "production_readiness": 8,
        "reason": "Agent framework release.",
    }
    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(mock_payload)}}]}
        )
    )

    passed = llm.classify_article_logic(sample_article)
    sample_article.refresh_from_db()

    assert passed is True
    assert sample_article.status == Article.Status.CLASSIFIED
    analysis = Analysis.objects.get(article=sample_article)
    assert analysis.topic == "ai_agents"
    assert analysis.maturity == "live_product"


@respx.mock
def test_triage_infra_failure_leaves_status_fetched(db, sample_article):
    """When Ollama returns 503/timeout, article must remain in FETCHED status for next retry."""
    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": "server busy"})
    )

    passed = llm.triage_article_logic(sample_article)
    sample_article.refresh_from_db()

    assert passed is False
    assert sample_article.status == Article.Status.FETCHED
    assert Analysis.objects.filter(article=sample_article).count() == 0


@respx.mock
def test_classify_infra_failure_leaves_status_triaged(db, sample_article):
    """When Ollama returns 503 during classify, article must remain in TRIAGED status."""
    sample_article.status = Article.Status.TRIAGED
    sample_article.save(update_fields=["status"])

    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": "server busy"})
    )

    passed = llm.classify_article_logic(sample_article)
    sample_article.refresh_from_db()

    assert passed is False
    assert sample_article.status == Article.Status.TRIAGED


@respx.mock
def test_triage_validation_failure_marks_skipped(db, sample_article):
    """When model output permanently fails schema validation after retry, mark SKIPPED."""
    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "not json content"}}]}
        )
    )

    passed = llm.triage_article_logic(sample_article)
    sample_article.refresh_from_db()

    assert passed is False
    assert sample_article.status == Article.Status.SKIPPED


@respx.mock
def test_triage_and_classify_batch(db, source, monkeypatch):
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    monkeypatch.setattr("redis.Redis.from_url", lambda url: mock_redis)

    art1 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art1",
        content_hash="h_art1",
        title="Article 1 Keep",
        extracted_text="Article 1 long text content " * 30,
        status=Article.Status.FETCHED,
    )
    art2 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art2",
        content_hash="h_art2",
        title="Article 2 Drop",
        extracted_text="Article 2 long text content " * 30,
        status=Article.Status.FETCHED,
    )

    # Triage answers a binary gate; classification still returns the full schema.
    keep_payload = {"relevant": True, "reason": "Named model release"}
    drop_payload = {"relevant": False, "reason": "Executive appointment"}
    classified_payload = {
        "primary_topic": "frontier_models",
        "maturity": "live_product",
        "novelty": 9,
        "evidence": 9,
        "production_readiness": 9,
        "reason": "Good",
    }

    route = respx.post(f"{GW}/chat/completions")
    route.side_effect = [
        # Two triage calls (art1 keeps, art2 drops), then one classification for art1.
        httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(keep_payload)}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(drop_payload)}}]}),
        httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(classified_payload)}}]}
        ),
    ]

    result = tasks.triage_and_classify()

    art1.refresh_from_db()
    art2.refresh_from_db()

    assert result["triaged"] == 2
    assert result["triage_survivors"] == 1
    assert result["classified"] == 1
    assert result["classify_survivors"] == 1

    assert art1.status == Article.Status.CLASSIFIED
    assert art2.status == Article.Status.SKIPPED


@respx.mock
def test_eval_classifier_command(tmp_path):
    from io import StringIO

    from django.core.management import call_command

    gold_file = tmp_path / "test_gold.jsonl"
    rows = [
        {
            "id": "1",
            "title": "Claude Opus 5",
            "source": "anthropic",
            "text_excerpt": "Opus 5 release",
            "human_label": "keep",
            "human_topic": "frontier_models",
            "human_maturity": "live_product",
        },
        {
            "id": "2",
            "title": "Donation announcement",
            "source": "anthropic",
            "text_excerpt": "Donation text",
            "human_label": "drop",
            "human_topic": "irrelevant",
            "human_maturity": "announcement_only",
        },
    ]
    with open(gold_file, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    respx.post(f"{GW}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "primary_topic": "frontier_models",
                                        "maturity": "live_product",
                                        "novelty": 9,
                                        "evidence": 9,
                                        "production_readiness": 9,
                                        "reason": "Top frontier model",
                                    }
                                )
                            }
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "primary_topic": "irrelevant",
                                        "maturity": "announcement_only",
                                        "novelty": 1,
                                        "evidence": 1,
                                        "production_readiness": 1,
                                        "reason": "Donation",
                                    }
                                )
                            }
                        }
                    ]
                },
            ),
        ]
    )

    out = StringIO()
    call_command("eval_classifier", gold_set=str(gold_file), stdout=out)
    output = out.getvalue()

    assert "Precision:         1.0000" in output
    assert "ACCEPTANCE CRITERIA MET" in output
    assert "MANDATORY CAVEAT" in output


@respx.mock
def test_eval_classifier_aborts_on_errors(tmp_path):
    """When LLM calls fail, eval_classifier must NOT report metrics — exit 1."""
    from io import StringIO

    from django.core.management import call_command

    gold_file = tmp_path / "test_gold_err.jsonl"
    rows = [
        {
            "id": "1",
            "title": "Test Article",
            "source": "test",
            "text_excerpt": "Some text",
            "human_label": "keep",
            "human_topic": "frontier_models",
            "human_maturity": "live_product",
        },
    ]
    with open(gold_file, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    # Simulate 503 from Ollama (all retries exhausted)
    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": "server busy"})
    )

    out = StringIO()
    err = StringIO()
    with pytest.raises(SystemExit) as exc_info:
        call_command("eval_classifier", gold_set=str(gold_file), stdout=out, stderr=err)

    assert exc_info.value.code == 1
    output = out.getvalue()
    assert "EVALUATION ABORTED" in output
    assert "Precision" not in output


def test_chat_result_carries_reported_tokens():
    """The provider reports usage; we store it rather than estimating from characters."""
    import respx as _respx

    with _respx.mock:
        _respx.post(f"{GW}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 197, "completion_tokens": 20},
                },
            )
        )
        result = llm.gateway_chat(
            model="fast", prompt="hi", schema={"type": "object"}, max_tokens=100
        )

    assert result.input_tokens == 197
    assert result.output_tokens == 20


def test_a_provider_that_reports_no_usage_records_none_not_zero():
    """None means unmeasured. Zero would make the call look free and understate every total."""
    import respx as _respx

    with _respx.mock:
        _respx.post(f"{GW}/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}
            )
        )
        result = llm.gateway_chat(
            model="fast", prompt="hi", schema={"type": "object"}, max_tokens=100
        )

    assert result.input_tokens is None
    assert result.output_tokens is None


def test_combine_bills_both_attempts_to_the_article():
    """A stage that retried made two calls. Recording only the last one hides the retry."""
    first = llm.ChatResult(
        {"a": 1}, latency_ms=100, model_tag="fast", input_tokens=10, output_tokens=2
    )
    retry = llm.ChatResult(
        {"a": 2}, latency_ms=250, model_tag="fast", input_tokens=30, output_tokens=5
    )

    combined = llm._combine(first, retry)

    assert combined.payload == {"a": 2}, "the retry's answer wins"
    assert combined.latency_ms == 350
    assert combined.input_tokens == 40
    assert combined.output_tokens == 7


def test_combine_survives_a_first_call_that_never_returned():
    """The first attempt can raise before reporting anything; there is then nothing to add."""
    retry = llm.ChatResult(
        {"a": 2}, latency_ms=250, model_tag="fast", input_tokens=30, output_tokens=5
    )
    assert llm._combine(None, retry) == retry


@respx.mock
def test_triage_stores_what_the_call_cost(db, sample_article):
    respx.post(f"{GW}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"relevant": True, "reason": "release"})}}
                ],
                "usage": {"prompt_tokens": 197, "completion_tokens": 20},
            },
        )
    )

    llm.triage_article_logic(sample_article)

    analysis = Analysis.objects.get(article=sample_article, stage=Analysis.Stage.TRIAGE)
    assert analysis.input_tokens == 197
    assert analysis.output_tokens == 20
