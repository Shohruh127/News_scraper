"""The reader is not an engineer (2026-09-09): classification scores who cares, the gate
drops what only the trade cares about, and the ranking reads the score."""

import pytest

from apps.digest import llm, ranking
from apps.digest.models import Analysis, Article, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def article(db):
    source = Source.objects.create(
        name="hn", connector=Source.Connector.RSS, url="https://hn.test/rss", priority=60
    )
    return Article.objects.create(
        source=source,
        canonical_url="https://hn.test/ollama-0-34",
        content_hash="aud_h1",
        title="ollama/ollama v0.34.0",
        extracted_text="Changelog: speculative decoding on AMD GPUs, 12% faster prefill. " * 20,
        status=Article.Status.TRIAGED,
    )


def _classified(audience, topic="production_engineering"):
    payload = {
        "primary_topic": topic,
        "maturity": "live_product",
        "novelty": 6,
        "evidence": 9,
        "production_readiness": 9,
        "audience": audience,
        "reason": "changelog",
    }
    return llm.Classification.model_validate(payload), llm.ChatResult(payload, 100, "smart", 10, 5)


def test_the_prompts_address_the_channel_s_reader_and_ask_who_cares():
    assert "ordinary readers" in llm.CLASSIFICATION_PROMPT_TEMPLATE
    assert "engineers and technical decision-makers" not in llm.CLASSIFICATION_PROMPT_TEMPLATE
    assert "## audience" in llm.CLASSIFICATION_PROMPT_TEMPLATE
    assert "reaches millions of phones is 8" in llm.CLASSIFICATION_PROMPT_TEMPLATE
    assert "working engineers" not in llm.TRIAGE_PROMPT_TEMPLATE
    assert "keeps recall" in llm.TRIAGE_PROMPT_TEMPLATE, "triage stays the recall-first gate"
    assert "audience" in llm.CLASSIFICATION_SCHEMA["required"]


def test_rows_written_before_the_field_keep_the_middle_score():
    old = llm.Classification.model_validate(
        {
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 7,
            "evidence": 7,
            "production_readiness": 7,
            "reason": "old row",
        }
    )
    assert old.audience == 5


def test_the_gate_drops_what_only_the_trade_cares_about(article, monkeypatch, settings):
    settings.AUDIENCE_MIN_SCORE = 3
    monkeypatch.setattr(llm, "classify_text", lambda **kw: _classified(2))

    assert llm.classify_article_logic(article) is False
    article.refresh_from_db()
    assert article.status == Article.Status.SKIPPED
    row = Analysis.objects.get(article=article, stage=Analysis.Stage.CLASSIFICATION)
    assert row.payload["audience"] == 2, "the verdict is recorded even when the article is dropped"


def test_the_gate_keeps_an_article_a_curious_adult_cares_about(article, monkeypatch, settings):
    settings.AUDIENCE_MIN_SCORE = 3
    monkeypatch.setattr(llm, "classify_text", lambda **kw: _classified(6))

    assert llm.classify_article_logic(article) is True
    article.refresh_from_db()
    assert article.status == Article.Status.CLASSIFIED


def test_the_ranking_reads_the_audience_score(article):
    """Same novelty, evidence and readiness; the post readers care about ranks higher."""

    def row(audience):
        return Analysis(
            article=article,
            stage=Analysis.Stage.CLASSIFICATION,
            model_tag="smart",
            payload={
                "primary_topic": "production_engineering",
                "maturity": "live_product",
                "novelty": 6,
                "evidence": 9,
                "production_readiness": 9,
                "audience": audience,
            },
            latency_ms=1,
        )

    low, high = ranking.calculate_score(article, row(2)), ranking.calculate_score(article, row(9))
    assert high - low == pytest.approx(0.7 * 0.30, abs=1e-6), "0.30 of the rank is who cares"
    middle = ranking.calculate_score(article, row(5))
    assert ranking.calculate_score(article, row(5)) == middle
