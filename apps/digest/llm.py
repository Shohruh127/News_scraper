"""LLM integration: provider clients, Pydantic schemas, prompt constants, and classification.

Rules:
1. Functions over classes.
2. One file for all LLM logic.
3. num_predict is mandatory on EVERY call.
4. Retry with tenacity on timeouts and 5xx only.
5. Pydantic validation failure retries once with error appended, then sets status='skipped'.
"""

import json
import logging
import time
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx
from django.conf import settings
from pydantic import BaseModel, Field, ValidationError, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import artifacts, post_format, translation_gates
from .editorial_prompts import EDITORIAL_UZ_PROMPT, UZ_BLOCKS, UZ_EXAMPLES
from .editorial_prompts_ru import (
    EDITORIAL_RU_PROMPT,
    REEXPRESS_RU_UZ_PROMPT,
    RU_BLOCKS,
    RU_EXAMPLES,
)
from .models import (
    EXCLUDED_MATURITIES,
    Analysis,
    Article,
    Maturity,
    Topic,
)

log = logging.getLogger(__name__)

# Known domain blocklist for rule-based pre-filter
BLOCKLISTED_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "twitter.com",
    "x.com",
}


class RetryableLLMError(Exception):
    """A 5xx or 429 from a provider. Worth retrying; client errors (4xx) are not."""


RETRYABLE_LLM_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    RetryableLLMError,
)

INFRASTRUCTURE_EXCEPTIONS = (
    httpx.HTTPError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    RetryableLLMError,
)


class Classification(BaseModel):
    """Matches CONTENT_SCHEMA.md §4 exactly."""

    primary_topic: Topic
    maturity: Maturity
    novelty: int = Field(..., ge=1, le=10)
    evidence: int = Field(..., ge=1, le=10)
    production_readiness: int = Field(..., ge=1, le=10)
    reason: str


CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "primary_topic": {
            "type": "string",
            "enum": [t.value for t in Topic],
        },
        "maturity": {
            "type": "string",
            "enum": [m.value for m in Maturity],
        },
        "novelty": {"type": "integer", "minimum": 1, "maximum": 10},
        "evidence": {"type": "integer", "minimum": 1, "maximum": 10},
        "production_readiness": {"type": "integer", "minimum": 1, "maximum": 10},
        "reason": {"type": "string"},
    },
    "required": [
        "primary_topic",
        "maturity",
        "novelty",
        "evidence",
        "production_readiness",
        "reason",
    ],
}


class TechnicalDetails(BaseModel):
    what_was_built: str = ""
    architecture: str = ""
    license: str = ""
    repo_url: str = ""
    api_url: str = ""
    install: str = ""
    benchmarks: str = ""
    limitations: str = ""
    local_deployable: bool = False

    @field_validator("local_deployable", mode="before")
    @classmethod
    def _blank_means_not_local(cls, value: Any) -> Any:
        """An empty string here means the article did not say, which is `False`.

        Measured 2026-08-26: MiMo returned `''` for this field on the same article in two
        consecutive runs, and each time the ValidationError cost a second editorial call —
        5346 input tokens for that article instead of 2682. The prompt had asked for it:
        the `technical` instruction said to return an empty string for anything the article
        omits, and that sentence covered the boolean too. The prompt now exempts this field;
        this validator keeps a model that answers the old way from costing the retry.

        Only blank is coerced. Pydantic already reads 'true'/'false'/'1'/'0', and anything
        else is still a real error worth retrying.
        """
        if isinstance(value, str) and not value.strip():
            return False
        return value


class EditorialUz(BaseModel):
    """The published post, written in one call (2026-08-26 design).

    Replaces the EditorialEn -> Translation pair. The reader-facing fields are Uzbek; the
    `technical` block stays English because it is copied verbatim from the article and
    published as live links.
    """

    lead_uz: str = ""
    body_1_uz: str = ""
    kicker_uz: str = ""

    technical: TechnicalDetails = Field(default_factory=TechnicalDetails)
    evidence_level: str = Field(default="vendor_claim_only")


class ReexpressUz(BaseModel):
    """The second layer's answer: the Russian draft said in Uzbek. Three fields, nothing else."""

    lead_uz: str = ""
    body_1_uz: str = ""
    kicker_uz: str = ""


REEXPRESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {f: {"type": "string"} for f in ("lead_uz", "body_1_uz", "kicker_uz")},
    "required": ["lead_uz", "body_1_uz", "kicker_uz"],
}

#: The re-expression is a faithful rewrite, not a composition: sampled a little for natural
#: wording, not enough to drift, and thinking at `low` because the facts are already chosen.
#: Measured 2026-09-09 on 26 articles: 0.8-2.2k tokens a post, no gate failures; one post's
#: thinking exhausted a 4000 cap at level medium, hence 8000.
REEXPRESS_TEMPERATURE = 0.4
REEXPRESS_NUM_PREDICT = 8000


#: Topic guidance varies; the shared dayjest format and voice live in editorial_prompts.
SHAPE_GENERAL = "general"

#: Topic -> shape. `irrelevant` is absent because it never reaches the editorial stage:
#: classification drops it. Verified complete by test_every_topic_maps_to_a_shape.
TOPIC_SHAPES: dict[str, str] = {
    Topic.FRONTIER_MODELS.value: "release",
    Topic.PRODUCTION_ENGINEERING.value: "release",
    Topic.SPEECH_VOICE.value: "release",
    Topic.AI_AGENTS.value: "agent",
    Topic.SAFETY_SECURITY.value: "risk",
    Topic.NEW_APPROACHES.value: "research",
    Topic.TECHNICAL_TALKS.value: "research",
    Topic.STARTUPS.value: "product",
    Topic.FINTECH.value: "product",
    Topic.GOVTECH.value: "product",
    Topic.ROBOTICS.value: "robotics",
}


def shape_for(topic: str | None) -> str:
    """The instruction group for a classified topic.

    An unmapped topic gets the general block rather than raising, so adding a Topic later
    degrades to today's behaviour instead of dropping every article in that category.
    """
    return TOPIC_SHAPES.get(topic or "", SHAPE_GENERAL)


EDITORIAL_UZ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lead_uz": {"type": "string"},
        "body_1_uz": {"type": "string"},
        "kicker_uz": {"type": "string"},
        "evidence_level": {
            "type": "string",
            "enum": ["vendor_claim_only", "multiple_evidence"],
        },
        "technical": {
            "type": "object",
            "properties": {
                "what_was_built": {"type": "string"},
                "architecture": {"type": "string"},
                "license": {"type": "string"},
                "repo_url": {"type": "string"},
                "api_url": {"type": "string"},
                "install": {"type": "string"},
                "benchmarks": {"type": "string"},
                "limitations": {"type": "string"},
                "local_deployable": {"type": "boolean"},
            },
        },
    },
    "required": ["lead_uz", "body_1_uz", "kicker_uz"],
}


#: Triage asks one question and returns one answer. It used to request the full
#: CLASSIFICATION_SCHEMA — topic, maturity and three 1-10 scores — from an 8000-character
#: article, and then decided on `primary_topic == irrelevant or all three scores < 3`.
#:
#: Measured 2026-08-25 on the 26-row gold set: that gate rejected 3 of 26 articles at
#: recall 1.00, for ~2134 input tokens each. The scores it asked for cannot be derived from
#: a headline and were never the deciding signal anyway; the topic was.
TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["relevant", "reason"],
}

