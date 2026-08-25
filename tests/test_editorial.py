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
    "lead_en": "Qwen released 2.4T open-weight model for enterprise reasoning.",
    "body_1_en": "The model features 2.4 trillion parameters and scores high on benchmarks.",
    "body_2_en": "",
    "why_it_matters_en": "It can be self-hosted.",
    "uzbekistan_application_en": "Local teams can self-host it.",
    "archetype": "release",
    "evidence_level": "vendor_claim_only",
}

UZ_PAYLOAD = {
    "lead_uz": "Qwen jamoasi 2.4T parametrli ochiq modelni taqdim etdi.",
    "body_1_uz": "Model 2.4 trillion parametrga ega bo'lib, yuqori natijalar ko'rsatgan.",
    "body_2_uz": "",
    "why_it_matters_uz": "Uni mahalliy serverda ishlatish mumkin.",
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
def test_two_stages_produce_two_analyses_on_the_gateway(article, settings):
    settings.LLM_PROVIDER = "gateway"
    settings.EDITORIAL_EN_PROVIDER = "gateway"
    settings.TRANSLATION_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"
    settings.GATEWAY_FAST_MODEL = "fast"
    settings.GATEWAY_SMART_MODEL = "smart"

    def reply(payload):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    route = respx.post("http://gw.test/v1/chat/completions").mock(
        side_effect=[reply(EN_PAYLOAD), reply(UZ_PAYLOAD)]
    )

    result = llm.analyse_for_digest_logic([article.id])

    assert len(result) == 1
    assert result[0].stage == Analysis.Stage.EDITORIAL_UZ
    stages = set(article.analyses.values_list("stage", flat=True))
    assert stages == {Analysis.Stage.EDITORIAL_EN, Analysis.Stage.EDITORIAL_UZ}

    en = article.analyses.get(stage=Analysis.Stage.EDITORIAL_EN)
    assert en.payload["lead_en"].startswith("Qwen released")
    # English on the deep tier, translation on the fast one — the 2026-08-17 measurement.
    assert [json.loads(c.request.content)["model"] for c in route.calls] == ["smart", "fast"]
    # No digest: only Ollama exposed /api/tags, and that path was removed 2026-08-25.
    assert en.model_digest == ""


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
    assert uz.payload["lead_uz"].startswith("Qwen jamoasi")


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
    assert "Qwen released 2.4T" in prompts[1], "it gets the English fields instead"


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

    with pytest.raises(ValueError, match="lacks editorial_uz"):
        ranking.render_item_post(digest.items.first())


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
    make_editorial(
        article,
        lead_uz="Yangi arxitektura sinovdan o'tdi.",
        built="Fast transformer layer",
        limitations="Memory bounds",
    )

    digest = Digest.objects.create(digest_date=date(2026, 8, 14))
    item = DigestItem.objects.create(digest=digest, article=article, position=1, score=0.85)

    post = ranking.render_item_post(item)
    assert "Yangi arxitektura" in post
    assert "English reason" not in post

    # The technical appendix reads the English stage on purpose: repo URLs, licences and
    # install commands are English artefacts and are not translated.
    appendix = ranking.render_item_appendix(item)
    assert "Fast transformer layer" in appendix


def test_the_editorial_schema_asks_only_for_what_gets_published():
    """Every field the model fills must reach a reader.

    `why_it_matters_en`, `uzbekistan_application_en`, `archetype` and `technical.hardware`
    were generated, translated and then rendered nowhere. Removed 2026-08-24: they cost
    output tokens on both calls and split the model's attention across fields with no reader.
    """
    from apps.digest.llm import EDITORIAL_EN_SCHEMA

    props = EDITORIAL_EN_SCHEMA["properties"]
    assert set(props) == {
        "headline_en",
        "lead_en",
        "body_1_en",
        "kicker_en",
        "evidence_level",
        "technical",
    }
    assert "hardware" not in props["technical"]["properties"]


def test_micro_pipeline_schema_required_fields():
    """Strict micro-pipeline required fields."""
    from apps.digest.llm import EDITORIAL_EN_SCHEMA

    required = EDITORIAL_EN_SCHEMA["required"]
    for field in ["headline_en", "lead_en", "body_1_en", "kicker_en"]:
        assert field in required


def test_editorial_model_validation():
    """The model validates a micro-pipeline payload."""
    from apps.digest.llm import EditorialEn

    payload = {
        "headline_en": "Ollama v0.32.10 doubles prefill speed",
        "lead_en": "Ollama released v0.32.10 with speedup.",
        "body_1_en": "The release changes a default and speeds up prefill by 2x.",
        "kicker_en": "One fewer flag to remember.",
        "evidence_level": "vendor_claim_only",
    }
    obj = EditorialEn(**payload)
    assert obj.lead_en.startswith("Ollama")


def test_translation_schema_follows_the_fields_it_is_given():
    """A block absent from the schema cannot be filled by a model that felt like filling it.

    Measured 2026-08-18: given six visible blocks and no definitions, the model filled six
    irrelevant ones.
    """
    from apps.digest.llm import translation_schema_for

    schema = translation_schema_for({"headline_en": "x", "summary_en": "y", "what_changed_en": "z"})
    assert set(schema["properties"]) == {"headline_uz", "summary_uz", "what_changed_uz"}
    assert set(schema["required"]) == {"headline_uz", "summary_uz", "what_changed_uz"}
    assert "policy_details" not in schema["properties"]


def test_translation_schema_only_rewrites_a_trailing_suffix():
    """`_en` is replaced at the end of the key, never in the middle of a word."""
    from apps.digest.llm import translation_schema_for

    schema = translation_schema_for({"deployment_en": "a", "residual_en": "b"})
    assert set(schema["properties"]) == {"deployment_uz", "residual_uz"}


def test_technical_fields_selects_prose_and_suffixes_it():
    """Prose is translated; URLs and commands are not.

    `install` is excluded because it is mixed: of five stored values two were prose and one was
    the bare command `ollama run muse-glimmer`. A mangled command is actively wrong — someone
    may run it — while an untranslated short phrase is merely suboptimal. The appendix already
    renders it inside <code>.
    """
    from apps.digest.llm import technical_fields

    payload = {
        "technical": {
            "what_was_built": "A minor version update for the checkpoint library.",
            "architecture": "Uses a custom database called DeltaDB.",
            "limitations": "Limited to American Sign Language.",
            "benchmarks": "Scores 70 BLEURT on FLEURS-ASL.",
            "hardware": "Spare smartphone or PC with a webcam.",
            "install": "ollama run muse-glimmer",
            "repo_url": "https://github.com/langchain-ai/langgraph",
            "api_url": "https://example.com/api",
            "license": "",
            "local_deployable": True,
        }
    }

    out = technical_fields(payload)

    assert set(out) == {
        "what_was_built_en",
        "architecture_en",
        "limitations_en",
        "benchmarks_en",
    }
    # `hardware` is supplied above but is not translated: item_appendix.html never rendered
    # it, so it was dropped with the other unpublished fields on 2026-08-24.
    assert "hardware_en" not in out
    assert out["what_was_built_en"].startswith("A minor version update")


def test_technical_fields_skips_empty_values():
    """A field the model could not ground stays out of the translation call."""
    from apps.digest.llm import technical_fields

    payload = {"technical": {"what_was_built": "Something", "architecture": "   "}}

    assert technical_fields(payload) == {"what_was_built_en": "Something"}


def test_technical_fields_handles_a_missing_block():
    """An article with no technical block must not raise."""
    from apps.digest.llm import technical_fields

    assert technical_fields({}) == {}


def test_technical_prose_reaches_the_translation_schema():
    """The `_en` suffix is what makes the existing dynamic schema produce `_uz`."""
    from apps.digest.llm import technical_fields, translation_schema_for

    fields = technical_fields({"technical": {"benchmarks": "7-8% faster prefill"}})

    assert set(translation_schema_for(fields)["properties"]) == {"benchmarks_uz"}


def test_editorial_en_v2_schema_validation():
    """EditorialEn validates clean v2 prose fields while remaining backward-compatible."""
    v2_data = {
        "lead_en": "EHang launched a fully autonomous passenger eVTOL route.",
        "body_1_en": "Flights take 20 minutes and cost 800 yuan per seat.",
        "body_2_en": "Civil aviation regulators issued complete type certificates.",
        "why_it_matters_en": "Commercialises urban air mobility.",
        "uzbekistan_application_en": "Could inform regional drone delivery regulations.",
        "technical": {
            "what_was_built": "EH216-S aircraft",
            "limitations": "30km range",
            "local_deployable": False,
        },
        "evidence_level": "vendor_claim_only",
        "archetype": "company_product",
    }
    model = llm.EditorialEn.model_validate(v2_data)
    assert model.lead_en.startswith("EHang")


def test_translation_schema_for_v2_fields():
    """Dynamic translation schema derives matching _uz properties for all v2 fields."""
    v2_en_fields = {
        "lead_en": "Lead text",
        "body_1_en": "Body 1 text",
        "body_2_en": "Body 2 text",
        "why_it_matters_en": "Why text",
        "uzbekistan_application_en": "UZ text",
    }
    schema = llm.translation_schema_for(v2_en_fields)
    expected = {
        "lead_uz",
        "body_1_uz",
        "body_2_uz",
        "why_it_matters_uz",
        "uzbekistan_application_uz",
    }
    assert set(schema["properties"]) == expected
    assert set(schema["required"]) == expected


# --- v3 prompt and schema contracts -------------------------------------------


def test_no_schema_mentions_the_link_anchor():
    from apps.digest.llm import (
        COMMON_TRANSLATED_FIELDS,
        EDITORIAL_EN_SCHEMA,
        TRANSLATION_SCHEMA,
    )

    assert "link_anchor_en" not in EDITORIAL_EN_SCHEMA["properties"]
    assert "link_anchor_en" not in EDITORIAL_EN_SCHEMA["required"]
    assert "link_anchor_uz" not in TRANSLATION_SCHEMA["properties"]
    assert "link_anchor_uz" not in TRANSLATION_SCHEMA["required"]
    assert "link_anchor_en" not in COMMON_TRANSLATED_FIELDS


def test_editorial_en_schema_covers_the_appendix_template():
    """A field the appendix renders but the schema cannot produce is dead ink."""
    import re as _re
    from pathlib import Path

    from django.conf import settings as _settings

    from apps.digest.llm import EDITORIAL_EN_SCHEMA

    template = Path(_settings.BASE_DIR) / "apps/digest/templates/digest/item_appendix.html"
    rendered_vars = set(_re.findall(r"{{\s*(\w+)", template.read_text(encoding="utf-8")))
    technical_props = set(EDITORIAL_EN_SCHEMA["properties"]["technical"]["properties"])
    from_technical = {
        "what_was_built",
        "architecture",
        "license",
        "repo_url",
        "api_url",
        "install",
        "benchmarks",
        "limitations",
    }
    missing = (rendered_vars & from_technical) - technical_props
    assert not missing, f"appendix renders {sorted(missing)} but the schema cannot produce them"
    assert "local_deployable" in technical_props


def test_editorial_en_prompt_documents_every_technical_field():
    """Schema and prompt must not drift: a field the prompt never names is never filled."""
    from apps.digest.llm import EDITORIAL_EN_PROMPT, EDITORIAL_EN_SCHEMA

    for field in EDITORIAL_EN_SCHEMA["properties"]["technical"]["properties"]:
        assert field in EDITORIAL_EN_PROMPT, f"prompt never mentions technical.{field}"


def _editorial_examples():
    """Both few-shot examples from EDITORIAL_EN_PROMPT, parsed."""
    import json as _json
    import re as _re

    from apps.digest.llm import EDITORIAL_EN_PROMPT

    filled = EDITORIAL_EN_PROMPT.format(title="T", source="S", text="X")
    blocks = _re.findall(r"Output JSON:\n(\{.*?\n\})\n", filled, _re.DOTALL)
    return [_json.loads(b) for b in blocks]


def test_editorial_en_prompt_examples_obey_their_own_rules():
    """The few-shot dominates the model's behaviour, so it must not contradict the rules."""
    import re as _re

    def sentences(text):
        return [s for s in _re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]

    examples = _editorial_examples()
    assert len(examples) == 2, "both few-shot examples must be parseable"

    for i, example in enumerate(examples, 1):
        assert len(sentences(example["lead_en"])) == 1, f"{i}: lead_en must be one sentence"
        assert example["lead_en"].rstrip().endswith("."), f"{i}: lead_en must be complete"
        assert len(sentences(example["body_1_en"])) == 1, f"{i}: body_1_en must be one sentence"
        assert len(example["headline_en"].split()) <= 8, f"{i}: headline_en is capped at 8 words"
        assert not example["headline_en"].rstrip().endswith("."), f"{i}: headline is not a sentence"
        assert len(example["kicker_en"].split()) <= 8, f"{i}: kicker_en is capped at 8 words"
        assert "link_anchor_en" not in example, "the anchor is derived, not modelled"
        assert "technical" in example, "the appendix depends on the technical block"
        for dead in ("body_2_en", "why_it_matters_en", "uzbekistan_application_en", "archetype"):
            assert dead not in example, f"{i}: {dead} was removed from the contract"


def test_the_second_example_teaches_the_no_number_case():
    """The old prompt demanded a number in body_1_en unconditionally.

    Measured 2026-08-24 on three live articles: two had no figure to give and the rule was
    simply ignored, which teaches the model that the rules around it are optional too. The
    second example exists to show what body_1_en does when the article states no number.
    """
    import re as _re

    with_numbers, without_numbers = _editorial_examples()
    assert _re.search(r"\d", with_numbers["body_1_en"]), "example 1 carries the number case"
    assert not _re.search(r"\d", without_numbers["body_1_en"]), (
        "example 2 must show a body_1_en with no invented number"
    )


def test_translation_prompt_has_no_anchor_rules():
    from apps.digest.llm import TRANSLATION_PROMPT

    assert "link_anchor" not in TRANSLATION_PROMPT
    assert "Link Anchor Translation" not in TRANSLATION_PROMPT


def test_translation_prompt_does_not_force_empty_fields():
    """v3 needs body_2_uz; v2 hard-coded it to an empty string."""
    from apps.digest.llm import TRANSLATION_PROMPT

    assert 'body_2_uz: ""' not in TRANSLATION_PROMPT


def test_translation_prompt_examples_are_complete():
    import json as _json
    import re as _re

    from apps.digest.llm import TRANSLATION_PROMPT

    filled = TRANSLATION_PROMPT.format(fields="{}")
    blocks = _re.findall(r"Chiquvchi JSON:\s*(\{.*?\n\})", filled, _re.DOTALL)
    assert len(blocks) == 2, "both few-shot examples must be parseable"
    for raw in blocks:
        example = _json.loads(raw)
        for key in ("headline_uz", "lead_uz", "body_1_uz", "kicker_uz"):
            assert example.get(key), f"{key} missing or empty in a few-shot example"
        assert "link_anchor_uz" not in example
        assert "body_2_uz" not in example, "body_2 was removed from the contract"


def test_editorial_schema_requires_headline_and_kicker():
    """The reference style carries a headline label and a closing sentence in every post."""
    from apps.digest.llm import EDITORIAL_EN_SCHEMA

    props = EDITORIAL_EN_SCHEMA["properties"]
    assert props["headline_en"] == {"type": "string"}
    assert props["kicker_en"] == {"type": "string"}
    assert set(EDITORIAL_EN_SCHEMA["required"]) == {
        "headline_en",
        "lead_en",
        "body_1_en",
        "kicker_en",
    }


def test_editorial_prompt_states_both_new_fields_and_their_word_cap():
    """A field the schema requires but the prompt never names is filled with an empty string."""
    from apps.digest.llm import EDITORIAL_EN_PROMPT

    assert "headline_en" in EDITORIAL_EN_PROMPT
    assert "kicker_en" in EDITORIAL_EN_PROMPT
    # Both are capped at 8 words; the cap must be stated, not implied.
    assert EDITORIAL_EN_PROMPT.count("8 words") >= 2


def test_common_translated_fields_carry_headline_and_kicker():
    """translation_schema_for derives _uz keys from this tuple, so membership is the switch."""
    from apps.digest.llm import COMMON_TRANSLATED_FIELDS

    assert "headline_en" in COMMON_TRANSLATED_FIELDS
    assert "kicker_en" in COMMON_TRANSLATED_FIELDS


def test_translation_schema_for_yields_headline_and_kicker():
    from apps.digest.llm import translation_schema_for

    schema = translation_schema_for({"headline_en": "x", "kicker_en": "y", "lead_en": "z"})
    assert set(schema["properties"]) == {"headline_uz", "kicker_uz", "lead_uz"}
    assert set(schema["required"]) == {"headline_uz", "kicker_uz", "lead_uz"}


def test_translation_prompt_no_longer_forbids_a_closing_sentence():
    """The prompt banned the kicker outright. The owner reversed that on 2026-08-24."""
    from apps.digest.llm import TRANSLATION_PROMPT

    assert "yakuniy izoh yoki xulosa jumlasi bilan tugatma" not in TRANSLATION_PROMPT
    assert "kicker_uz" in TRANSLATION_PROMPT
    assert "headline_uz" in TRANSLATION_PROMPT
