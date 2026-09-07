"""The single-stage Uzbek editorial (2026-08-26 design).

One call reads the article and writes the post. This file covers the prompt, the class
blocks, the call, and the pipeline function that runs it — `analyse_for_digest_logic` is
this path now, so these tests do touch the pipeline.
"""

import json

import httpx
import pytest
import respx

from apps.digest.models import Analysis, Article, Source

pytestmark = pytest.mark.django_db


def test_every_topic_maps_to_an_uzbek_block():
    """A topic with no block silently falls back, so the map must be complete by test."""
    from apps.digest.llm import TOPIC_SHAPES, UZ_BLOCKS
    from apps.digest.models import Topic

    expected = {t.value for t in Topic} - {Topic.IRRELEVANT.value}
    assert set(TOPIC_SHAPES) == expected
    for topic, key in TOPIC_SHAPES.items():
        assert key in UZ_BLOCKS, f"{topic} maps to unknown block {key}"


def test_the_uzbek_blocks_actually_differ():
    """Seven identical blocks would pass every other test and change nothing."""
    from apps.digest.llm import UZ_BLOCKS

    assert len(UZ_BLOCKS) == 7, "six groups plus the general fallback"
    assert len({b.strip() for b in UZ_BLOCKS.values()}) == 7, "blocks must be distinct"


def test_no_uzbek_block_redefines_the_headline():
    """headline_uz is a label and does not change with the story type.

    The English agent block leaked into the headline on 2026-08-26 by arguing a thesis.
    Keeping headline_uz out of every block is how that stays impossible.
    """
    from apps.digest.llm import UZ_BLOCKS

    for key, block in UZ_BLOCKS.items():
        assert "headline_uz" not in block, f"{key} must not redefine the headline"


def test_the_model_accepts_a_full_uzbek_payload():
    from apps.digest.llm import EditorialUz

    parsed = EditorialUz.model_validate(
        {
            "headline_uz": "Qwen ochiq model chiqardi",
            "lead_uz": "Qwen jamoasi yangi modelni ochiq taqdim etdi.",
            "body_1_uz": "Model 123B parametrga ega.",
            "kicker_uz": "Shartnomasiz kuchli model.",
            "technical": {"repo_url": "https://example.com/r", "local_deployable": ""},
            "evidence_level": "vendor_claim_only",
        }
    )
    assert parsed.headline_uz.startswith("Qwen")
    assert parsed.technical.repo_url == "https://example.com/r"
    # The blank-boolean coercion added 2026-08-26 must still apply on this model.
    assert parsed.technical.local_deployable is False


def test_each_uzbek_block_reaches_the_formatted_prompt():
    """The archetype system failed for want of exactly this test: code present, prompt
    never asking for what the code consumed, nothing comparing the two."""
    from apps.digest.llm import EDITORIAL_UZ_PROMPT, UZ_BLOCKS

    for key, block in UZ_BLOCKS.items():
        filled = EDITORIAL_UZ_PROMPT.format(block=block, title="T", source="S", text="X")
        first_line = block.strip().splitlines()[0]
        assert first_line in filled, f"{key} block did not reach the prompt"


def _draft_result():
    from apps.digest.llm import ChatResult

    return ChatResult(
        payload={
            "headline_uz": "Filtr chetlab o'tildi",
            "lead_uz": "Model 123B parametr bilan filtrni chetlab o'tdi.",
            "body_1_uz": "Hujum 128k kontekst oynasida sinalgan.",
            "kicker_uz": "",
            "evidence_level": "vendor_claim_only",
            "technical": {"what_was_built": "A jailbreak of the content filter."},
        },
        latency_ms=100,
        model_tag="smart",
        input_tokens=100,
        output_tokens=50,
    )


