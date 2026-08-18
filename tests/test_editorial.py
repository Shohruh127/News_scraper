"""Editorial stages (ADR-005): English analysis, then Uzbek translation.

The two stages are separate so a poor post can be traced to comprehension or to
translation, never to an ambiguous single step. These tests pin that separation.
"""

import json
from datetime import date

import httpx
import pytest
import respx

from apps.digest import llm, ranking
from apps.digest.models import Analysis, Article, Digest, DigestItem, Source
from tests.helpers import make_editorial

pytestmark = pytest.mark.django_db


EN_PAYLOAD = {
    "headline_en": "Qwen releases 2.4T open-weight model",
    "summary_en": "Qwen released a 2.4 trillion parameter open-weight model.",
    "why_it_matters_en": "It can be self-hosted.",
    "leadership_en": "Reduces API dependency.",
    "uzbekistan_application_en": "Local teams can self-host it.",
    "technical": {
        "what_was_built": "A 2.4T MoE model",
        "limitations": "Requires large VRAM",
        "local_deployable": True,
    },
    "evidence_level": "vendor_claim_only",
}

UZ_PAYLOAD = {
    "headline_uz": "Qwen 2.4T open-weight model chiqardi",
    "summary_uz": "Qwen 2.4 trillion parametrli open-weight model taqdim etdi.",
    "why_it_matters_uz": "Uni mahalliy serverda ishlatish mumkin.",
    "leadership_uz": "API'ga bog'liqlikni kamaytiradi.",
    "uzbekistan_application_uz": "Mahalliy jamoalar o'zida joylashtira oladi.",
}


@pytest.fixture
def source(db):
    return Source.objects.create(
        name="test_editorial_src",
        connector=Source.Connector.RSS,
        url="https://example.com/rss",
        priority=85,
    )


@pytest.fixture
def article(source):
    return Article.objects.create(
        source=source,
        canonical_url="https://example.com/editorial-art",
        content_hash="h_ed",
        title="Qwen Open Weights Release",
        extracted_text="Qwen released open weights with FP8 quantisation." * 20,
        status=Article.Status.CLASSIFIED,
    )


@respx.mock
def test_two_stages_produce_two_analyses_on_ollama(article, settings):
    settings.LLM_PROVIDER = "ollama"
    settings.EDITORIAL_EN_PROVIDER = "ollama"
    settings.TRANSLATION_PROVIDER = "ollama"
    settings.OLLAMA_FAST_MODEL = "gemma4:31b"
    settings.OLLAMA_BASE_URL = "http://localhost:11434"
    settings.OLLAMA_DEEP_MODEL = "gemma4:31b"

    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "gemma4:31b", "digest": "d31b"}]}
        )
    )
    respx.post("http://localhost:11434/api/chat").mock(
        side_effect=[
            httpx.Response(200, json={"message": {"content": json.dumps(EN_PAYLOAD)}}),
            httpx.Response(200, json={"message": {"content": json.dumps(UZ_PAYLOAD)}}),
        ]
    )

    result = llm.analyse_for_digest_logic([article.id])

    assert len(result) == 1
    assert result[0].stage == Analysis.Stage.EDITORIAL_UZ
    stages = set(article.analyses.values_list("stage", flat=True))
    assert stages == {Analysis.Stage.EDITORIAL_EN, Analysis.Stage.EDITORIAL_UZ}

    en = article.analyses.get(stage=Analysis.Stage.EDITORIAL_EN)
    assert en.payload["summary_en"].startswith("Qwen released")
    assert en.payload["technical"]["local_deployable"] is True
    # The Ollama tag can be repointed silently, so the digest is recorded.
    assert en.model_digest == "d31b"


@respx.mock
def test_two_stages_on_mimo_record_the_mimo_tag(article, settings):
    """MiMo is OpenAI-compatible, so the response envelope differs and must be normalised."""
    settings.LLM_PROVIDER = "mimo"
    settings.EDITORIAL_EN_PROVIDER = "mimo"
    settings.TRANSLATION_PROVIDER = "mimo"
    settings.MIMO_BASE_URL = "https://mimo.test/v1"
    settings.MIMO_API_KEY = "test-key"
    settings.MIMO_EDITORIAL_MODEL = "mimo-v2.5"

    def mimo(payload):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    route = respx.post("https://mimo.test/v1/chat/completions").mock(
        side_effect=[mimo(EN_PAYLOAD), mimo(UZ_PAYLOAD)]
    )

    result = llm.analyse_for_digest_logic([article.id])

    assert route.call_count == 2, "one call for English, one for translation"
    assert len(result) == 1
    uz = result[0]
    assert uz.model_tag == "mimo-v2.5"
    # MiMo exposes no digest, so the drift-detection field is deliberately empty.
    assert uz.model_digest == ""
    assert uz.payload["summary_uz"].startswith("Qwen 2.4 trillion")


@respx.mock
def test_strict_json_schema_is_requested_not_json_object(article, settings):
    """Measured 2026-08-17: json_object conformed 2/7 times on real articles because the
    model invented its own keys. Ollama enforces the schema in the decoder; an
    OpenAI-compatible endpoint only does so when strict mode is asked for explicitly."""
    settings.LLM_PROVIDER = "mimo"
    settings.EDITORIAL_EN_PROVIDER = "mimo"
    settings.TRANSLATION_PROVIDER = "mimo"
    settings.MIMO_BASE_URL = "https://mimo.test/v1"
    settings.MIMO_API_KEY = "k"

    captured = {}

    def capture(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(EN_PAYLOAD)}}]}
        )

    respx.post("https://mimo.test/v1/chat/completions").mock(side_effect=capture)
    llm.editorial_chat(prompt="x", schema=llm.EDITORIAL_EN_SCHEMA, num_predict=100)

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True


