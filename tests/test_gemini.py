"""Tests for the Gemini provider (B2, F4/OPEN-4: model gemini-3.8-flash).

Gemini speaks `generateContent`, not OpenAI chat completions, so it has its own
adapter. These tests need no database and no API key: every call is HTTP via respx.
"""

import json

import httpx
import pytest
import respx

from apps.digest import llm

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
URL = "https://gem.test/v1beta/models/gemini-3.8-flash:generateContent"


@pytest.fixture
def gemini(settings):
    settings.GEMINI_BASE_URL = "https://gem.test"
    settings.GEMINI_API_KEY = "k-test"
    settings.GEMINI_MODEL = "gemini-3.8-flash"
    settings.GEMINI_TIMEOUT = 300
    settings.GEMINI_THINKING_LEVEL = "medium"
    return settings


def _ok(payload=None, prompt_tokens=10, candidates_tokens=5, thoughts_tokens=3):
    body = json.dumps(payload if payload is not None else {"ok": True})
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": body}]}, "finishReason": "STOP"},
            ],
            "usageMetadata": {
                "promptTokenCount": prompt_tokens,
                "candidatesTokenCount": candidates_tokens,
                "thoughtsTokenCount": thoughts_tokens,
            },
        },
    )


@respx.mock
def test_gemini_chat_parses_json_and_reports_usage(gemini):
    respx.post(URL).mock(side_effect=[_ok({"ok": True})])

    result = llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)

    assert result.payload == {"ok": True}
    assert result.model_tag == "gemini-3.8-flash"
    assert result.input_tokens == 10
    # Thinking tokens bill as output, so both buckets count.
    assert result.output_tokens == 8


@respx.mock
def test_gemini_chat_sends_key_in_header_and_model_in_url(gemini):
    route = respx.post(URL).mock(side_effect=[_ok()])

    llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)

    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == "k-test"
    assert "k-test" not in str(request.url), "the key must not travel in the URL"
    sent = json.loads(request.content)
    assert sent["contents"][0]["parts"][0]["text"] == "hi"
    assert sent["generationConfig"]["responseMimeType"] == "application/json"


@respx.mock
def test_gemini_chat_converts_schema_to_gemini_dialect(gemini):
    route = respx.post(URL).mock(side_effect=[_ok()])

    llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)

    sent = json.loads(route.calls[0].request.content)
    schema = sent["generationConfig"]["responseSchema"]
    assert schema["type"] == "OBJECT"
    assert schema["properties"]["ok"]["type"] == "BOOLEAN"
    assert schema["required"] == ["ok"]


@respx.mock
def test_dayjest_schema_is_accepted_by_generate_content(gemini):
    """Replay the 400 discovered with the real dayjest link-array schema."""
    payload = {
        "lead_uz": "Yangi vosita chiqdi.",
        "body_1_uz": "",
        "kicker_uz": "",
    }

    def respond(request):
        schema = json.loads(request.content)["generationConfig"]["responseSchema"]
        if "additionalProperties" in json.dumps(schema):
            return httpx.Response(
                400,
                json={
                    "error": {"message": 'Unknown name "additionalProperties" in response_schema'}
                },
            )
        return _ok(payload)

    route = respx.post(URL).mock(side_effect=respond)
    result = llm.gemini_chat(
        model=gemini.GEMINI_MODEL, prompt="Write a dayjest.", schema=llm.EDITORIAL_UZ_SCHEMA
    )
    assert llm.EditorialUz.model_validate(result.payload).lead_uz
    assert route.call_count == 1


@respx.mock
def test_gemini_chat_retries_a_429_then_reports_both_calls(gemini):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, text="quota exhausted"),
            _ok({"ok": True}, prompt_tokens=10, candidates_tokens=5, thoughts_tokens=0),
        ]
    )

    result = llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)

    assert result.payload == {"ok": True}
    assert len(route.calls) == 2
    assert result.input_tokens == 10


@respx.mock
def test_gemini_chat_names_a_bad_key(gemini):
    respx.post(URL).mock(side_effect=[httpx.Response(403, text="API key not valid")])

    with pytest.raises(RuntimeError, match="API key"):
        llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)


@respx.mock
def test_gemini_chat_names_an_unknown_model(gemini):
    respx.post(URL).mock(side_effect=[httpx.Response(404, text="not found")])

    with pytest.raises(RuntimeError, match="no model"):
        llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)


