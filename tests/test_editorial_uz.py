"""The single-stage Uzbek editorial (2026-08-26 design).

Phase 1 builds this beside the two-stage flow. Nothing here touches the pipeline.
"""

import json
import re

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


def test_every_uzbek_block_states_its_word_limits():
    """The block is read before the field bullets, so it has to carry the limits itself.

    Measured 2026-08-26 on the English shape blocks: the risk block produced a 24-word lead
    against an 18-word cap stated further down the prompt. The content instruction wins.
    """
    from apps.digest.llm import SHAPE_GENERAL, UZ_BLOCKS

    for key, block in UZ_BLOCKS.items():
        if key == SHAPE_GENERAL:
            assert "14 so'z" in block and "16 so'z" in block and "8 so'z" in block
            continue
        for cap in ("<= 14 so'z", "<= 16 so'z", "<= 8 so'z"):
            assert cap in block, f"{key} block does not state {cap}"


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


def test_the_prompt_scopes_the_block_to_the_three_sentences():
    """The English agent block wrote the headline until an equivalent guard was added."""
    from apps.digest.llm import EDITORIAL_UZ_PROMPT

    guard = EDITORIAL_UZ_PROMPT.split("## What this story needs")[1].split("{block}")[0]
    assert "three sentences only" in guard
    assert "headline_uz" in guard


def test_the_prompt_states_the_word_limits_in_the_field_definitions():
    """Stated twice on purpose. This pins the second copy."""
    from apps.digest.llm import EDITORIAL_UZ_PROMPT

    fields = EDITORIAL_UZ_PROMPT.split("## Output fields")[1].split("## Style rules")[0]
    assert "AT MOST 8 UZBEK WORDS" in fields
    assert "AT MOST 14 UZBEK WORDS" in fields
    assert "AT MOST 16 UZBEK WORDS" in fields


def test_the_prompt_exempts_local_deployable_from_the_empty_string_rule():
    """MiMo returned '' for the boolean on 2026-08-26 because the prompt asked for it,
    and the retry cost that article a second call."""
    from apps.digest.llm import EDITORIAL_UZ_PROMPT

    rule = EDITORIAL_UZ_PROMPT.split("- technical:")[1].split("## Style rules")[0]
    assert "EXCEPT" in rule and "local_deployable, which is a boolean" in rule


def _example_payloads():
    """Every JSON object under the examples heading, parsed."""
    import json

    from apps.digest.llm import EDITORIAL_UZ_PROMPT

    section = EDITORIAL_UZ_PROMPT.split("## Namunalar")[1].split("ARTICLE")[0]
    # The prompt doubles its braces for str.format; undo that before parsing.
    section = section.replace("{{", "{").replace("}}", "}")
    return [json.loads(m) for m in re.findall(r"\{\s*\"headline_uz\".*?\n\}", section, re.S)]


def test_the_examples_are_valid_json():
    """A previous prompt shipped examples with real newlines inside string values."""
    payloads = _example_payloads()
    assert len(payloads) == 2, "two examples"


def test_the_examples_obey_the_limits_they_teach():
    """A model copies a shown example over a stated rule, so an example that breaks the
    cap teaches the model to break it. Measured 2026-08-26: the shape block's content
    instruction beat a limit stated later in the prompt."""
    caps = {"headline_uz": 8, "lead_uz": 14, "body_1_uz": 16, "kicker_uz": 8}
    for payload in _example_payloads():
        for field, cap in caps.items():
            words = len(payload[field].split())
            assert words <= cap, f"{field} example is {words} words, cap is {cap}"


def test_the_examples_are_one_sentence_each():
    """Three sentences total is the whole post contract."""

    for payload in _example_payloads():
        for field in ("lead_uz", "body_1_uz", "kicker_uz"):
            sents = [s for s in re.split(r"(?<=[.!?])\s+", payload[field].strip()) if s.strip()]
            assert len(sents) == 1, f"{field} must be exactly one sentence"
            assert payload[field].rstrip().endswith("."), f"{field} must end with a full stop"
        assert not payload["headline_uz"].endswith("."), "the headline is a label"


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
        extracted_text="Details of the jailbreak. " * 40,
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