def test_simplify_keeps_the_draft_when_the_rewrite_breaks_a_gate(risk_article, monkeypatch):
    """The polish step must never cost a post: a rewrite that invents a number is thrown
    away and the caller keeps the draft."""
    from apps.digest import llm as llm_mod

    draft = _draft_result()

    def fake_call(**kwargs):
        bad = dict(draft.payload)
        bad["lead_uz"] = "Model 99% hollarda filtrni chetlab o'tdi."
        return llm_mod.ChatResult(bad, 5, "smart", 10, 5)

    monkeypatch.setattr(llm_mod, "_editorial_call", fake_call)
    assert llm_mod._simplify_editorial_uz(risk_article, draft, None) is None


def test_simplify_overwrites_text_and_preserves_technical_and_cost(risk_article, monkeypatch):
    """Reader-facing fields come from the rewrite; `technical` is copied from the draft
    rather than trusted to survive a round trip; the Analysis row reports both calls."""
    from apps.digest import llm as llm_mod

    draft = _draft_result()

    def fake_call(**kwargs):
        assert risk_article.extracted_text[:8000] in kwargs["prompt"]
        return llm_mod.ChatResult(
            {
                "headline_uz": "Filtr aylanib o'tildi",
                "lead_uz": "Dastur 123B parametr bilan filtrdan o'tib ketdi.",
                "body_1_uz": "Sinov 128k kontekstda o'tkazildi.",
                "kicker_uz": "",
                "evidence_level": "vendor_claim_only",
                "technical": {"what_was_built": "MANGLED BY THE MODEL"},
            },
            latency_ms=200,
            model_tag="smart-2",
            input_tokens=30,
            output_tokens=7,
        )

    monkeypatch.setattr(llm_mod, "_editorial_call", fake_call)
    out = llm_mod._simplify_editorial_uz(risk_article, draft, None)

    assert out is not None
    assert out.payload["lead_uz"] == "Dastur 123B parametr bilan filtrdan o'tib ketdi."
    assert out.payload["technical"] == {"what_was_built": "A jailbreak of the content filter."}
    assert out.input_tokens == 130 and out.output_tokens == 57
    assert out.latency_ms == 300


def test_the_pipeline_prefers_the_simplified_post(risk_article, monkeypatch):
    """analyse_for_digest_logic stores the rewrite when it exists, the draft when not."""
    from apps.digest import llm as llm_mod

    draft = _draft_result()
    simp = draft._replace(payload={**draft.payload, "lead_uz": "Oddiy gap."})

    monkeypatch.setattr(llm_mod, "editorial_uz_for_article", lambda art, client=None: draft)
    monkeypatch.setattr(llm_mod, "_simplify_editorial_uz", lambda art, first, client: simp)
    rows = llm_mod.analyse_for_digest_logic([risk_article.id])
    assert rows[0].payload["lead_uz"] == "Oddiy gap."


UZ_PAYLOAD = {
    "headline_uz": "Qwen ochiq model chiqardi",
    "lead_uz": "Qwen jamoasi 123B parametrli modelni ochiq taqdim etdi.",
    "body_1_uz": "Model 128k kontekstga ega.",
    "kicker_uz": "Shartnomasiz kuchli model.",
    "evidence_level": "vendor_claim_only",
    "technical": {"repo_url": "https://example.com/repo", "local_deployable": True},
}


@pytest.fixture
def risk_article(db):
    source = Source.objects.create(
        name="uz_src", connector=Source.Connector.RSS, url="https://e.test/rss", priority=80
    )
    article = Article.objects.create(
        source=source,
        canonical_url="https://e.test/a",
        content_hash="uz_h1",
        title="A jailbreak bypasses the content filter",
        extracted_text="Details of the jailbreak with 123B parameters and 128k context. " * 40,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="smart",
        payload={
            "primary_topic": "safety_security",
            "maturity": "live_product",
            "novelty": 7,
            "evidence": 7,
            "production_readiness": 7,
            "reason": "security",
        },
        latency_ms=100,
    )
    return article


