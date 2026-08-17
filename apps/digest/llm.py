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


class TechnicalDetails(BaseModel):
    what_was_built: str = ""
    architecture: str = ""
    license: str = ""
    repo_url: str = ""
    api_url: str = ""
    hardware: str = ""
    install: str = ""
    benchmarks: str = ""
    limitations: str = ""
    local_deployable: bool = False


class DeepAnalysis(BaseModel):
    """Matches CONTENT_SCHEMA.md §5 exactly."""

    summary_uz: str
    why_it_matters_uz: str
    leadership_uz: str
    technical: TechnicalDetails
    uzbekistan_application_uz: str
    evidence_level: str = Field(default="vendor_claim_only")


DEEP_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary_uz": {"type": "string"},
        "why_it_matters_uz": {"type": "string"},
        "leadership_uz": {"type": "string"},
        "technical": {
            "type": "object",
            "properties": {
                "what_was_built": {"type": "string"},
                "architecture": {"type": "string"},
                "license": {"type": "string"},
                "repo_url": {"type": "string"},
                "api_url": {"type": "string"},
                "hardware": {"type": "string"},
                "install": {"type": "string"},
                "benchmarks": {"type": "string"},
                "limitations": {"type": "string"},
                "local_deployable": {"type": "boolean"},
            },
            "required": ["what_was_built", "limitations", "local_deployable"],
        },
        "uzbekistan_application_uz": {"type": "string"},
        "evidence_level": {
            "type": "string",
            "enum": ["vendor_claim_only", "multiple_evidence"],
        },
    },
    "required": [
        "summary_uz",
        "why_it_matters_uz",
        "leadership_uz",
        "technical",
        "uzbekistan_application_uz",
        "evidence_level",
    ],
}

EDITORIAL_PROMPT_TEMPLATE = (
    "You are a senior AI technical editor writing for an audience of engineering leaders "
    "and AI engineers in Uzbekistan.\n\n"
    "Perform a deep technical and editorial analysis of the article below. "
    "Return JSON only conforming strictly to the schema.\n\n"
    "## Strategy C and Editorial Rules:\n"
    "1. Ground every claim strictly in the article text. If a detail (license, benchmark, repo, "
    'architecture) is not in the source, leave it as an empty string (""). NEVER INVENT.\n'
    "2. Write summary_uz, why_it_matters_uz, leadership_uz, and uzbekistan_application_uz "
    "in clear, natural, grammatical Uzbek (Latin script).\n"
    "3. Keep technical terminology, model names, metric names, and software names in English "
    "(e.g., 'service tier', 'output token', 'Mean Absolute Error (MAE)', 'weights', 'checkpoint', "
    "'fine-tuning', 'inference', 'benchmark', 'repo', 'latency').\n"
    "4. local_deployable must be true ONLY if weights/code can be run self-hosted.\n"
    "5. evidence_level must be 'vendor_claim_only' unless independently validated.\n\n"
    "ARTICLE\n"
    "Title: {title}\n"
    "Source: {source}\n"
    "---\n"
    "{text}\n"
)

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
    'Not any paper that merely uses the word "agent". '
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
    'Not "could be deployed".\n'
    "- live_product: A publicly usable product or API available today. "
    "A changelog for an already-shipped tool is live_product, not production_deployment.\n"
    "- reproducible_open_source: Code or weights are downloadable today at a working link.\n"
    "- public_pilot: Limited preview, waitlist, or restricted access.\n"
    "- announcement_only: Announced, but nothing usable has been released.\n"
    "- paper_only: A research paper or preprint.\n\n"
    "The paper_only / reproducible_open_source boundary:\n"
    'A paper is paper_only even when it promises code, says "code will be released", or links '
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
def _chat_post(
    client: httpx.Client, url: str, payload: dict, headers: dict | None = None
) -> httpx.Response:
    r = client.post(url, json=payload, headers=headers)
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


