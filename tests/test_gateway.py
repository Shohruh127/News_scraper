"""Tests for the internal LLM gateway provider.

The gateway speaks the same OpenAI Chat Completions protocol as MiMo but reaches local
GPU models through aliases. These tests need no database: every call is HTTP.
"""

import json

import httpx
import pytest
import respx

from apps.digest import llm

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


@pytest.fixture
def gateway(settings):
    settings.GATEWAY_BASE_URL = "http://gw.test/v1"
    settings.GATEWAY_TOKEN = "sk-gw-test"
    settings.GATEWAY_FAST_MODEL = "fast"
    settings.GATEWAY_SMART_MODEL = "smart"
    settings.GATEWAY_TIMEOUT = 300
    settings.OLLAMA_FAST_MODEL = "gemma4:latest"
    settings.OLLAMA_DEEP_MODEL = "gemma4:31b"
    return settings


def _ok(payload=None):
    body = json.dumps(payload if payload is not None else {"ok": True})
    return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})


def _fenced(payload, language="json"):
    """Reply wrapped in a markdown code fence, the way the live gateway answers."""
    body = "```" + language + "\n" + json.dumps(payload) + "\n```"
    return httpx.Response(200, json={"choices": [{"message": {"content": body}}]})


@respx.mock
def test_gateway_chat_parses_a_markdown_fenced_reply(gateway):
    """The gateway does not enforce json_schema strictly; it fences the JSON.

    Measured 2026-08-21 against the live gateway: every reply came back inside a json
    fence whatever max_tokens was. json.loads on that raises "Expecting value: line 1
    column 1", which cost a doubled call in the stages that retry and dropped the article
    outright in the stages that do not.
    """
    respx.post("http://gw.test/v1/chat/completions").mock(side_effect=[_fenced({"ok": True})])

    payload, _ = llm.gateway_chat(model="fast", prompt="hi", schema=SCHEMA)

    assert payload == {"ok": True}


@respx.mock
def test_gateway_chat_parses_a_fence_with_no_language(gateway):
    respx.post("http://gw.test/v1/chat/completions").mock(
        side_effect=[_fenced({"ok": False}, language="")]
    )

    payload, _ = llm.gateway_chat(model="fast", prompt="hi", schema=SCHEMA)

    assert payload == {"ok": False}


@respx.mock
def test_an_empty_reply_names_the_token_budget(gateway):
    """`smart` is a reasoning model and its reasoning is charged to max_tokens.

    Measured 2026-08-21: at max_tokens=50 it returned finish_reason "length" with empty
    content, having spent the whole budget on its `reasoning` field. Parsing that raised
    JSONDecodeError, which points at neither the cause nor the fix.
    """
    respx.post("http://gw.test/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
            )
        ]
    )

    with pytest.raises(RuntimeError, match="finish_reason"):
        llm.gateway_chat(model="smart", prompt="hi", schema=SCHEMA, max_tokens=50)


@respx.mock
def test_gateway_chat_still_parses_an_unfenced_reply(gateway):
    """Stripping the fence must not break the providers that answer with bare JSON."""
    respx.post("http://gw.test/v1/chat/completions").mock(side_effect=[_ok({"ok": True})])

    payload, _ = llm.gateway_chat(model="fast", prompt="hi", schema=SCHEMA)

    assert payload == {"ok": True}


@respx.mock
def test_gateway_chat_sends_bearer_token_and_alias(gateway):
    route = respx.post("http://gw.test/v1/chat/completions").mock(side_effect=[_ok()])

    payload, latency_ms = llm.gateway_chat(model="fast", prompt="hi", schema=SCHEMA)

    assert payload == {"ok": True}
    assert latency_ms >= 0
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer sk-gw-test"
    sent = json.loads(request.content)
    assert sent["model"] == "fast", "the alias goes on the wire, never a real model name"


@respx.mock
def test_gateway_chat_requests_strict_json_schema(gateway):
    route = respx.post("http://gw.test/v1/chat/completions").mock(side_effect=[_ok()])

    llm.gateway_chat(model="smart", prompt="hi", schema=SCHEMA)

    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["response_format"]["json_schema"]["schema"] == SCHEMA