@respx.mock
def test_one_call_produces_the_uzbek_post(risk_article, settings):
    """One request, not two. The two-stage flow costs 3600 + 2700 input tokens."""
    from apps.digest import llm

    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"
    settings.GATEWAY_SMART_MODEL = "smart"

    route = respx.post("http://gw.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(UZ_PAYLOAD)}}]}
        )
    )

    result = llm.editorial_uz_for_article(risk_article)

    assert route.call_count == 1, "one call replaces the editorial+translation pair"
    assert result.payload["headline_uz"].startswith("Qwen")
    assert result.payload["technical"]["repo_url"] == "https://example.com/repo"


@respx.mock
def test_the_call_uses_the_block_for_the_classified_topic(risk_article, settings):
    """A safety item must be written to the risk block, not the release one."""
    from apps.digest import llm

    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"

    sent = {}

    def capture(request):
        sent.update(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(UZ_PAYLOAD)}}]}
        )

    respx.post("http://gw.test/v1/chat/completions").mock(side_effect=capture)
    llm.editorial_uz_for_article(risk_article)

    prompt = sent["messages"][0]["content"]
    assert llm.UZ_BLOCKS["risk"].strip().splitlines()[0] in prompt
    assert llm.UZ_BLOCKS["release"].strip().splitlines()[0] not in prompt


@respx.mock
def test_the_call_runs_on_the_deep_tier(risk_article, settings):
    """It comprehends the article and writes the post; translation's fast tier is not enough."""
    from apps.digest import llm

    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"
    settings.GATEWAY_FAST_MODEL = "fast"
    settings.GATEWAY_SMART_MODEL = "smart"

    route = respx.post("http://gw.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(UZ_PAYLOAD)}}]}
        )
    )

    llm.editorial_uz_for_article(risk_article)

    assert json.loads(route.calls[0].request.content)["model"] == "smart"


def test_posting_is_refused_without_an_eval_channel(settings):
    """The guard exists because a stray compose_and_publish reached worker-publish on
    2026-08-26 through a path nobody had modelled. The server will not set this key."""
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    settings.TELEGRAM_EVAL_CHANNEL_ID = ""

    with pytest.raises(CommandError, match="TELEGRAM_EVAL_CHANNEL_ID"):
        call_command("eval_editorial_uz", "--post", stdout=StringIO())


def test_eval_command_limit_selects_one_article(risk_article):
    from apps.digest.management.commands.eval_editorial_uz import Command

    picked = Command()._one_article_per_class(days=1, limit=1)

    assert len(picked) == 1
    assert next(iter(picked.values())).pk == risk_article.pk


def test_eval_command_rejects_negative_limit():
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="--limit must be"):
        call_command("eval_editorial_uz", "--limit", "-1", stdout=StringIO())


def test_the_prompt_never_teaches_a_form_the_glossary_gate_forbids():
    """A prompt that plants a calque and a gate that rejects it cannot both be right.

    Measured 2026-08-26 on the live comparison: the RELIZ post was flagged twice for
    'weights' rendered as 'vaznlar' and 'vaznlari'. Neither string is in the prompt — the
    stem is. The example headline said "ochiq vazn" and the research block said "kod yoki
    vazn", the model inflected the stem, and the gate rejected what the prompt taught.

    Stems, not exact forms: CALQUES lists the inflected renderings a model produces, while
    a prompt plants the uninflected root.
    """
    import re

    from apps.digest.llm import EDITORIAL_UZ_PROMPT, UZ_BLOCKS
    from apps.digest.translation_gates import CALQUES

    text = (EDITORIAL_UZ_PROMPT + " " + " ".join(UZ_BLOCKS.values())).lower()
    suffixes = ("lari", "lar", "li")

    for term, bad_forms in CALQUES.items():
        for bad in bad_forms:
            stem = bad.lower()
            for suffix in suffixes:
                if stem.endswith(suffix) and len(stem) - len(suffix) >= 4:
                    stem = stem[: -len(suffix)]
                    break
            assert not re.search(rf"\b{re.escape(stem)}", text), (
                f"the prompt contains {stem!r}, the Uzbek rendering of {term!r} that "
                f"check_glossary rejects. Rule 5 says this term stays in English."
            )


