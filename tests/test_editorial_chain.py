"""The two-layer editorial (2026-09-09): a Russian draft, then the post said in Uzbek.

Behind EDITORIAL_DRAFT_LANG, default `uz`. The provider is mocked over HTTP in the
gateway's shape so the chain's two calls, their prompts and the stored row are visible.
"""

import json

import httpx
import pytest
import respx

from apps.digest import llm
from apps.digest.models import Analysis, Article, Source

pytestmark = pytest.mark.django_db

GW = "http://gw.test/v1/chat/completions"

RU_DRAFT = {
    "lead_uz": "Смотрим погоду точнее: Google выпустила модель WeatherNext 3.",
    "body_1_uz": "По словам Google, прогноз стал примерно в пять раз чётче.",
    "kicker_uz": "Уже работает в Google Картах.",
    "evidence_level": "vendor_claim_only",
    "technical": {"repo_url": "https://example.com/weathernext", "local_deployable": ""},
}
UZ_SAID = {
    "lead_uz": "Ob-havoni aniqroq ko'ramiz: Google WeatherNext 3 modelini chiqardi.",
    "body_1_uz": "Google aytishicha, prognoz taxminan besh barobar aniqroq bo'ldi.",
    "kicker_uz": "Google Xaritalarda allaqachon ishlayapti.",
}
UZ_ONE_CALL = {**UZ_SAID, "evidence_level": "vendor_claim_only", "technical": {}}


@pytest.fixture
def article(db):
    source = Source.objects.create(
        name="deepmind", connector=Source.Connector.RSS, url="https://dm.test/rss", priority=80
    )
    art = Article.objects.create(
        source=source,
        canonical_url="https://dm.test/weathernext-3",
        content_hash="chain_h1",
        title="Introducing WeatherNext 3",
        extracted_text="WeatherNext 3 makes forecasts roughly five times sharper. " * 30,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="smart",
        payload={"primary_topic": "frontier_models", "maturity": "live_product"},
        latency_ms=100,
    )
    return art


def _gateway(settings, lang="ru"):
    settings.EDITORIAL_UZ_PROVIDER = "gateway"
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-test"
    settings.EDITORIAL_DRAFT_LANG = lang


def _answer(payload, prompt_tokens=10, completion_tokens=5):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


def _capture(route, *payloads):
    """Answer the calls in order and keep every request body."""
    sent = []
    answers = list(payloads)

    def respond(request):
        sent.append(json.loads(request.content))
        return _answer(answers.pop(0)) if answers else _answer(payloads[-1])

    route.mock(side_effect=respond)
    return sent


def test_the_default_is_the_one_call_uzbek_path(settings):
    """`uz` is the shipped behaviour; the chain is opt-in until judged on live days."""
    import environ

    assert environ.Env()("EDITORIAL_DRAFT_LANG", default="uz") == "uz"
    assert settings.EDITORIAL_DRAFT_LANG in ("uz", "ru")


@respx.mock
def test_write_post_routes_uz_to_the_one_call(article, settings):
    _gateway(settings, lang="uz")
    sent = _capture(respx.post(GW), UZ_ONE_CALL)

    result = llm.write_post(article)

    assert len(sent) == 1
    prompt = sent[0]["messages"][0]["content"]
    assert "## Avval: nimasi qiziq?" in prompt
    assert "<recent_leads>" not in prompt, "no old posts in the context, either path"
    assert "draft_ru" not in result.payload


@respx.mock
def test_ru_drafts_in_russian_then_says_it_in_uzbek(article, settings):
    """Two calls. The first is the localised production prompt for the story's block; the
    second is the plain re-expression prompt carrying the draft, not the article."""
    _gateway(settings)
    sent = _capture(respx.post(GW), RU_DRAFT, UZ_SAID)

    result = llm.write_post(article)

    assert len(sent) == 2
    draft_prompt, said_prompt = (s["messages"][0]["content"] for s in sent)
    assert llm.RU_BLOCKS["release"].strip().splitlines()[0] in draft_prompt
    assert llm.RU_EXAMPLES["release"] in draft_prompt
    assert "<recent_leads>" not in draft_prompt, "no old posts in the context"
    assert RU_DRAFT["lead_uz"] in said_prompt and "fifth-grader" in said_prompt
    assert "sun'iy intellekt" in said_prompt, "the glossary rides with the second layer"
    assert article.extracted_text[:40] not in said_prompt, "the second layer never sees the article"

    p = result.payload
    assert p["lead_uz"] == UZ_SAID["lead_uz"] and p["kicker_uz"] == UZ_SAID["kicker_uz"]
    assert p["technical"]["repo_url"] == RU_DRAFT["technical"]["repo_url"]
    assert p["draft_ru"] == {k: RU_DRAFT[k] for k in llm.READER_FIELDS}
    assert p["post_style"] == "plain_photo_v1"
    assert (result.input_tokens, result.output_tokens) == (20, 10), "both calls are accounted"