#: Title and source only. This is the whole cost saving: the article body is already
#: downloaded and stored, but sending it to the model costs ~2000 input tokens per article
#: on the highest-volume stage in the pipeline, several hundred times a day.
TRIAGE_PROMPT_TEMPLATE = """You are the first filter for an AI-engineering news digest read
by working engineers. Return JSON only.

Answer relevant=true if ANY ONE of these holds. They are independent — one is enough.

  A. The headline names a specific model, tool, library, protocol, API, dataset or product.
  B. It reports something shipped, released, opened, updated, deprecated or priced.
  C. It reports an operational or engineering action taken with real systems — disrupting,
     detecting, mitigating, hardening, migrating, scaling — even when nothing is named.
  D. It reports a concrete technical finding, benchmark or measurement.

Answer relevant=false only when none of A-D holds and the headline is the business around
the work: funding, acquisitions, hiring, events, partnerships, policy, opinion, marketing,
consumer gadgets.

On rule A, do not judge how the headline relates to the named thing — a question about it,
a complaint about it, or a problem with it all count. That judgement
belongs to the classification stage, which reads the article.

If one of A-D holds but you cannot tell how significant it is, keep it: one extra costs
one call, a dropped release is lost for good.

reason: at most 10 words, naming what decided it.

Title: {title}
Source: {source}
"""


# Verbatim enum definitions and boundaries from CONTENT_SCHEMA.md §2 and §3 for deep classification
CLASSIFICATION_PROMPT_TEMPLATE = (
    "You are a technical editor for an AI-engineering news digest read by engineers and "
    "technical decision-makers. Classify the article below. Return JSON only.\n\n"
    "## primary_topic — the SINGLE best fit\n"
    "- frontier_models: a named model released, updated or given new capabilities. Not a "
    "technique — that is new_approaches. Not a tool that runs models — that is "
    "production_engineering.\n"
    "- ai_agents: an LLM acting through tools: agent frameworks, tool calling, MCP/A2A, "
    "multi-agent orchestration, coding or browser agents. Not every paper that says "
    '"agent"; not a tool release that merely supports agents — that is '
    "production_engineering.\n"
    "- new_approaches: a new method, architecture, training or inference technique. The "
    "default for research papers. Not a named model release.\n"
    "- speech_voice: audio as input or output: STT, TTS, voice agents, diarization.\n"
    "- robotics: physical embodiment: robots, control policies, embodied AI.\n"
    "- fintech: payments, banking, lending, financial infrastructure.\n"
    "- govtech: government digital services and public administration.\n"
    "- production_engineering: infrastructure, serving, deployment and developer tooling — "
    "including their releases and changelogs. An Ollama, vLLM or LangGraph changelog "
    "belongs here even when it mentions agents or models.\n"
    "- startups: a company shipping a deployed commercial product. Not any article that "
    "mentions a company; a model release from a large lab is frontier_models.\n"
    "- technical_talks: a recorded presentation: conference talk, demo, technical video.\n"
    "- safety_security: alignment, jailbreaks, model or agent security, permissions, "
    "red-teaming. General research into model behaviour is new_approaches.\n"
    "- irrelevant: everything else: appointments, funding, partnerships, marketing, "
    "opinion, consumer gadgets, general business news.\n\n"
    "No technical substance means primary_topic MUST be irrelevant. Never force a "
    "technical category onto a business story.\n\n"
    "## maturity — what exists right now\n"
    "- production_deployment: running in a named real organisation, with reported "
    'results. Not "could be deployed".\n'
    "- live_product: a publicly usable product or API today. A changelog of an "
    "already-shipped tool is live_product, not production_deployment.\n"
    "- reproducible_open_source: code or weights downloadable today at a working link.\n"
    "- public_pilot: limited preview, waitlist or restricted access.\n"
    "- announcement_only: announced, nothing usable released.\n"
    '- paper_only: a research paper or preprint — even when it says "code will be '
    'released" or links a repository that does not resolve to real artifacts today. '
    "Excellent results do not raise maturity; only shipped artifacts do.\n\n"
    "## Numeric dimensions\n"
    "- novelty: 1 = rehash of known news, 10 = genuinely new capability or result\n"
    "- evidence: 1 = vendor claim only, 10 = reproducible artifacts\n"
    "- production_readiness: 1 = paper or announcement, 10 = deployed and documented\n\n"
    "reason: at most 10 words, naming what decided it. Always include it.\n\n"
    "ARTICLE\n"
    "Title: {title}\n"
    "Source: {source}\n"
    "---\n"
    "{text}\n"
)


#: The two speed tiers every provider offers. The gateway calls its deep tier "smart" and
#: MiMo calls it "deep"; both map from this one name.
#:
#: Until 2026-08-25 the tier was carried as an Ollama model tag: passing "gemma4:latest"
#: meant "fast", and every provider compared against OLLAMA_FAST_MODEL to work out which
#: tier the caller wanted. That made the Ollama settings load-bearing for providers that
#: never spoke to Ollama, which is why CLAUDE.md had to warn they must stay set even when
#: nothing used them. The tier is now said out loud.
TIER_FAST = "fast"
TIER_DEEP = "deep"


class ChatResult(NamedTuple):
    """One provider call, with what it cost.

    A NamedTuple rather than a widening tuple: the return was unpacked positionally at
    fourteen call sites, and adding token counts to that was a rename waiting to go wrong.
    Named fields are also why `input_tokens` cannot be silently confused with `latency_ms`.

    `input_tokens` / `output_tokens` are what the provider reported. None means it sent no
    usage block — deliberately not 0, which would make the call look free.
    """

    payload: dict
    latency_ms: int
    model_tag: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(RETRYABLE_LLM_EXCEPTIONS),
    reraise=True,
)
def _chat_post(
    client: httpx.Client, url: str, payload: dict, headers: dict | None = None
) -> httpx.Response:
    r = client.post(url, json=payload, headers=headers)
    if r.status_code >= 500 or r.status_code == 429:
        raise RetryableLLMError(f"{r.status_code} from {url}: {r.text[:200]}")
    r.raise_for_status()
    return r