@respx.mock
def test_the_pipeline_makes_a_draft_and_a_rewrite_call_per_article(risk_article, settings):
    """Draft plus the language-only rewrite (2026-08-28). The count is pinned so a third
    call cannot creep in unnoticed - the old two-stage flow died precisely because its
    second call worked blind, and the rewrite is allowed only because it does not."""
    from apps.digest import llm
    from apps.digest.models import Analysis

    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"

    route = respx.post("http://gw.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(UZ_PAYLOAD)}}]}
        )
    )

    created = llm.analyse_for_digest_logic([risk_article.id])

    assert route.call_count == 2, "the draft call and the rewrite call, nothing more"
    assert len(created) == 1
    assert created[0].stage == Analysis.Stage.EDITORIAL_UZ
    stages = set(risk_article.analyses.values_list("stage", flat=True))
    assert Analysis.Stage.EDITORIAL_EN not in stages, "no English row is written any more"


@respx.mock
def test_the_pipeline_strips_markdown_from_the_uzbek(risk_article, settings):
    """The prompt forbids markdown and the old path stripped it anyway. Trusting the model
    is how bold reached the channel wrongly enough that the owner removed it on 2026-08-24.
    """
    from apps.digest import llm

    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"

    bolded = dict(UZ_PAYLOAD, lead_uz="**Qwen** jamoasi modelni ochiq taqdim etdi.")
    respx.post("http://gw.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(bolded)}}]}
        )
    )

    created = llm.analyse_for_digest_logic([risk_article.id])

    assert "**" not in created[0].payload["lead_uz"]


@respx.mock
def test_a_gate_violation_retries_once_with_the_violation_named(risk_article, settings):
    """The old path retried once with the violations appended. So does this one."""
    from apps.digest import llm

    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"

    invented = dict(UZ_PAYLOAD, body_1_uz="Model 999B parametrga ega.")
    prompts = []

    def capture(request):
        body = json.loads(request.content)
        prompts.append(body["messages"][0]["content"])
        payload = invented if len(prompts) == 1 else UZ_PAYLOAD
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    respx.post("http://gw.test/v1/chat/completions").mock(side_effect=capture)
    created = llm.analyse_for_digest_logic([risk_article.id])

    assert len(prompts) == 3, "one retry, then the rewrite"
    assert "999" in prompts[1], "the retry must name the violation it is fixing"
    assert prompts[2].startswith(llm.SIMPLIFY_UZ_PROMPT.split("{post_json}")[0])
    assert len(created) == 1


@respx.mock
def test_an_article_already_written_is_not_written_again(risk_article, settings):
    """The old path skipped an article that already had a usable row. Losing that makes a
    re-run pay for every article again."""
    from apps.digest import llm

    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"

    route = respx.post("http://gw.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(UZ_PAYLOAD)}}]}
        )
    )

    llm.analyse_for_digest_logic([risk_article.id])
    llm.analyse_for_digest_logic([risk_article.id])

    assert route.call_count == 2, "the second run must reuse the stored row, not pay again"


def test_a_real_boolean_still_survives_the_coercion():
    """A validator that swallowed everything would silently report nothing as local."""
    from apps.digest.llm import EditorialUz

    for given, expected in ((True, True), ("true", True), (False, False), ("false", False)):
        parsed = EditorialUz.model_validate(
            {"headline_uz": "H", "technical": {"local_deployable": given}}
        )
        assert parsed.technical.local_deployable is expected, given


def test_a_nonsense_local_deployable_is_still_an_error():
    """Only blank is forgiven. Anything else is a model that misread the field."""
    from pydantic import ValidationError

    from apps.digest.llm import EditorialUz

    with pytest.raises(ValidationError):
        EditorialUz.model_validate({"technical": {"local_deployable": "maybe"}})