@respx.mock
def test_translation_never_sees_the_article_only_the_english(article, settings):
    """The translator must translate, not re-summarise. It receives the English fields,
    not the source text, so it cannot add information the analysis did not find."""
    settings.LLM_PROVIDER = "mimo"
    settings.EDITORIAL_EN_PROVIDER = "mimo"
    settings.TRANSLATION_PROVIDER = "mimo"
    settings.MIMO_BASE_URL = "https://mimo.test/v1"
    settings.MIMO_API_KEY = "k"

    prompts = []

    def capture(request):
        body = json.loads(request.content)
        prompts.append(body["messages"][0]["content"])
        payload = EN_PAYLOAD if len(prompts) == 1 else UZ_PAYLOAD
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    respx.post("https://mimo.test/v1/chat/completions").mock(side_effect=capture)
    llm.analyse_for_digest_logic([article.id])

    assert len(prompts) == 2
    assert "FP8 quantisation" in prompts[0], "English stage gets the article text"
    assert "FP8 quantisation" not in prompts[1], "translation stage must not get the source"
    assert "Qwen released a 2.4 trillion" in prompts[1], "it gets the English fields instead"


def test_rendering_requires_the_translation_stage(article):
    """Rendering must fail loudly rather than fall back to English (ADR-003)."""
    Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
            "reason": "English reason only",
        },
        latency_ms=1000,
    )
    digest = Digest.objects.create(digest_date=date(2026, 8, 14))
    DigestItem.objects.create(digest=digest, article=article, position=1, score=0.85)

    with pytest.raises(ValueError, match="English fallback is prohibited"):
        ranking.render_channel_post(digest)


def test_rendering_succeeds_with_both_stages(article):
    Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
        latency_ms=1000,
    )
    make_editorial(article, summary_uz="Yangi arxitektura sinovdan o'tdi.",
                   built="Fast transformer layer", limitations="Memory bounds")

    digest = Digest.objects.create(digest_date=date(2026, 8, 14))
    DigestItem.objects.create(digest=digest, article=article, position=1, score=0.85)

    post = ranking.render_channel_post(digest)
    assert "Yangi arxitektura sinovdan o'tdi." in post
    assert "English reason" not in post

    # The technical appendix reads the English stage on purpose: repo URLs, licences and
    # install commands are English artefacts and are not translated.
    appendix = ranking.render_group_comment(digest)
    assert "Fast transformer layer" in appendix


def test_archetype_enum_matches_the_detail_blocks():
    """Every archetype must have a block, and every block an archetype."""
    from apps.digest.llm import ARCHETYPES, EDITORIAL_EN_SCHEMA

    props = EDITORIAL_EN_SCHEMA["properties"]
    assert set(props["archetype"]["enum"]) == set(ARCHETYPES)
    for name in ARCHETYPES:
        assert f"{name}_details" in props, f"{name} has no detail block"


def test_no_detail_field_is_required_in_the_schema():
    """A strict schema does not make the model know an answer, it makes it produce one.

    Measured 2026-08-18: a change to a default sampling parameter was given HIGH severity.
    """
    from apps.digest.llm import ARCHETYPES, EDITORIAL_EN_SCHEMA

    top_required = EDITORIAL_EN_SCHEMA["required"]
    for name in ARCHETYPES:
        block = EDITORIAL_EN_SCHEMA["properties"][f"{name}_details"]
        assert not block.get("required"), f"{name}_details marks fields required"
        assert f"{name}_details" not in top_required


def test_editorial_model_accepts_one_block_and_none():
    """The model validates a payload with a single block, and one with no block at all."""
    from apps.digest.llm import EditorialEn

    common = {
        "headline_en": "Ollama v0.32.10 changes the default repeat penalty",
        "summary_en": "The release changes a default and speeds up prefill.",
        "why_it_matters_en": "It standardises behaviour across engines.",
        "leadership_en": "A routine update with a measurable speedup.",
        "uzbekistan_application_en": "Local teams running Ollama benefit directly.",
        "technical": {
            "what_was_built": "Ollama v0.32.10",
            "limitations": "Applies to NVFP4 MLX models only",
            "local_deployable": True,
        },
        "evidence_level": "vendor_claim_only",
    }

    with_block = EditorialEn(
        archetype="release",
        release_details={"what_changed_en": "repeat_penalty now defaults to 1.0"},
        **common,
    )
    assert with_block.release_details.what_changed_en.startswith("repeat_penalty")
    assert with_block.risk_hardening_details is None

    without_block = EditorialEn(archetype="release", **common)
    assert without_block.release_details is None


def test_archetype_definitions_are_in_the_prompt():
    """The definitions moved accuracy from 0/6 to 5/6, so their absence is a defect."""
    from apps.digest.llm import ARCHETYPES, EDITORIAL_EN_PROMPT

    for name in ARCHETYPES:
        assert name in EDITORIAL_EN_PROMPT, f"{name} is not defined in the prompt"
    assert "Pricing is not policy" in EDITORIAL_EN_PROMPT