def mimo_chat(
    model: str,
    prompt: str,
    schema: dict | None = None,
    timeout: int = 120,
    max_tokens: int = 1500,
    client: httpx.Client | None = None,
) -> tuple[dict, int]:
    """OpenAI-compatible chat completion against MiMo. Returns (parsed_payload, latency_ms).

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
    url = f"{settings.MIMO_BASE_URL}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
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
        r = _chat_post(client, url, payload, headers={
            "Authorization": f"Bearer {settings.MIMO_API_KEY}",
            "Content-Type": "application/json",
        })
        latency_ms = int((time.perf_counter() - t0) * 1000)
        content = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content) if schema else {"raw": content}
        return parsed, latency_ms
    finally:
        if close_client:
            client.close()


def editorial_chat(
    prompt: str,
    schema: dict,
    num_predict: int,
    client: httpx.Client | None = None,
) -> tuple[dict, int, str]:
    """Dispatch the editorial call to the configured provider.

    Returns (payload, latency_ms, model_tag). Only the editorial stage is routed this
    way — triage and classification stay on local Ollama (ADR-004 §5).
    """
    if settings.LLM_PROVIDER == "mimo":
        if not settings.MIMO_API_KEY or not settings.MIMO_BASE_URL:
            raise RuntimeError("LLM_PROVIDER=mimo but MIMO_API_KEY/MIMO_BASE_URL are unset")
        model = settings.MIMO_EDITORIAL_MODEL
        payload, ms = mimo_chat(
            model=model,
            prompt=prompt,
            schema=schema,
            timeout=settings.MIMO_TIMEOUT,
            max_tokens=num_predict,
            client=client,
        )
        return payload, ms, model

    model = settings.OLLAMA_DEEP_MODEL
    payload, ms = ollama_chat(
        model=model,
        prompt=prompt,
        schema=schema,
        timeout=settings.OLLAMA_DEEP_TIMEOUT,
        num_predict=num_predict,
        client=client,
    )
    return payload, ms, model


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
    latency_ms = 0

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
            num_predict=max(num_predict, 1500),
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
            num_predict=1000,
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
        stage=Analysis.Stage.TRIAGE,
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
    if classification.primary_topic == Topic.IRRELEVANT or (
        classification.novelty < 3
        and classification.evidence < 3
        and classification.production_readiness < 3
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

    Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.CLASSIFICATION,
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


def deep_analyze_text(
    title: str,
    source_name: str,
    text: str,
    num_predict: int = 1500,
    client: httpx.Client | None = None,
) -> tuple[DeepAnalysis, dict, int, str]:
    """Deep technical and Uzbek editorial analysis. Provider chosen by LLM_PROVIDER.

    Returns (deep_obj, raw_payload, latency_ms, model_tag).
    """
    truncated_text = text[:8000]
    prompt = EDITORIAL_PROMPT_TEMPLATE.format(
        title=title,
        source=source_name,
        text=truncated_text,
    )

    try:
        raw_payload, latency_ms, model_tag = editorial_chat(
            prompt=prompt,
            schema=DEEP_ANALYSIS_SCHEMA,
            num_predict=num_predict,
            client=client,
        )
        deep_obj = DeepAnalysis.model_validate(raw_payload)
        return deep_obj, raw_payload, latency_ms, model_tag
    except (ValidationError, json.JSONDecodeError) as exc:
        log.warning(
            "Editorial validation error on first attempt for '%s': %s. Retrying once with error.",
            title,
            exc,
        )
        recovery_prompt = (
            f"{prompt}\n\n"
            f"IMPORTANT: Your previous output failed schema validation with error:\n{exc}\n"
            "Please fix the error and return valid JSON conforming strictly to the schema."
        )
        raw_payload, latency_retry_ms, model_tag = editorial_chat(
            prompt=recovery_prompt,
            schema=DEEP_ANALYSIS_SCHEMA,
            num_predict=max(num_predict, 2000),
            client=client,
        )
        deep_obj = DeepAnalysis.model_validate(raw_payload)
        return deep_obj, raw_payload, latency_retry_ms, model_tag


def analyse_for_digest_logic(
    article_ids: list[int],
    client: httpx.Client | None = None,
) -> list[Analysis]:
    """Run deep editorial analysis over the selected items to produce Uzbek text."""
    articles = Article.objects.filter(id__in=article_ids).select_related("source")
    created_analyses: list[Analysis] = []

    for art in articles:
        # If editorial analysis already exists and is non-empty, avoid redundant LLM calls
        existing = (
            art.analyses.filter(stage=Analysis.Stage.EDITORIAL).order_by("-created_at").first()
        )
        if existing and existing.payload.get("summary_uz"):
            created_analyses.append(existing)
            continue

        try:
            deep_obj, raw_payload, latency_ms, model_tag = deep_analyze_text(
                title=art.title,
                source_name=art.source.name if art.source else "",
                text=art.extracted_text,
                num_predict=1500,
                client=client,
            )
            analysis = Analysis.objects.create(
                article=art,
                stage=Analysis.Stage.EDITORIAL,
                model_tag=model_tag,
                # MiMo exposes no model digest; only Ollama tags can drift silently.
                model_digest=(
                    fetch_model_digest(model_tag) if settings.LLM_PROVIDER != "mimo" else ""
                ),
                payload=raw_payload,
                latency_ms=latency_ms,
            )
            created_analyses.append(analysis)
            log.info("Editorial analysis completed for article %s (%s)", art.id, art.title)
        except Exception as exc:
            log.error(
                "Failed deep editorial analysis for article %s (%s): %s", art.id, art.title, exc
            )

    return created_analyses