def test_editorial_uz_prompt_documents_every_technical_field():
    """Schema and prompt must not drift: a field the prompt never names is never filled."""
    from apps.digest.llm import EDITORIAL_UZ_PROMPT, EDITORIAL_UZ_SCHEMA

    for field in EDITORIAL_UZ_SCHEMA["properties"]["technical"]["properties"]:
        assert field in EDITORIAL_UZ_PROMPT, f"prompt never mentions technical.{field}"


@pytest.mark.django_db
def test_classified_topic_reads_the_latest_classification(risk_article):
    """Two classifications can exist after a re-run; the newest is the live one."""
    from apps.digest import llm

    for topic in ("frontier_models", "safety_security"):
        Analysis.objects.create(
            article=risk_article,
            stage=Analysis.Stage.CLASSIFICATION,
            model_tag="smart",
            payload={
                "primary_topic": topic,
                "maturity": "live_product",
                "novelty": 5,
                "evidence": 5,
                "production_readiness": 5,
                "reason": "x",
            },
            latency_ms=1,
        )

    assert llm._classified_topic(risk_article) == "safety_security"
    assert llm.shape_for(llm._classified_topic(risk_article)) == "risk"


def test_an_unknown_topic_falls_back_to_the_general_shape():
    """A Topic added later must degrade to today's behaviour, not raise."""
    from apps.digest.llm import SHAPE_GENERAL, shape_for

    assert shape_for("a_topic_invented_next_year") == SHAPE_GENERAL
    assert shape_for(None) == SHAPE_GENERAL
    assert shape_for("") == SHAPE_GENERAL


def test_irrelevant_is_not_mapped():
    """It never reaches the editorial stage; classification filters it."""
    from apps.digest.llm import TOPIC_SHAPES
    from apps.digest.models import Topic

    assert Topic.IRRELEVANT.value not in TOPIC_SHAPES


@respx.mock
def test_strict_json_schema_is_requested_not_json_object(risk_article, settings):
    """Measured 2026-08-17: json_object conformed 2/7 times on real articles because the
    model invented its own keys. Ollama enforces the schema in the decoder; an
    OpenAI-compatible endpoint only does so when strict mode is asked for explicitly."""
    from apps.digest import llm

    settings.LLM_PROVIDER = "mimo"
    settings.EDITORIAL_UZ_PROVIDER = "mimo"
    settings.MIMO_BASE_URL = "https://mimo.test/v1"
    settings.MIMO_API_KEY = "k"

    captured = {}

    def capture(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(UZ_PAYLOAD)}}]}
        )

    respx.post("https://mimo.test/v1/chat/completions").mock(side_effect=capture)
    llm.editorial_chat(prompt="x", schema=llm.EDITORIAL_UZ_SCHEMA, num_predict=100)

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True


# --- Recovered from tests/test_editorial.py -----------------------------------
# That file was deleted whole when the two-stage flow went, and 41 of its 48 tests went
# correctly with it. These five did not: they cover code the single-stage path still runs,
# and the suite was green without them because nothing was left to notice.


def test_triage_keep_rules_are_independent():
    """A-D must each be sufficient on their own.

    Measured 2026-08-25 while tuning: an earlier draft said the naming test "overrides
    everything else", and the model read that as subordinating the other rules to it.
    "Disrupting a covert influence campaign" names no model, so it was dropped — recall
    fell to 0.80 on the replay. Stating the rules as independent restored it to 1.00.
    """
    from apps.digest.llm import TRIAGE_PROMPT_TEMPLATE

    assert "ANY ONE of these holds" in TRIAGE_PROMPT_TEMPLATE
    assert "They are independent" in TRIAGE_PROMPT_TEMPLATE
    for rule in ("A.", "B.", "C.", "D."):
        assert rule in TRIAGE_PROMPT_TEMPLATE, f"keep rule {rule} is missing"
    # Rule C is the one an override clause silently disables.
    assert "even when nothing is named" in TRIAGE_PROMPT_TEMPLATE


