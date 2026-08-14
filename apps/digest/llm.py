"""LLM integration: Ollama client, Pydantic schemas, prompt constants, and classification.

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
from typing import Any
from urllib.parse import urlparse

import httpx
from django.conf import settings
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import EXCLUDED_MATURITIES, Analysis, Article, Maturity, Topic

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
    """A 5xx or 429 from Ollama. Worth retrying; client errors (4xx) are not."""


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

# Verbatim enum definitions and boundaries from CONTENT_SCHEMA.md §2 and §3
CLASSIFICATION_PROMPT_TEMPLATE = (
    "You are a technical editor for an AI-engineering news digest read by "
    "engineers and technical decision-makers.\n\n"
    "Classify the article below. Return JSON only conforming to the schema.\n\n"
    "## primary_topic — choose the SINGLE best fit\n\n"
    "- frontier_models: A specific named model is released, updated, or given new capabilities. "
    "Not a technique — that is new_approaches. "
    "Not a tool that runs models — that is production_engineering.\n"
    "- ai_agents: A system where an LLM takes actions through tools: agent frameworks, "
    "tool calling, MCP/A2A, multi-agent orchestration, coding or browser agents. "
    "Not any paper that merely uses the word \"agent\". "
    "Not a tool release that happens to support agents — that is production_engineering.\n"
    "- new_approaches: A new method, architecture, training technique, or inference technique. "
    "This is the default for research papers. Not a named model release.\n"
    "- speech_voice: Audio is an input or an output: STT, TTS, voice agents, diarization, "
    "audio models.\n"
    "- robotics: Physical embodiment: robots, control policies, embodied AI.\n"
    "- fintech: Financial technology: payments, banking, lending, financial infrastructure.\n"
    "- govtech: Government digital services and public administration systems.\n"
    "- production_engineering: Infrastructure, serving, deployment, and developer tooling — "
    "including releases and changelogs of such tools. "
    "An Ollama, vLLM or LangGraph changelog belongs here even when it mentions agents or models.\n"
    "- startups: A company shipping a deployed commercial product. "
    "Not any article that mentions a company. "
    "Not a model release from a large lab — that is frontier_models.\n"
    "- technical_talks: A recorded presentation: conference talk, demo, technical video.\n"
    "- safety_security: Alignment, jailbreaks, model or agent security, permissions, red-teaming. "
    "Not general research into model behaviour — that is new_approaches.\n"
    "- irrelevant: Everything else: executive appointments, funding rounds, partnerships, "
    "marketing, opinion pieces, consumer gadgets, general business news.\n\n"
    "Mandatory rule:\n"
    "If the article contains no technical substance, primary_topic MUST be irrelevant.\n"
    "Do not force a technical category onto a business story.\n\n"
    "## maturity — what actually exists right now\n\n"
    "- production_deployment: Running in a named real organisation, with reported results. "
    "Not \"could be deployed\".\n"
    "- live_product: A publicly usable product or API available today. "
    "A changelog for an already-shipped tool is live_product, not production_deployment.\n"
    "- reproducible_open_source: Code or weights are downloadable today at a working link.\n"
    "- public_pilot: Limited preview, waitlist, or restricted access.\n"
    "- announcement_only: Announced, but nothing usable has been released.\n"
    "- paper_only: A research paper or preprint.\n\n"
    "The paper_only / reproducible_open_source boundary:\n"
    "A paper is paper_only even when it promises code, says \"code will be released\", or links "
    "a repository that does not yet exist. reproducible_open_source requires a link that resolves "
    "to real artifacts today. Excellent results do not raise maturity — only shipped artifacts do."
    "\n\n"
    "## Numeric dimensions\n\n"
    "- novelty: 1 = rehash of known news, 10 = genuinely new capability or result\n"
    "- evidence: 1 = vendor claim only, 10 = reproducible artifacts: weights, repo, "
    "independent eval\n"
    "- production_readiness: 1 = paper or announcement, 10 = deployed and documented\n\n"
    "ARTICLE\n"
    "Title: {title}\n"
    "Source: {source}\n"
    "---\n"
    "{text}\n"
)


def _get_base_url() -> str:
    return getattr(settings, "OLLAMA_BASE_URL", "").rstrip("/")


def fetch_model_digest(model_name: str, client: httpx.Client | None = None) -> str:
    """Fetch or resolve the 64-char model digest from Ollama tags."""
    base_url = _get_base_url()
    if not base_url:
        return ""
    try:
        if client:
            r = client.get(f"{base_url}/api/tags", timeout=10)
        else:
            with httpx.Client(timeout=10) as c:
                r = c.get(f"{base_url}/api/tags")
        if r.status_code == 200:
            for m in r.json().get("models", []):
                if m.get("name") == model_name or m.get("model") == model_name:
                    return m.get("digest", "")
    except Exception as exc:
        log.debug("Could not fetch digest for model %s: %s", model_name, exc)
    return ""


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(RETRYABLE_LLM_EXCEPTIONS),
    reraise=True,
)
def _chat_post(client: httpx.Client, url: str, payload: dict) -> httpx.Response:
    r = client.post(url, json=payload)
    if r.status_code >= 500 or r.status_code == 429:
        raise RetryableLLMError(f"{r.status_code} from {url}: {r.text[:200]}")
    r.raise_for_status()
    return r


def ollama_chat(
    model: str,
    prompt: str,
    schema: dict | None = None,
    timeout: int = 60,
    num_predict: int = 400,
    client: httpx.Client | None = None,
) -> tuple[dict, int]:
    """Execute a structured chat completion against Ollama with mandatory num_predict and retries.

    Returns (parsed_payload, latency_ms).
    """
    base_url = _get_base_url()
    url = f"{base_url}/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {
            "temperature": 0,
            "num_predict": num_predict,
        },
    }
    if schema:
        payload["format"] = schema

    close_client = False
    if client is None:
        client = httpx.Client(timeout=timeout)
        close_client = True

    t0 = time.perf_counter()
    try:
        r = _chat_post(client, url, payload)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        data = r.json()
        content = data.get("message", {}).get("content", "")
        if schema:
            parsed = json.loads(content)
        else:
            parsed = {"raw": content}
        return parsed, latency_ms
    finally:
        if close_client:
            client.close()


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

    return True, ""


def classify_text(
    title: str,
    source_name: str,
    text: str,
    model: str,
    timeout: int,
    num_predict: int = 400,
    client: httpx.Client | None = None,
) -> tuple[Classification, dict, int, str]:
    """Classify article text with Pydantic validation and 1-attempt recovery.

    Returns (classification_obj, raw_payload, latency_ms, digest).
    """
    truncated_text = text[:8000]
    prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(
        title=title,
        source=source_name,
        text=truncated_text,
    )

    digest = fetch_model_digest(model, client=client)

    try:
        raw_payload, latency_ms = ollama_chat(
            model=model,
            prompt=prompt,
            schema=CLASSIFICATION_SCHEMA,
            timeout=timeout,
            num_predict=num_predict,
            client=client,
        )
        classification = Classification.model_validate(raw_payload)
        return classification, raw_payload, latency_ms, digest
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
        raw_payload, latency_retry_ms = ollama_chat(
            model=model,
            prompt=recovery_prompt,
            schema=CLASSIFICATION_SCHEMA,
            timeout=timeout,
            num_predict=num_predict,
            client=client,
        )
        classification = Classification.model_validate(raw_payload)
        return classification, raw_payload, latency_ms + latency_retry_ms, digest


def triage_article_logic(article: Article, client: httpx.Client | None = None) -> bool:
    """Triage logic using the fast model. Sets article status to TRIAGED or SKIPPED."""
    passed, reason = check_rule_prefilter(article)
    if not passed:
        log.info("Rule prefilter rejected article %s (%s): %s", article.id, article.title, reason)
        article.status = Article.Status.SKIPPED
        article.save(update_fields=["status"])
        return False

    model = getattr(settings, "OLLAMA_FAST_MODEL", "gemma4:latest")
    timeout = getattr(settings, "OLLAMA_FAST_TIMEOUT", 60)

    try:
        classification, raw_payload, latency_ms, digest = classify_text(
            title=article.title,
            source_name=article.source.name if article.source else "",
            text=article.extracted_text,
            model=model,
            timeout=timeout,
            num_predict=400,
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

    Analysis.objects.create(
        article=article,
        model_tag=model,
        model_digest=digest,
        payload=raw_payload,
        latency_ms=latency_ms,
    )

    # Triage decision: drop only clear irrelevant or all-low scores.
    # Do NOT reject on maturity here — 8B is unreliable for maturity
    # (M0.1: AVA-Encoder arXiv paper got production_deployment).
    # Maturity exclusion happens in ranking.select_digest_candidates()
    # after the 31B classification pass.
    if (
        classification.primary_topic == Topic.IRRELEVANT
        or (
            classification.novelty < 3
            and classification.evidence < 3
            and classification.production_readiness < 3
        )
    ):
        article.status = Article.Status.SKIPPED
    else:
        article.status = Article.Status.TRIAGED

    article.save(update_fields=["status"])
    return article.status == Article.Status.TRIAGED


def classify_article_logic(article: Article, client: httpx.Client | None = None) -> bool:
    """Classification logic using the deep model. Sets article status to CLASSIFIED or SKIPPED."""
    model = getattr(settings, "OLLAMA_DEEP_MODEL", "gemma4:31b")
    timeout = getattr(settings, "OLLAMA_DEEP_TIMEOUT", 300)

    try:
        classification, raw_payload, latency_ms, digest = classify_text(
            title=article.title,
            source_name=article.source.name if article.source else "",
            text=article.extracted_text,
            model=model,
            timeout=timeout,
            num_predict=400,
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

    Analysis.objects.create(
        article=article,
        model_tag=model,
        model_digest=digest,
        payload=raw_payload,
        latency_ms=latency_ms,
    )

    if (
        classification.primary_topic == Topic.IRRELEVANT
        or classification.maturity in EXCLUDED_MATURITIES
    ):
        article.status = Article.Status.SKIPPED
    else:
        article.status = Article.Status.CLASSIFIED

    article.save(update_fields=["status"])
    return article.status == Article.Status.CLASSIFIED