@respx.mock
def test_the_second_layer_samples_cool(article, settings):
    """The draft is sampled at EDITORIAL_TEMPERATURE; the rewrite at REEXPRESS_TEMPERATURE."""
    _gateway(settings)
    settings.EDITORIAL_TEMPERATURE = 1.0
    sent = _capture(respx.post(GW), RU_DRAFT, UZ_SAID)

    llm.write_post(article)

    assert [s["temperature"] for s in sent] == [1.0, llm.REEXPRESS_TEMPERATURE]


@respx.mock
def test_a_gate_failure_redoes_the_cheap_layer_not_the_draft(article, settings):
    """The gates are deterministic and judge the Uzbek text; the draft's facts stand."""
    _gateway(settings)
    settings.PUBLISHING_ENABLED = False
    bad = {**UZ_SAID, "body_1_uz": "Google aytishicha, prognoz 30 barobar aniqroq bo'ldi."}
    sent = _capture(respx.post(GW), RU_DRAFT, bad, UZ_SAID)

    rows = llm.analyse_for_digest_logic([article.id])

    assert len(sent) == 3, "draft, failed rewrite, retried rewrite"
    retry_prompt = sent[2]["messages"][0]["content"]
    assert "IMPORTANT: your previous answer failed these checks" in retry_prompt
    assert "Number not in the article: 30" in retry_prompt
    assert RU_DRAFT["lead_uz"] in retry_prompt, "the retry is a re-expression of the draft"
    assert article.extracted_text[:40] not in retry_prompt
    assert len(rows) == 1 and rows[0].payload["lead_uz"] == UZ_SAID["lead_uz"]
    assert rows[0].payload["draft_ru"]["lead_uz"] == RU_DRAFT["lead_uz"]
    assert rows[0].input_tokens == 30, "three calls, all accounted"


def test_the_russian_prompt_mirrors_the_uzbek_one_and_its_examples_render():
    """Same seven blocks, one example each, and every example passes the real renderer."""
    import re

    from apps.digest import post_format
    from apps.digest.editorial_prompts import UZ_BLOCKS
    from apps.digest.editorial_prompts_ru import (
        EDITORIAL_RU_PROMPT,
        REEXPRESS_RU_UZ_PROMPT,
        RU_BLOCKS,
        RU_EXAMPLES,
    )

    assert set(RU_BLOCKS) == set(UZ_BLOCKS) == set(RU_EXAMPLES)
    filled = EDITORIAL_RU_PROMPT.format(block="B", example="E", title="T", source="S", text="X")
    assert all(mark in filled for mark in ("B", "E", "T", "S", "X"))
    assert "<recent_leads>" not in EDITORIAL_RU_PROMPT and "naebnet" not in EDITORIAL_RU_PROMPT
    for chunk in RU_EXAMPLES.values():
        fields = {}
        for name in ("lead_uz", "body_1_uz", "kicker_uz"):
            found = re.search(rf"^{name}: (.*?)(?=^[a-z_0-9]+_uz: |\Z)", chunk, re.S | re.M)
            raw = found.group(1).strip() if found else ""
            fields[name] = raw if name == "body_1_uz" else " ".join(raw.split())
        post_format.render_dayjest_post(
            {
                **fields,
                "post_style": post_format.PLAIN_PHOTO_STYLE,
                "url": "https://example.com/news",
                "article_text": "x",
                "topic": "ai_agents",
            }
        )  # raises ValueError if an example teaches a post the renderer would discard
    said = REEXPRESS_RU_UZ_PROMPT.format(lead="L", body="B", kicker="K")
    assert "Ob-havoni aniqroq ko'ramiz:" in said, "the RU->UZ pair that keeps the colon formula"
    assert "at most 7 sentences" in said and "«vosita», not «qurol»" in said