def test_triage_does_not_ask_the_model_to_judge_significance():
    """Significance needs the article. Triage decides whether the article is worth reading,
    and says so, or the fast tier starts guessing at what classification is for."""
    from apps.digest.llm import TRIAGE_PROMPT_TEMPLATE

    assert "cannot tell how significant it is, keep it" in TRIAGE_PROMPT_TEMPLATE
    assert "belongs to the classification stage" in TRIAGE_PROMPT_TEMPLATE


def test_a_blank_local_deployable_does_not_cost_a_retry():
    """The one non-string field in `technical`, and models returned '' for it.

    Measured 2026-08-26 on MiMo: '' raised ValidationError, `_editorial_call` retried, and
    the article cost 5346 input tokens instead of 2682. Blank means the article did not
    say, which is exactly the default. The validator moved to EditorialUz with the merge;
    its test did not, and was restored the same day.
    """
    from apps.digest.llm import EditorialUz

    parsed = EditorialUz.model_validate({"headline_uz": "H", "technical": {"local_deployable": ""}})
    assert parsed.technical.local_deployable is False


def test_rendering_requires_the_uzbek_editorial(risk_article):
    """Rendering fails loudly rather than falling back (ADR-003).

    There is no English text to fall back to any more, which makes the loud failure the
    only behaviour left — and therefore the one worth pinning.
    """
    from datetime import date

    from apps.digest import ranking
    from apps.digest.models import Digest, DigestItem

    digest = Digest.objects.create(digest_date=date(2026, 8, 26))
    DigestItem.objects.create(digest=digest, article=risk_article, position=1, score=0.85)

    with pytest.raises(ValueError, match="lacks editorial_uz"):
        ranking.render_item_post(digest.items.first())


@respx.mock
def test_an_article_with_no_classification_gets_the_general_block(settings):
    """shape_for(None) returns the general key. Nothing in the pipeline reaches editorial
    without a classification, but a direct call must not crash on one."""
    from apps.digest import llm

    source = Source.objects.create(
        name="uz_src_nc", connector=Source.Connector.RSS, url="https://e.test/rss2", priority=50
    )
    article = Article.objects.create(
        source=source,
        canonical_url="https://e.test/nc",
        content_hash="uz_nc",
        title="Something happened",
        extracted_text="Body text. " * 40,
        status=Article.Status.CLASSIFIED,
    )

    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"

    prompts = []

    def capture(request):
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(UZ_PAYLOAD)}}]}
        )

    respx.post("http://gw.test/v1/chat/completions").mock(side_effect=capture)
    llm.analyse_for_digest_logic([article.id])

    assert llm.UZ_BLOCKS[llm.SHAPE_GENERAL] in prompts[0]


def test_the_rewrite_may_not_blank_the_headline(risk_article, monkeypatch):
    """body_1_uz and kicker_uz may be emptied by the rewrite; headline_uz may not.

    The merge accepted any string the rewrite returned, guarding only lead_uz, and the
    prompt tells the rewrite that emptying a field is allowed. An empty headline then
    survives every gate -- no numbers to check, the case gate skips a falsy headline --
    so the post shipped with no headline line at all.
    """
    from apps.digest import llm, post_format

    draft = _draft_result()._replace(
        payload={**_draft_result().payload, "post_style": post_format.PLAIN_PHOTO_STYLE}
    )
    rewritten = draft._replace(payload={**draft.payload, "headline_uz": ""})
    monkeypatch.setattr(llm, "_editorial_call", lambda **kwargs: rewritten)
    result = llm._simplify_editorial_uz(risk_article, draft)
    assert result is not None
    assert result.payload["headline_uz"] == draft.payload["headline_uz"]