def test_gateway_chat_refuses_to_run_unconfigured(settings):
    settings.GATEWAY_BASE_URL = ""
    settings.GATEWAY_TOKEN = ""

    with pytest.raises(RuntimeError, match="GATEWAY_BASE_URL"):
        llm.gateway_chat(model="fast", prompt="hi", schema=SCHEMA)


@respx.mock
def test_classifier_chat_maps_fast_model_to_fast_alias(gateway):
    gateway.CLASSIFIER_PROVIDER = "gateway"
    route = respx.post("http://gw.test/v1/chat/completions").mock(side_effect=[_ok()])

    payload, _, model_tag = llm.classifier_chat(
        model="gemma4:latest", prompt="hi", schema=SCHEMA, timeout=10, num_predict=400
    )

    assert payload == {"ok": True}
    assert model_tag == "fast"
    assert json.loads(route.calls[0].request.content)["model"] == "fast"


@respx.mock
def test_classifier_chat_maps_deep_model_to_smart_alias(gateway):
    gateway.CLASSIFIER_PROVIDER = "gateway"
    route = respx.post("http://gw.test/v1/chat/completions").mock(side_effect=[_ok()])

    _, _, model_tag = llm.classifier_chat(
        model="gemma4:31b", prompt="hi", schema=SCHEMA, timeout=10, num_predict=400
    )

    assert model_tag == "smart"
    assert json.loads(route.calls[0].request.content)["model"] == "smart"


@respx.mock
def test_classifier_chat_defaults_to_ollama(gateway):
    """Triage and classification stay on Ollama until CLASSIFIER_PROVIDER says otherwise."""
    gateway.CLASSIFIER_PROVIDER = "ollama"
    gateway.OLLAMA_BASE_URL = "http://ollama.test"
    route = respx.post("http://ollama.test/api/chat").mock(
        side_effect=[httpx.Response(200, json={"message": {"content": '{"ok": true}'}})]
    )

    _, _, model_tag = llm.classifier_chat(
        model="gemma4:31b", prompt="hi", schema=SCHEMA, timeout=10, num_predict=400
    )

    assert route.called
    assert model_tag == "gemma4:31b", "Ollama records the real tag, not an alias"


@respx.mock
def test_classifier_chat_can_run_on_mimo(gateway):
    """All four stages must be able to sit on one provider, MiMo included."""
    gateway.CLASSIFIER_PROVIDER = "mimo"
    gateway.MIMO_BASE_URL = "https://mimo.test/v1"
    gateway.MIMO_API_KEY = "k"
    gateway.MIMO_FAST_MODEL = "mimo-v2.5"
    gateway.MIMO_DEEP_MODEL = "mimo-v2.5-pro"
    route = respx.post("https://mimo.test/v1/chat/completions").mock(side_effect=[_ok()])

    _, _, model_tag = llm.classifier_chat(
        model="gemma4:31b", prompt="hi", schema=SCHEMA, timeout=10, num_predict=400
    )

    assert route.called
    assert model_tag == "mimo-v2.5-pro"


@respx.mock
def test_editorial_chat_routes_translation_to_the_fast_alias(gateway):
    """Translation belongs on the fast model; that measured decision must survive routing."""
    route = respx.post("http://gw.test/v1/chat/completions").mock(side_effect=[_ok()])

    _, _, model_tag = llm.editorial_chat(
        prompt="hi",
        schema=SCHEMA,
        num_predict=800,
        provider="gateway",
        ollama_model="gemma4:latest",
    )

    assert model_tag == "fast"
    assert json.loads(route.calls[0].request.content)["model"] == "fast"


@respx.mock
def test_editorial_chat_defaults_to_the_smart_alias(gateway):
    route = respx.post("http://gw.test/v1/chat/completions").mock(side_effect=[_ok()])

    _, _, model_tag = llm.editorial_chat(
        prompt="hi", schema=SCHEMA, num_predict=800, provider="gateway"
    )

    assert model_tag == "smart"
    assert json.loads(route.calls[0].request.content)["model"] == "smart"