def _strip_code_fence(text: str) -> str:
    """Return the JSON inside a markdown fence, or the text unchanged.

    The internal gateway accepts `json_schema` with `strict: true` and then wraps its
    answer in a ```json fence anyway. Measured 2026-08-21 against the live gateway: every
    reply was fenced, at max_tokens 50, 300 and 1000 alike. json.loads fails on that before
    any schema validation runs, which cost a doubled call in the stages that retry and
    dropped the article outright in the stages that do not.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text

    body = stripped[3:]
    first_newline = body.find("\n")
    if first_newline != -1:
        # Drop the language hint on the opening line, if any.
        body = body[first_newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body


def _openai_chat(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    schema: dict | None = None,
    timeout: int = 120,
    max_tokens: int = 1500,
    client: httpx.Client | None = None,
    temperature: float = 0,
) -> ChatResult:
    """Chat completion against any OpenAI-compatible endpoint.

    Uses `json_schema` strict mode, not `json_object`. Measured 2026-08-17 on the
    editorial schema:

      json_object          returned malformed JSON (trailing comma) and, over 7 real
                           articles, conformed to the schema 2/7 times — it invented
                           its own keys (`title`, `article_title`) and dropped required
                           ones.
      json_schema strict   returned exactly the six required keys.

    Ollama enforces the schema in the decoder via XGrammar, so the prompt never had to
    name the fields. That assumption does not carry to an OpenAI-compatible endpoint
    unless strict mode is requested explicitly.
    """
    url = f"{base_url}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "strict": True, "schema": schema},
        }

    close_client = False
    if client is None:
        client = httpx.Client(timeout=timeout)
        close_client = True

    t0 = time.perf_counter()
    try:
        r = _chat_post(
            client,
            url,
            payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        choice = r.json()["choices"][0]
        content = choice["message"]["content"]
        if schema and not (content or "").strip():
            # A reasoning model bills its reasoning to max_tokens before it writes any
            # answer, so too small a budget yields finish_reason "length" and no content.
            # Parsing that raises JSONDecodeError, which names neither cause nor fix.
            raise RuntimeError(
                f"{base_url} returned an empty message for model {model!r} "
                f"(finish_reason={choice.get('finish_reason')!r}). If this is a reasoning "
                "model, max_tokens has to cover its reasoning as well as the answer."
            )
        parsed = json.loads(_strip_code_fence(content)) if schema else {"raw": content}
        # Verified live against the gateway on 2026-08-25: it returns prompt_tokens,
        # completion_tokens and total_tokens. `.get` rather than `[...]` so a provider that
        # omits the block records None instead of failing the call.
        usage = r.json().get("usage") or {}
        return ChatResult(
            payload=parsed,
            latency_ms=latency_ms,
            model_tag=model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
    finally:
        if close_client:
            client.close()


def mimo_chat(
    model: str,
    prompt: str,
    schema: dict | None = None,
    timeout: int = 120,
    max_tokens: int = 1500,
    client: httpx.Client | None = None,
    temperature: float = 0,
) -> ChatResult:
    """OpenAI-compatible chat completion against MiMo."""
    return _openai_chat(
        base_url=settings.MIMO_BASE_URL,
        api_key=settings.MIMO_API_KEY,
        model=model,
        prompt=prompt,
        schema=schema,
        timeout=timeout,
        max_tokens=max_tokens,
        client=client,
        temperature=temperature,
    )


def gateway_chat(
    model: str,
    prompt: str,
    schema: dict | None = None,
    timeout: int | None = None,
    max_tokens: int = 1500,
    client: httpx.Client | None = None,
    temperature: float = 0,
) -> ChatResult:
    """Chat completion against the internal LLM gateway.

    `model` must be a tier alias (`fast`/`smart`), never a real model name — the gateway
    answers 404 model_not_found for real names on purpose, because the alias is what lets
    it repoint a tier at a different model without any caller changing.
    """
    if not settings.GATEWAY_BASE_URL or not settings.GATEWAY_TOKEN:
        raise RuntimeError("GATEWAY_BASE_URL and GATEWAY_TOKEN must be set to use the gateway")
    return _openai_chat(
        base_url=settings.GATEWAY_BASE_URL,
        api_key=settings.GATEWAY_TOKEN,
        model=model,
        prompt=prompt,
        schema=schema,
        timeout=timeout or settings.GATEWAY_TIMEOUT,
        max_tokens=max_tokens,
        temperature=temperature,
        client=client,
    )


_GEMINI_TYPES = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
}


def _to_gemini_schema(schema: Any) -> Any:
    """Convert a lowercase JSON-Schema dict to Gemini's `responseSchema` dialect.

    Same shape, uppercase type names: Gemini's REST API speaks `{"type": "OBJECT",
    "properties": ...}` where the OpenAI-compatible providers take lowercase. Keys it
    does not know (e.g. `strict`, `name`) are dropped rather than sent: an unknown key
    fails the call, and neither carries meaning for Gemini.
    """
    if isinstance(schema, list):
        return [_to_gemini_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("strict", "name"):
            continue
        if key == "type" and isinstance(value, str):
            out[key] = _GEMINI_TYPES.get(value, value)
        elif key == "properties" and isinstance(value, dict):
            out[key] = {name: _to_gemini_schema(sub) for name, sub in value.items()}
        elif key in ("items", "prefixItems", "additionalProperties"):
            out[key] = _to_gemini_schema(value)
        else:
            out[key] = value
    return out


def gemini_chat(
    model: str,
    prompt: str,
    schema: dict | None = None,
    timeout: int | None = None,
    max_tokens: int = 1500,
    client: httpx.Client | None = None,
    temperature: float = 0,
    thinking_level: str | None = None,
) -> ChatResult:
    """One `generateContent` call against the Gemini Developer API (B2).

    Contract matches `_openai_chat`: returns a `ChatResult` with the provider's usage,
    raises `RetryableLLMError` (via `_chat_post`) on 429/5xx, and raises a plain
    `RuntimeError` naming the cause on permanent failures — bad key, unknown model,
    safety block, truncation — so none of them can become a "valid empty post".

    Token mapping: `promptTokenCount` is input; output is `candidatesTokenCount` plus
    `thoughtsTokenCount`, because thinking tokens are billed as output on this API. No
    usage block means None, never 0.
    """
    base_url = (settings.GEMINI_BASE_URL or "").rstrip("/")
    api_key = settings.GEMINI_API_KEY or ""
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY must be set to use the gemini provider")
    # Both are checked, as gateway_chat checks both its URL and its token. A blank base
    # URL builds a protocol-less relative URL, and httpx raises UnsupportedProtocol for
    # it — an httpx.HTTPError, therefore a member of INFRASTRUCTURE_EXCEPTIONS, therefore
    # retried every cycle forever as if the network were down. It is a config error.
    if not base_url:
        raise RuntimeError("GEMINI_BASE_URL must be set to use the gemini provider")
    url = f"{base_url}/v1beta/models/{model}:generateContent"
    body: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingLevel": thinking_level or settings.GEMINI_THINKING_LEVEL},
        },
    }
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = _to_gemini_schema(schema)

    close_client = False
    if client is None:
        client = httpx.Client(timeout=timeout or settings.GEMINI_TIMEOUT)
        close_client = True

    t0 = time.perf_counter()
    try:
        try:
            r = _chat_post(client, url, body, headers={"x-goog-api-key": api_key})
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = (exc.response.text or "")[:200]
            if status in (401, 403):
                raise RuntimeError(
                    f"Gemini rejected the API key ({status}). Check GEMINI_API_KEY. {detail}"
                ) from exc
            if status == 400:
                raise RuntimeError(
                    f"Gemini rejected the request (400) for model {model!r}. Check "
                    f"GEMINI_MODEL and the responseSchema. {detail}"
                ) from exc
            if status == 404:
                raise RuntimeError(
                    f"Gemini has no model {model!r} (404). Check GEMINI_MODEL. {detail}"
                ) from exc
            raise
        latency_ms = int((time.perf_counter() - t0) * 1000)
        data = r.json()

        # Read the cost before anything can raise. Google bills a blocked or truncated
        # call for every thinking token it spent getting there, and each raise below
        # used to discard the figure — the one number that says why the call failed.
        usage = data.get("usageMetadata") or {}
        output_tokens = None
        if "candidatesTokenCount" in usage or "thoughtsTokenCount" in usage:
            output_tokens = (usage.get("candidatesTokenCount") or 0) + (
                usage.get("thoughtsTokenCount") or 0
            )
        cost = f"in={usage.get('promptTokenCount')} out={output_tokens}"

        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "unknown")
            raise RuntimeError(
                f"Gemini returned no candidates (promptFeedback.blockReason={reason}; "
                f"{cost}). The prompt was blocked before generation; retrying it is "
                "pointless."
            )
        first = candidates[0]
        finish = first.get("finishReason")
        # An allowlist, not a denylist. STOP is the only finish reason that means the
        # model wrote the whole answer; MAX_TOKENS, RECITATION and OTHER all return
        # content, and a truncated JSON object that happens to parse is indistinguishable
        # from a complete post once it reaches json.loads. Measured 2026-09-07: all three
        # were accepted as successful posts, one of them cut off mid-sentence.
        if finish and finish != "STOP":
            if finish in ("SAFETY", "PROHIBITED_CONTENT"):
                detail = "Safety block, not a retryable error."
            elif finish == "MAX_TOKENS":
                detail = (
                    "The answer was truncated. Thinking tokens bill to maxOutputTokens, "
                    "so max_tokens has to cover reasoning as well as the answer."
                )
            else:
                detail = "The answer is incomplete; it must not be stored as a post."
            raise RuntimeError(
                f"Gemini did not finish for model {model!r} (finishReason={finish}; "
                f"{cost}). {detail}"
            )
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if schema and not (text or "").strip():
            raise RuntimeError(
                f"Gemini returned an empty message for model {model!r} "
                f"(finishReason={finish!r}; {cost}). Thinking tokens bill to "
                "maxOutputTokens, so max_tokens has to cover reasoning as well as "
                "the answer."
            )
        try:
            parsed = json.loads(_strip_code_fence(text)) if schema else {"raw": text}
        except json.JSONDecodeError:
            # Re-raised unchanged: `_editorial_call` catches JSONDecodeError and retries,
            # and a STOP answer that is merely malformed is worth one more call. Only the
            # cost is added here, to the log, because the exception carries none.
            log.warning(
                "Gemini returned unparseable JSON for model %s (finishReason=%r; %s)",
                model,
                finish,
                cost,
            )
            raise
        return ChatResult(
            payload=parsed,
            latency_ms=latency_ms,
            model_tag=model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=output_tokens,
        )
    finally:
        if close_client:
            client.close()


def _combine(first: ChatResult | None, retry: ChatResult) -> ChatResult:
    """Fold a first attempt's cost into the retry that replaced it.

    A stage that retries makes two calls and must report both, or a validation failure
    looks free in the token accounting and nobody notices the stage is retrying.
    `first` is None when the first call raised before returning anything.
    """
    if first is None:
        return retry

    def add(a: int | None, b: int | None) -> int | None:
        return None if a is None and b is None else (a or 0) + (b or 0)

    return retry._replace(
        latency_ms=first.latency_ms + retry.latency_ms,
        input_tokens=add(first.input_tokens, retry.input_tokens),
        output_tokens=add(first.output_tokens, retry.output_tokens),
    )


def _model_for(provider: str, tier: str) -> str:
    """The model name `provider` uses for `tier`.

    The gateway addresses models by tier alias only and answers 404 for a real model name,
    which is the point: a tier can be repointed without any caller changing.
    """
    if provider == "gateway":
        return settings.GATEWAY_FAST_MODEL if tier == TIER_FAST else settings.GATEWAY_SMART_MODEL
    if provider == "mimo":
        return settings.MIMO_FAST_MODEL if tier == TIER_FAST else settings.MIMO_DEEP_MODEL
    if provider == "gemini":
        return settings.GEMINI_FAST_MODEL if tier == TIER_FAST else settings.GEMINI_DEEP_MODEL
    raise RuntimeError(f"unknown LLM provider {provider!r}; expected 'gateway', 'mimo' or 'gemini'")


def _dispatch(
    provider: str,
    tier: str,
    prompt: str,
    schema: dict,
    num_predict: int,
    client: httpx.Client | None = None,
    temperature: float = 0,
    thinking_level: str | None = None,
) -> ChatResult:
    """One chat call to `provider` at `tier`.

    `model_tag` is what actually served the call. On the gateway that is the alias, not the
    model behind it — the gateway echoes the alias back and does not say which model it
    resolves to, so the database can record the tier and no more. `Analysis.model_digest` is
    empty for the same reason: only Ollama exposed /api/tags, and the direct Ollama path was
    removed on 2026-08-25.
    """
    model = _model_for(provider, tier)
    if provider == "gateway":
        return gateway_chat(
            model=model,
            prompt=prompt,
            schema=schema,
            max_tokens=num_predict,
            client=client,
            temperature=temperature,
        )

    if provider == "gemini":
        return gemini_chat(
            model=model,
            prompt=prompt,
            schema=schema,
            max_tokens=num_predict,
            client=client,
            temperature=temperature,
            thinking_level=thinking_level,
        )

    if not settings.MIMO_API_KEY or not settings.MIMO_BASE_URL:
        raise RuntimeError("provider is mimo but MIMO_API_KEY/MIMO_BASE_URL are unset")
    return mimo_chat(
        model=model,
        prompt=prompt,
        schema=schema,
        timeout=settings.MIMO_TIMEOUT,
        max_tokens=num_predict,
        temperature=temperature,
        client=client,
    )


def classifier_chat(
    tier: str,
    prompt: str,
    schema: dict,
    num_predict: int,
    client: httpx.Client | None = None,
) -> ChatResult:
    """Dispatch a triage or classification call.

    CLASSIFIER_PROVIDER covers both stages together because they share a backend; there is
    no measurement saying triage and classification want different providers. It stays a
    separate setting from LLM_PROVIDER on purpose: these two stages make several hundred
    calls a day, and inheriting would move that volume the moment the editorial provider
    changed.
    """
    # A decision is deterministic. Temperature 0 is stated here, not inherited, so the
    # editorial's sampling setting can never leak into triage or classification.
    return _dispatch(
        provider=settings.CLASSIFIER_PROVIDER,
        tier=tier,
        prompt=prompt,
        schema=schema,
        num_predict=num_predict,
        client=client,
        temperature=0,
    )


def editorial_chat(
    prompt: str,
    schema: dict,
    num_predict: int,
    client: httpx.Client | None = None,
    provider: str | None = None,
    tier: str = TIER_DEEP,
    temperature: float | None = None,
    thinking_level: str | None = None,
) -> ChatResult:
    """Dispatch an editorial call.

    The single-stage Uzbek editorial is routed via EDITORIAL_UZ_PROVIDER.
    Triage and classification have their own switch via classifier_chat.
    """
    # The post is sampled, not decided: see EDITORIAL_TEMPERATURE in settings. The fact
    # and length constraints are enforced by code after this call, not by the temperature.
    return _dispatch(
        provider=provider or settings.LLM_PROVIDER,
        tier=tier,
        prompt=prompt,
        schema=schema,
        num_predict=num_predict,
        client=client,
        temperature=settings.EDITORIAL_TEMPERATURE if temperature is None else temperature,
        thinking_level=thinking_level,
    )


# --- Source-based maturity ceiling -------------------------------------------
# Measured 2026-08-17: 12 of 15 selected items came back `reproducible_open_source`,
# including seven arXiv abstracts scored evidence 9-10. paper_only was assigned to
# nothing, so the hard exclusion that implements the anti-vapourware rule excluded
# nothing.
#
# The prompt is not at fault. CONTENT_SCHEMA §3 says reproducible_open_source requires a
# link that resolves today — but the model cannot open a link. It falls back to the only
# signal present, "we release our code", which appears in essentially every paper
# abstract. The task was given to the wrong layer.
#
# The source is ground truth and needs no inference: an arXiv abstract is a paper.

#: A URL from one of these is a paper whatever its abstract promises.
PAPER_DOMAINS = (
    "arxiv.org",
    "huggingface.co/papers",
    "openreview.net",
    "biorxiv.org",
    "medrxiv.org",
    "ar5iv.org",
)

#: Claim strength, strongest to weakest. Used only to detect a claim above the ceiling.
#:
#: paper_only ranks above announcement_only: a paper is a real artifact that can be read,
#: while a bare announcement offers nothing. Ordering them the other way made the ceiling
#: rewrite announcement_only into paper_only, which is not capping — both are excluded
#: from publication either way, so the rewrite was churn with no effect on output.
MATURITY_RANK = {
    Maturity.PRODUCTION_DEPLOYMENT: 5,
    Maturity.LIVE_PRODUCT: 4,
    Maturity.REPRODUCIBLE_OPEN_SOURCE: 3,
    Maturity.PUBLIC_PILOT: 2,
    Maturity.PAPER_ONLY: 1,
    Maturity.ANNOUNCEMENT_ONLY: 0,
}


def maturity_ceiling(article: Article) -> str | None:
    """Highest maturity this item may claim without checking an artifact. None = no cap.

    Deliberately keyed on the URL, not the connector: `hn` links to papers, repositories
    and products alike, so the connector alone would cap the wrong things. A HuggingFace
    *model card* is not a paper — only `huggingface.co/papers` is — and the Qwen model
    card that scored reproducible_open_source was correct to.
    """
    url = (article.canonical_url or "").lower()
    is_paper = any(d in url for d in PAPER_DOMAINS) or (
        article.source and article.source.connector == "hf"
    )
    if article.artifact_verified and is_paper:
        return Maturity.REPRODUCIBLE_OPEN_SOURCE
    if is_paper:
        return Maturity.PAPER_ONLY
    return None


def apply_maturity_ceiling(article: Article, payload: dict) -> dict:
    """Downgrade an over-claimed maturity in place. Logs every correction it makes.

    The log line matters: it is the measurement of how often the model over-claims, and
    the evidence for whether this rule can later be relaxed.
    """
    ceiling = maturity_ceiling(article)
    if ceiling is None:
        return payload
    claimed = payload.get("maturity")
    if claimed not in MATURITY_RANK or MATURITY_RANK[claimed] <= MATURITY_RANK[ceiling]:
        return payload
    log.info(
        "Maturity ceiling: article %s claimed %s, capped to %s (%s)",
        article.id,
        claimed,
        ceiling,
        article.canonical_url[:80],
    )
    payload["maturity"] = ceiling
    payload["maturity_capped_from"] = claimed
    return payload


def _verify_artifact(article: Article) -> bool:
    """Verify a paper repository once and carry the verdict to classification."""
    if not getattr(settings, "ARTIFACT_VERIFICATION_ENABLED", True):
        return False
    if article.artifact_verified is not None:
        return article.artifact_verified

    url = artifacts.find_repo_url(article.extracted_text or "", article.title or "")
    if not url:
        return False

    verified = artifacts.repo_is_real(url)
    if verified is None:
        log.warning(
            "Artifact check for article %s was inconclusive; storing nothing so a later run "
            "can ask again",
            article.id,
        )
        return False

    article.artifact_url = url
    article.artifact_verified = verified
    article.save(update_fields=["artifact_url", "artifact_verified"])
    log.info("Artifact for article %s: %s -> %s", article.id, url, verified)
    return verified


def check_rule_prefilter(article: Article) -> tuple[bool, str]:
    """Rule pre-filter before invoking any LLM.

    Returns (passed, reason_if_failed).
    """
    domain = urlparse(article.canonical_url).netloc.lower().split(":")[0]
    for block_domain in BLOCKLISTED_DOMAINS:
        if domain == block_domain or domain.endswith(f".{block_domain}"):
            return False, f"Blocklisted domain: {domain}"

    text_length = len((article.extracted_text or "").strip())
    min_chars = getattr(settings, "ARTICLE_MIN_CHARS", 400)
    if text_length < min_chars:
        return False, f"Text too short: {text_length} chars < {min_chars}"

    # A paper cannot reach a digest today, so triaging one spends the model for nothing.
    # `maturity_ceiling` caps it at `paper_only` and EXCLUDED_MATURITIES removes that from
    # ranking, both by construction rather than by score. Reusing the ceiling here rather
    # than rematching PAPER_DOMAINS keeps the two rules from drifting apart, and covers
    # the `hf` connector case as well.
    #
    # Measured 2026-08-18: 216 of 411 stored articles came from these domains and consumed
    # 169 triage and classification calls between them. Not one has ever appeared in a
    # digest, as an item or as a secondary source.
    skip_papers = getattr(settings, "SKIP_PAPER_DOMAINS", True)
    if skip_papers and maturity_ceiling(article) == Maturity.PAPER_ONLY:
        if _verify_artifact(article):
            return True, ""
        return False, "Paper domain: excluded from ranking by maturity, so never triaged"

    return True, ""


def classify_text(
    title: str,
    source_name: str,
    text: str,
    tier: str,
    num_predict: int = 400,
    client: httpx.Client | None = None,
    prompt_template: str = CLASSIFICATION_PROMPT_TEMPLATE,
) -> tuple[Classification, ChatResult]:
    """Classify article text with Pydantic validation and 1-attempt recovery.

    The recovery attempt's latency and tokens are folded into the returned result, so the
    stored Analysis records what the article actually cost rather than what the last call
    cost. A retry billed as one call is a retry nobody notices.
    """
    truncated_text = text[:8000]
    prompt = prompt_template.format(
        title=title,
        source=source_name,
        text=truncated_text,
    )

    first: ChatResult | None = None
    try:
        first = classifier_chat(
            tier=tier,
            prompt=prompt,
            schema=CLASSIFICATION_SCHEMA,
            num_predict=num_predict,
            client=client,
        )
        return Classification.model_validate(first.payload), first
    except (ValidationError, json.JSONDecodeError) as exc:
        log.warning(
            "Validation error on first attempt for '%s': %s. Retrying once with error.",
            title,
            exc,
        )
        recovery_prompt = (
            f"{prompt}\n\n"
            f"IMPORTANT: Your previous output failed schema validation with error:\n{exc}\n"
            "Please fix the error and return valid JSON conforming strictly to the schema."
        )
        retry = classifier_chat(
            tier=tier,
            prompt=recovery_prompt,
            schema=CLASSIFICATION_SCHEMA,
            num_predict=max(num_predict, 1500),
            client=client,
        )
        return Classification.model_validate(retry.payload), _combine(first, retry)


def triage_text(
    title: str,
    source_name: str,
    client: httpx.Client | None = None,
) -> tuple[bool, ChatResult]:
    """Decide from the headline whether an article is worth classifying.

    Pure with respect to the database so `eval_classifier --stage triage` and
    `eval_triage_replay` measure the same gate the pipeline runs.

    Recall-first by design: a false positive costs one classification call, a false negative
    loses the article. The prompt says so explicitly, and the measurement to watch is recall,
    not precision.

    The article body is deliberately not passed. It is already downloaded and stored, but
    sending it costs ~2000 input tokens on the pipeline's highest-volume stage.
    """
    prompt = TRIAGE_PROMPT_TEMPLATE.format(title=title, source=source_name)
    # 1000, not the ~40 tokens this answer needs. Measured 2026-08-25: at 200 the gateway's
    # `fast` alias returned finish_reason "length" with empty content on all 26 gold-set
    # rows — it charges its own reasoning to max_tokens before writing any answer, exactly
    # as the `smart` alias does. The budget is a cap, not a cost: the model stops when it is
    # done, so the saving here comes from the input side and this stays generous.
    result = classifier_chat(
        tier=TIER_FAST,
        prompt=prompt,
        schema=TRIAGE_SCHEMA,
        num_predict=1000,
        client=client,
    )
    return bool(result.payload.get("relevant")), result


def triage_article_logic(article: Article, client: httpx.Client | None = None) -> bool:
    """Triage logic using the fast model and lightweight triage prompt (T1.17)."""
    passed, reason = check_rule_prefilter(article)
    if not passed:
        log.info("Rule prefilter rejected article %s (%s): %s", article.id, article.title, reason)
        article.status = Article.Status.SKIPPED
        article.save(update_fields=["status"])
        return False

    try:
        passed, result = triage_text(
            title=article.title,
            source_name=article.source.name if article.source else "",
            client=client,
        )
    except (ValidationError, json.JSONDecodeError) as exc:
        # Permanent model schema failure on this article after retry
        log.error(
            "Model validation failed permanently for article %s (%s): %s. Marking skipped.",
            article.id,
            article.title,
            exc,
        )
        article.status = Article.Status.SKIPPED
        article.save(update_fields=["status"])
        return False
    except INFRASTRUCTURE_EXCEPTIONS as exc:
        # Transient infrastructure failure (503, timeout, connect error)
        # Do NOT change status — article remains FETCHED and will be retried on next run.
        log.warning(
            "Transient infrastructure failure during triage of article %s (%s): %s. "
            "Leaving status as FETCHED for next retry.",
            article.id,
            article.title,
            exc,
        )
        return False
    except Exception as exc:
        log.error(
            "Unexpected error during triage for article %s (%s): %s. Leaving status unchanged.",
            article.id,
            article.title,
            exc,
        )
        return False

    # No maturity ceiling here: the triage payload carries no maturity to cap. Paper
    # domains are already excluded before any LLM call by check_rule_prefilter, and the
    # ceiling still applies to the classification payload.
    _record_analysis(article, Analysis.Stage.TRIAGE, result)

    article.status = Article.Status.TRIAGED if passed else Article.Status.SKIPPED
    article.save(update_fields=["status"])
    return article.status == Article.Status.TRIAGED


def classify_article_logic(article: Article, client: httpx.Client | None = None) -> bool:
    """Classification logic on the deep tier. Sets article status to CLASSIFIED or SKIPPED."""
    try:
        classification, result = classify_text(
            title=article.title,
            source_name=article.source.name if article.source else "",
            text=article.extracted_text,
            tier=TIER_DEEP,
            num_predict=2000,
            client=client,
        )
    except (ValidationError, json.JSONDecodeError) as exc:
        # Permanent model schema failure on this article after retry
        log.error(
            "Deep classification model validation failed permanently for article %s (%s): %s. "
            "Marking skipped.",
            article.id,
            article.title,
            exc,
        )
        article.status = Article.Status.SKIPPED
        article.save(update_fields=["status"])
        return False
    except INFRASTRUCTURE_EXCEPTIONS as exc:
        # Transient infrastructure failure (503, timeout, connect error)
        # Do NOT change status — article remains TRIAGED and will be retried on next run.
        log.warning(
            "Transient infrastructure failure during deep classification of article %s (%s): %s. "
            "Leaving status as TRIAGED for next retry.",
            article.id,
            article.title,
            exc,
        )
        return False
    except Exception as exc:
        log.error(
            "Unexpected error during deep classification for article %s (%s): %s. "
            "Leaving status unchanged.",
            article.id,
            article.title,
            exc,
        )
        return False

    # The source decides what a paper is; the model is not asked to re-derive it.
    capped = apply_maturity_ceiling(article, result.payload)
    classification = Classification.model_validate(capped)

    _record_analysis(article, Analysis.Stage.CLASSIFICATION, result._replace(payload=capped))

    if (
        classification.primary_topic == Topic.IRRELEVANT
        or classification.maturity in EXCLUDED_MATURITIES
    ):
        article.status = Article.Status.SKIPPED
    else:
        article.status = Article.Status.CLASSIFIED

    article.save(update_fields=["status"])
    return article.status == Article.Status.CLASSIFIED


def _normalize_uz_payload(payload: dict) -> dict:
    """Normalize Uzbek translation payload to satisfy deterministic gates."""
    if not isinstance(payload, dict):
        return payload
    normalized = {}
    for k, v in payload.items():
        if isinstance(v, str):
            normalized[k] = post_format.strip_markdown_formatting(v)
        else:
            normalized[k] = v

    return normalized


def render_editorial_preview(article, payload: dict) -> str:
    """The caption exactly as the channel would show it, or ValueError naming why not.

    One place builds the renderer's input from an article and a payload. The gates use
    it to refuse a post before storing it, and the admin uses it to show the operator a
    freshly written post without creating a Digest -- a Digest would claim a publishing
    slot, and the uniqueness constraint on (digest_date, edition) makes that expensive.
    """
    return post_format.render_dayjest_post(
        {
            **payload,
            "url": article.canonical_url,
            "article_text": article.extracted_text or "",
            "topic": _classified_topic(article) or "production_engineering",
        },
        max_chars=settings.DAYJEST_MAX_CHARS,
        max_sentences=settings.DAYJEST_MAX_SENTENCES,
    )


#: What the reader sees. The gates judge these and nothing else.
READER_FIELDS = ("lead_uz", "body_1_uz", "kicker_uz")


def reader_fields(payload: dict) -> dict:
    """The reader-facing slice of an editorial payload, for the source-fidelity gates.

    The gates used to receive the whole payload, and `check_numbers_against_source`
    walks every key it is given -- so `technical`, a block that is never published and
    that the prompt tells the model to copy verbatim from the source, was judged as a
    reader-facing numeric claim. Measured 2026-09-08 on a sherpa-onnx release: the gate
    reported "Number not in the article: 2.0 (in technical)" because
    `technical.license` was "Apache-2.0". In the pipeline that is one retry and then a
    discarded article, over a licence string. `post_style` travels along because the
    gate reads it to decide the legacy headline exemption; `technical` reaches the
    glossary gate through its own parameter, as the English side.
    """
    return {k: payload.get(k, "") for k in READER_FIELDS} | (
        {"post_style": payload["post_style"]} if "post_style" in payload else {}
    )


def _uz_violations(article, payload: dict) -> list[str]:
    """Check source fidelity gates and the actual new renderer before storing a post."""
    violations = translation_gates.validate_against_source(
        article_title=article.title,
        article_text=article.extracted_text or "",
        uz_fields=reader_fields(payload),
        technical=payload.get("technical"),
    )
    if payload.get("post_style") in post_format.DAYJEST_STYLES:
        try:
            render_editorial_preview(article, payload)
        except ValueError as exc:
            violations.append(str(exc))
    return violations


def _classified_topic(article: Article) -> str | None:
    """The topic the deep tier assigned, or None if the article was never classified.

    The newest row wins: a re-run leaves more than one classification and the latest is the
    live verdict, which is the rule select_digest_candidates uses too.

    Reads `analyses.all()` rather than filtering in the database on purpose. The caller
    prefetches `analyses`, and a `.filter()` on a prefetched related manager issues a fresh
    query and throws that cache away — so the prefetch would have bought nothing.
    """
    classifications = [
        a for a in article.analyses.all() if a.stage == Analysis.Stage.CLASSIFICATION
    ]
    if not classifications:
        return None
    return max(classifications, key=lambda a: a.created_at).topic


def analyse_for_digest_logic(
    article_ids: list[int],
    client: httpx.Client | None = None,
    force: bool = False,
) -> list[Analysis]:
    """Read each article and write its Uzbek post in one call.

    `force=True` skips the reuse memo and writes a fresh row even when a usable or a
    discarded editorial already exists. The pipeline never sets it; the admin's "write a
    new post" button does, because an operator pressing it on an article that already
    has a post wants to see what the *current* prompt makes of it.

    Replaced the two-stage English-then-translate flow on 2026-08-26. That split let a
    poor post be traced to comprehension or to translation, which was worth less than the
    repair it prevented: translation received four English fields and never the article, so
    it could render a badly chosen fact but never replace it.
    """
    articles = list(
        Article.objects.filter(id__in=article_ids)
        .select_related("source")
        .prefetch_related("analyses")
    )

    created: list[Analysis] = []
    for art in articles:
        existing = (
            art.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ).order_by("-created_at").first()
        )
        # A row marked `discarded_violations` is a post that failed its gates twice. It
        # is stored so its cost is counted, and it is skipped here so the article is not
        # re-drafted every cycle: the gates are deterministic and the draft already had
        # its retry, so a third attempt spends two more deep-tier calls on an outcome
        # that has not changed. Before this the row was never written at all, which lost
        # the cost from `pipeline_stats` *and* re-paid it morning and evening until the
        # article aged out of the candidate window.
        if not force and existing and existing.payload.get("discarded_violations"):
            log.info(
                "Skipping article %s: its editorial was discarded on %s.",
                art.id,
                existing.created_at.date(),
            )
            continue
        if not force and existing and existing.payload.get("lead_uz"):
            created.append(existing)
            continue

        try:
            result = write_post(art, client=client)

            violations = _uz_violations(art, result.payload)
            if violations:
                log.warning(
                    "Uzbek gates failed for article %s: %s. Retrying once.", art.id, violations
                )
                if settings.EDITORIAL_DRAFT_LANG == "ru":
                    result = _retry_reexpress(art, violations, result, client)
                else:
                    result = _retry_editorial_uz(art, violations, result, client)

            still = _uz_violations(art, result.payload)
            if still:
                # Recorded, not dropped. `continue` alone skipped `_record_analysis`, so
                # the two-to-four deep-tier calls this article had already cost vanished
                # from the accounting entirely -- not even as an unmeasured row, which is
                # the one thing `pipeline_stats` says its totals are a floor because of.
                # The marker keeps the row out of the reuse path above.
                log.error("Discarding invalid editorial for article %s: %s", art.id, still)
                discarded = result._replace(
                    payload={**result.payload, "discarded_violations": still}
                )
                _record_analysis(art, Analysis.Stage.EDITORIAL_UZ, discarded)
                continue

            # One call. The rewrite pass that used to run here cost ~45% of every
            # article's tokens; measured 2026-09-08 it changed 7 of 15 posts substantially
            # with nothing to show those were improvements, and its two unique jobs --
            # cross-check against the article, do not repeat the lead -- now sit in the
            # draft prompt's own self-check. The draft is the post.
            created.append(_record_analysis(art, Analysis.Stage.EDITORIAL_UZ, result))
            log.info("Uzbek post done for article %s", art.id)
        except Exception as exc:
            log.error("Uzbek editorial failed for article %s (%s): %s", art.id, art.title, exc)

    return created


def _editorial_prompt(article: Article) -> str:
    """The one prompt, built in one place for the draft and its retry."""
    block_key = shape_for(_classified_topic(article))
    prompt = EDITORIAL_UZ_PROMPT.format(
        block=UZ_BLOCKS[block_key],
        example=UZ_EXAMPLES[block_key],
        title=article.title,
        source=article.source.name if article.source else "",
        text=(article.extracted_text or "")[:8000],
    )
    return prompt


def _retry_editorial_uz(art, violations, first, client):
    """One retry naming the violations, then keep whichever attempt is clean.

    A permanently failing article is dropped by compose_and_publish, which filters
    candidates on a usable row; DIGEST_SELECT_MARGIN covers the hole.
    """
    retry_prompt = (
        _editorial_prompt(art)
        + "\n\nIMPORTANT: your previous answer failed these checks:\n"
        + "\n".join(f"- {v}" for v in violations)
        + "\nFix exactly these and return valid JSON."
    )
    retry = _editorial_call(
        prompt=retry_prompt,
        schema=EDITORIAL_UZ_SCHEMA,
        model_cls=EditorialUz,
        num_predict=settings.EDITORIAL_NUM_PREDICT,
        client=client,
        provider=settings.EDITORIAL_UZ_PROVIDER,
        tier=TIER_DEEP,
    )
    retry = retry._replace(payload=_normalize_uz_payload(retry.payload))
    retry.payload["post_style"] = post_format.PLAIN_PHOTO_STYLE
    still = _uz_violations(art, retry.payload)
    if still:
        log.error("Uzbek gates failed permanently for article %s: %s", art.id, still)
    return _combine(first, retry)


def editorial_uz_for_article(
    article: Article,
    client: httpx.Client | None = None,
) -> ChatResult:
    """Read the article and write the Uzbek post in one call (2026-08-26 design).

    `analyse_for_digest_logic` is the pipeline's only caller. This was introduced beside
    the two-stage English-then-translate flow so the two could be measured against each
    other; that flow was removed on 2026-08-26, leaving this the single editorial path.

    No Analysis row is written here. The caller decides whether the result is worth storing,
    which keeps the eval command from polluting the pipeline's data.
    """
    result = _editorial_call(
        prompt=_editorial_prompt(article),
        schema=EDITORIAL_UZ_SCHEMA,
        model_cls=EditorialUz,
        num_predict=settings.EDITORIAL_NUM_PREDICT,
        client=client,
        provider=settings.EDITORIAL_UZ_PROVIDER,
        tier=TIER_DEEP,
    )
    # The prompt forbids markdown; normalize mechanically rather than trusting the model.
    payload = _normalize_uz_payload(result.payload)
    payload["post_style"] = post_format.PLAIN_PHOTO_STYLE
    return result._replace(payload=payload)


def _record_analysis(article, stage, result: ChatResult) -> Analysis:
    """Store one call. Every Analysis row goes through here so none forgets its cost."""
    return Analysis.objects.create(
        article=article,
        stage=stage,
        model_tag=result.model_tag,
        model_digest="",
        payload=result.payload,
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


def _draft_prompt_ru(article: Article) -> str:
    """The Russian draft's prompt: the production prompt localised, one block and one example
    of the story's kind, no recent-leads block -- the Russian draft varies its openings on
    its own (measured 2026-09-09: 0 of 15 release-verb openings without it)."""
    block_key = shape_for(_classified_topic(article))
    return EDITORIAL_RU_PROMPT.format(
        block=RU_BLOCKS[block_key],
        example=RU_EXAMPLES[block_key],
        title=article.title,
        source=article.source.name if article.source else "",
        text=(article.extracted_text or "")[:8000],
    )


def _reexpress_prompt(draft: dict) -> str:
    return REEXPRESS_RU_UZ_PROMPT.format(
        lead=draft.get("lead_uz", ""),
        body=draft.get("body_1_uz") or "(нет)",
        kicker=draft.get("kicker_uz") or "(нет)",
    )


def _reexpress(draft: dict, client, suffix: str = "") -> ChatResult:
    """Say the Russian draft in Uzbek. Three fields come back; the facts are already chosen."""
    result = _editorial_call(
        prompt=_reexpress_prompt(draft) + suffix,
        schema=REEXPRESS_SCHEMA,
        model_cls=ReexpressUz,
        num_predict=REEXPRESS_NUM_PREDICT,
        client=client,
        provider=settings.EDITORIAL_UZ_PROVIDER,
        tier=TIER_DEEP,
        temperature=REEXPRESS_TEMPERATURE,
        thinking_level="low",
    )
    said = _normalize_uz_payload(result.payload)
    for field in READER_FIELDS:
        if (said.get(field) or "").strip() in ("(нет)", "(yo'q)"):
            said[field] = ""
    return result._replace(payload=said)


def _chain_payload(draft: dict, said: dict) -> dict:
    """The stored row: Uzbek reader fields, the draft's technical block and evidence level,
    and the Russian draft under `draft_ru` so a post can be traced to the text it came from."""
    return {
        **{k: v for k, v in draft.items() if k not in READER_FIELDS},
        **{k: said.get(k, "") for k in READER_FIELDS},
        "draft_ru": {k: draft.get(k, "") for k in READER_FIELDS},
        "post_style": post_format.PLAIN_PHOTO_STYLE,
    }


def editorial_ru_uz_for_article(article: Article, client: httpx.Client | None = None) -> ChatResult:
    """Two layers: a Russian draft, then the same post said in Uzbek (2026-09-09 design).

    The draft is the production editorial in Russian, where the model chooses facts and
    structure best; the second call is a rewrite with the facts fixed. Both calls' cost is
    folded into the one result. See editorial_prompts_ru for the measurements.
    """
    draft = _editorial_call(
        prompt=_draft_prompt_ru(article),
        schema=EDITORIAL_UZ_SCHEMA,
        model_cls=EditorialUz,
        num_predict=settings.EDITORIAL_NUM_PREDICT,
        client=client,
        provider=settings.EDITORIAL_UZ_PROVIDER,
        tier=TIER_DEEP,
    )
    ru = _normalize_uz_payload(draft.payload)
    said = _reexpress(ru, client)
    return _combine(draft, said)._replace(payload=_chain_payload(ru, said.payload))


def _retry_reexpress(art, violations, first: ChatResult, client) -> ChatResult:
    """A gate failure on the chain redoes the cheap layer, not the draft.

    The draft's facts stand; the Uzbek wording is said again with the violations named.
    That is ~1k tokens against the draft's 7k, and the gates are deterministic, so a
    second draft would be spent re-choosing facts the gates did not object to.
    """
    ru = {
        **{
            k: v
            for k, v in first.payload.items()
            if k not in (*READER_FIELDS, "draft_ru", "post_style")
        },
        **(first.payload.get("draft_ru") or {}),
    }
    suffix = (
        "\n\nIMPORTANT: your previous answer failed these checks:\n"
        + "\n".join(f"- {v}" for v in violations)
        + "\nFix exactly these and return valid JSON."
    )
    retry = _reexpress(ru, client, suffix)
    payload = _chain_payload(ru, retry.payload)
    still = _uz_violations(art, payload)
    if still:
        log.error("Uzbek gates failed permanently for article %s: %s", art.id, still)
    return _combine(first, retry)._replace(payload=payload)


def write_post(
    article: Article,
    client: httpx.Client | None = None,
) -> ChatResult:
    """The editorial stage's one entry point; EDITORIAL_DRAFT_LANG picks the design.

    `uz` (default) writes the Uzbek post in one call; `ru` drafts in Russian and says it
    in Uzbek. Neither is shown earlier posts: the recent-leads history of 2026-09-08 was
    removed on 2026-09-09 -- the owner wants no old news in the model's context, and the
    Russian draft varies its openings without it (0 of 15 release-verb openings).
    """
    if settings.EDITORIAL_DRAFT_LANG == "ru":
        return editorial_ru_uz_for_article(article, client=client)
    return editorial_uz_for_article(article, client=client)


def _editorial_call(
    prompt: str,
    schema: dict,
    model_cls,
    num_predict: int,
    client=None,
    provider: str | None = None,
    tier: str = TIER_DEEP,
    temperature: float | None = None,
    thinking_level: str | None = None,
) -> ChatResult:
    """One editorial call with validation retry and empty technical block check (T1.17).

    Every attempt's cost is folded into the returned result, so a stage that retried twice
    is not recorded as having cost one call.
    """
    first: ChatResult | None = None
    try:
        first = editorial_chat(
            prompt, schema, num_predict, client, provider, tier, temperature, thinking_level
        )
        model_cls.model_validate(first.payload)
        result = first
    except (ValidationError, json.JSONDecodeError) as exc:
        log.warning("Editorial validation failed, retrying once: %s", exc)
        recovery = (
            f"{prompt}\n\nIMPORTANT: your previous output failed validation:\n{exc}\n"
            "Return valid JSON conforming strictly to the schema."
        )
        retry = editorial_chat(
            recovery,
            schema,
            max(num_predict, 2000),
            client,
            provider,
            tier,
            temperature,
            thinking_level,
        )
        model_cls.model_validate(retry.payload)
        result = _combine(first, retry)

    # Post-check for empty lead_uz in Uzbek editorial
    if model_cls is EditorialUz and not result.payload.get("lead_uz", "").strip():
        log.warning("Empty lead_uz in Uzbek editorial, retrying once.")
        recovery = (
            f"{prompt}\n\nIMPORTANT: The 'lead_uz' field was empty. "
            "You must provide a non-empty 1-sentence lead."
        )
        try:
            retry = editorial_chat(
                recovery,
                schema,
                max(num_predict, 2000),
                client,
                provider,
                tier,
                temperature,
                thinking_level,
            )
            model_cls.model_validate(retry.payload)
            if retry.payload.get("lead_uz", "").strip():
                result = _combine(result, retry)
        except Exception as exc:
            # The failed attempt still cost tokens, but the provider raised before
            # reporting them, so there is nothing to add.
            log.debug("lead_uz recovery attempt failed: %s", exc)

    return result