def test_a_discarded_editorial_is_recorded_and_not_re_drafted(risk_article, monkeypatch):
    """A post that fails its gates twice costs real tokens; the row must say so.

    `continue` alone skipped `_record_analysis`, so the calls disappeared from the
    accounting and the article was re-drafted on every later cycle because the reuse
    memo had nothing to find.
    """
    from apps.digest import llm, post_format

    draft = _draft_result()._replace(
        payload={
            **_draft_result().payload,
            "post_style": post_format.PLAIN_PHOTO_STYLE,
            # Eleven words: over the ten-word cap, on every attempt.
            "headline_uz": "Bir ikki uch tort besh olti yetti sakkiz toqqiz on bir",
        }
    )
    calls = []

    def _call(**kwargs):
        calls.append(1)
        return draft

    monkeypatch.setattr(llm, "_editorial_call", _call)
    assert llm.analyse_for_digest_logic([risk_article.id]) == []

    row = risk_article.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ).first()
    assert row is not None, "the cost of a discarded editorial must still be recorded"
    assert row.payload["discarded_violations"]

    # A second cycle must not re-draft it.
    spent = len(calls)
    assert llm.analyse_for_digest_logic([risk_article.id]) == []
    assert len(calls) == spent, "a discarded article was re-drafted at full cost"


def test_plain_rewrite_can_remove_secondary_technical_detail(risk_article, monkeypatch):
    from apps.digest import llm, post_format

    draft = _draft_result()._replace(
        payload={
            **_draft_result().payload,
            "post_style": post_format.PLAIN_PHOTO_STYLE,
        }
    )
    rewritten = draft._replace(payload={**draft.payload, "body_1_uz": ""})
    monkeypatch.setattr(llm, "_editorial_call", lambda **kwargs: rewritten)
    result = llm._simplify_editorial_uz(risk_article, draft)
    assert result is not None
    assert result.payload["body_1_uz"] == ""
    assert result.payload["technical"] == draft.payload["technical"]


def test_both_prompts_keep_the_measured_language_guards():
    """The rewrite dropped two guards that each pin a defect measured in production.

    The calque rule ("atlatdi" -> "chetlab o'tdi") and the empty-praise ban
    ("inqilobiy", "ulkan yutuq") were deleted with the prompts they lived in, and with
    the test that pinned them. Neither has a mechanical gate behind it: `CALQUES` in
    translation_gates covers six English ML terms and cannot fire on a Turkish verb
    form, and nothing at all checks for hype. The prompt is the only guard, so the
    prompt has to keep saying it.
    """
    from apps.digest.editorial_prompts import EDITORIAL_UZ_PROMPT, SIMPLIFY_UZ_PROMPT

    for name, prompt in (("draft", EDITORIAL_UZ_PROMPT), ("rewrite", SIMPLIFY_UZ_PROMPT)):
        lowered = prompt.lower()
        assert "atlat" in lowered, f"{name} prompt lost the Turkish/Russian calque rule"
        assert "inqilobiy" in lowered, f"{name} prompt lost the empty-praise ban"


def test_the_rewrite_is_told_which_fields_may_not_be_emptied():
    """ "Bo'sh maydon yaratish mumkin" was unqualified, and the merge trusted it.

    body_1_uz and kicker_uz are genuinely droppable; headline_uz and lead_uz are not.
    The code guards this too (see test_the_rewrite_may_not_blank_the_headline); the
    prompt has to agree, or every post pays a rewrite that the merge then rejects.
    """
    from apps.digest.editorial_prompts import SIMPLIFY_UZ_PROMPT

    assert "headline_uz va lead_uz hech qachon bo'sh qolmaydi" in SIMPLIFY_UZ_PROMPT


def test_the_rewrite_keeps_the_sentence_agency_rules():
    """Measured 2026-08-28: these three rules were what finally moved the register.

    They were asserted by `test_the_sentence_structure_rules_live_with_the_rewrite`,
    deleted in the same change that removed them from the prompt.
    """
    from apps.digest.editorial_prompts import SIMPLIFY_UZ_PROMPT

    assert "egasi aniq" in SIMPLIFY_UZ_PROMPT
    assert "Ot zanjiri" in SIMPLIFY_UZ_PROMPT
    assert "kim endi nima qila olishini" in SIMPLIFY_UZ_PROMPT