@respx.mock
def test_gemini_chat_refuses_a_safety_block(gemini):
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": []}, "finishReason": "SAFETY"},
                    ],
                },
            )
        ]
    )

    with pytest.raises(RuntimeError, match="Safety block"):
        llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)


@respx.mock
def test_gemini_chat_names_the_budget_on_truncation(gemini):
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": ""}]}, "finishReason": "MAX_TOKENS"},
                    ],
                },
            )
        ]
    )

    with pytest.raises(RuntimeError, match="maxOutputTokens"):
        llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA, max_tokens=50)


@respx.mock
def test_gemini_chat_records_none_when_usage_is_missing(gemini):
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"parts": [{"text": '{"ok": true}'}]},
                            "finishReason": "STOP",
                        },
                    ],
                },
            )
        ]
    )

    result = llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)

    assert result.payload == {"ok": True}
    assert result.input_tokens is None
    assert result.output_tokens is None


def test_gemini_chat_refuses_to_run_without_a_key(gemini):
    gemini.GEMINI_API_KEY = ""

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        llm.gemini_chat(model="gemini-3.8-flash", prompt="hi", schema=SCHEMA)


@respx.mock
def test_dispatch_routes_gemini_through_gemini_chat(gemini):
    route = respx.post(URL).mock(side_effect=[_ok()])

    result = llm._dispatch(
        provider="gemini",
        tier=llm.TIER_DEEP,
        prompt="hi",
        schema=SCHEMA,
        num_predict=5000,
    )

    assert route.called
    assert result.model_tag == "gemini-3.8-flash"


@respx.mock
def test_editorial_can_run_on_gemini_without_moving_the_classifier(gemini):
    gemini.LLM_PROVIDER = "gateway"
    gemini.CLASSIFIER_PROVIDER = "gateway"
    gemini.EDITORIAL_UZ_PROVIDER = "gemini"
    route = respx.post(URL).mock(side_effect=[_ok()])

    result = llm.editorial_chat(
        prompt="hi",
        schema=SCHEMA,
        num_predict=5000,
        provider=gemini.EDITORIAL_UZ_PROVIDER,
    )

    assert route.called
    assert result.model_tag == "gemini-3.8-flash"
    assert gemini.CLASSIFIER_PROVIDER == "gateway"


@respx.mock
def test_the_editorial_samples_at_the_configured_temperature_and_triage_does_not(gemini):
    """Decisions are deterministic; the post is not.

    Every provider call was hardcoded to temperature 0 until 2026-09-08. At 0 the model
    returns its single most probable continuation, which with examples in the prompt is
    the shape of the examples: seven of the last eight published leads read "<Kompaniya>
    <narsa>ni chiqardi". The editorial now sends EDITORIAL_TEMPERATURE (default 1.0, the
    model's own default per the Gemini API); triage and classification still send 0.
    """
    gemini.EDITORIAL_UZ_PROVIDER = "gemini"
    gemini.CLASSIFIER_PROVIDER = "gemini"
    gemini.EDITORIAL_TEMPERATURE = 0.8

    sent = []

    def capture(request):
        sent.append(json.loads(request.content)["generationConfig"]["temperature"])
        return _ok()

    respx.post(URL).mock(side_effect=capture)

    # provider= is passed explicitly, as editorial_uz_for_article passes it; the bare
    # fallback is LLM_PROVIDER, which is not what routes the editorial in production.
    llm.editorial_chat(prompt="p", schema=SCHEMA, num_predict=100, provider="gemini")
    llm.classifier_chat(tier=llm.TIER_FAST, prompt="p", schema=SCHEMA, num_predict=100)

    assert sent == [0.8, 0], "editorial at the setting, classifier pinned at 0"


@respx.mock
def test_the_thinking_level_can_be_set_per_call(gemini):
    """The second editorial layer thinks at `low`: its facts are already chosen. Until
    2026-09-09 the level was global, so a cheap rewrite thought as hard as the draft."""
    sent = []

    def capture(request):
        sent.append(json.loads(request.content)["generationConfig"]["thinkingConfig"])
        return _ok()

    respx.post(URL).mock(side_effect=capture)

    llm.gemini_chat(model="gemini-3.8-flash", prompt="p", schema=SCHEMA)
    llm.gemini_chat(model="gemini-3.8-flash", prompt="p", schema=SCHEMA, thinking_level="low")

    assert sent == [{"thinkingLevel": "medium"}, {"thinkingLevel": "low"}]
