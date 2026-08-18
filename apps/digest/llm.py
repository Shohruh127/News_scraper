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
from pydantic import BaseModel, Field, ValidationError, create_model
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import translation_gates
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
    """Legacy single-step editorial (strategy C). Superseded by EditorialEn + Translation."""

    summary_uz: str
    why_it_matters_uz: str
    leadership_uz: str
    technical: TechnicalDetails
    uzbekistan_application_uz: str
    evidence_level: str = Field(default="vendor_claim_only")


#: The shape of a post. Boundary definitions live in CONTENT_SCHEMA.md §5 and are quoted into
#: EDITORIAL_EN_PROMPT verbatim, because measurement showed they carry the accuracy: without
#: them the model scored 0/6 and filled six irrelevant blocks; with them, 5/6 and none.
ARCHETYPES = (
    "release",
    "agent_protocol",
    "risk_hardening",
    "policy",
    "research",
    "company_product",
)


class ReleaseDetails(BaseModel):
    what_changed_en: str = ""
    benchmarks_en: str = ""
    availability_en: str = ""


class AgentProtocolDetails(BaseModel):
    connects_en: str = ""
    deployment_en: str = ""


class RiskHardeningDetails(BaseModel):
    """No severity enum. Of 11 stored security articles, none carried a CVSS score, so a
    three-value enum with no "not stated" option would be invented nine times in eleven.
    A stated CVE or severity belongs inside `risk_en`, quoted rather than classified."""

    risk_en: str = ""
    mitigation_en: str = ""
    residual_en: str = ""


class PolicyDetails(BaseModel):
    who_issued_en: str = ""
    who_must_comply_en: str = ""
    deadline_en: str = ""


class ResearchDetails(BaseModel):
    #: Deliberately not `evidence_level`, which is a frozen enum meaning something else.
    claim_en: str = ""
    evidence_strength_en: str = ""
    reproducible_en: str = ""


class CompanyProductDetails(BaseModel):
    what_they_do_en: str = ""
    availability_en: str = ""


class EditorialEn(BaseModel):
    """English analysis. Verified independently of translation (ADR-005)."""

    headline_en: str
    summary_en: str
    why_it_matters_en: str
    leadership_en: str
    uzbekistan_application_en: str
    technical: TechnicalDetails
    evidence_level: str = Field(default="vendor_claim_only")
    archetype: str = "release"
    release_details: ReleaseDetails | None = None
    agent_protocol_details: AgentProtocolDetails | None = None
    risk_hardening_details: RiskHardeningDetails | None = None
    policy_details: PolicyDetails | None = None
    research_details: ResearchDetails | None = None
    company_product_details: CompanyProductDetails | None = None


class Translation(BaseModel):
    """Uzbek rendering of the *_en fields. `technical` is not translated."""

    headline_uz: str
    summary_uz: str
    why_it_matters_uz: str
    leadership_uz: str
    uzbekistan_application_uz: str


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

# --- Editorial: English analysis ---------------------------------------------
# Separated from translation (ADR-005). English is verified on its own so that a poor
# post can be attributed to comprehension or to translation, never to both at once.

EDITORIAL_EN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline_en": {"type": "string"},
        "summary_en": {"type": "string"},
        "why_it_matters_en": {"type": "string"},
        "leadership_en": {"type": "string"},
        "uzbekistan_application_en": {"type": "string"},
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
        "evidence_level": {
            "type": "string",
            "enum": ["vendor_claim_only", "multiple_evidence"],
        },
        "archetype": {"type": "string", "enum": list(ARCHETYPES)},
        "release_details": {
            "type": "object",
            "properties": {
                "what_changed_en": {"type": "string"},
                "benchmarks_en": {"type": "string"},
                "availability_en": {"type": "string"},
            },
        },
        "agent_protocol_details": {
            "type": "object",
            "properties": {
                "connects_en": {"type": "string"},
                "deployment_en": {"type": "string"},
            },
        },
        "risk_hardening_details": {
            "type": "object",
            "properties": {
                "risk_en": {"type": "string"},
                "mitigation_en": {"type": "string"},
                "residual_en": {"type": "string"},
            },
        },
        "policy_details": {
            "type": "object",
            "properties": {
                "who_issued_en": {"type": "string"},
                "who_must_comply_en": {"type": "string"},
                "deadline_en": {"type": "string"},
            },
        },
        "research_details": {
            "type": "object",
            "properties": {
                "claim_en": {"type": "string"},
                "evidence_strength_en": {"type": "string"},
                "reproducible_en": {"type": "string"},
            },
        },
        "company_product_details": {
            "type": "object",
            "properties": {
                "what_they_do_en": {"type": "string"},
                "availability_en": {"type": "string"},
            },
        },
    },
    "required": [
        "headline_en",
        "summary_en",
        "why_it_matters_en",
        "leadership_en",
        "uzbekistan_application_en",
        "technical",
        "evidence_level",
        "archetype",
    ],
}

#: Quoted verbatim from CONTENT_SCHEMA.md §5. Measured: 0/6 without, 5/6 with.
ARCHETYPE_DEFINITIONS = (
    "## Choose exactly one archetype\n"
    "release          A named product or model shipped a new version. A changelog, a release\n"
    "                 note, a version number. This is the default for any version bump.\n"
    "agent_protocol   A protocol or framework for connecting tools to models, where the news\n"
    "                 IS the connection mechanism. Not a runtime that happens to run agents.\n"
    "risk_hardening   A risk, a weakness, or work done to reduce one. There must be something\n"
    "                 that can go wrong and someone acting on it.\n"
    "policy           A rule issued by a government or standards body, with someone obliged to\n"
    "                 comply. Pricing is not policy.\n"
    "research         A method or a finding with a claim and evidence, not a shipped artifact.\n"
    "company_product  A company entering a market or making a commercial launch, where the\n"
    "                 company is the news rather than the version.\n\n"
    "Fill ONLY the detail block for the archetype you chose. Leave every other block absent.\n"
    "Omit any field whose value is not stated in the article. Never infer a severity.\n\n"
)

EDITORIAL_EN_PROMPT = (
    "You are a senior editor for a daily AI-engineering digest read by engineering "
    "leaders and AI engineers in Uzbekistan.\n\n"
    "Write the English analysis of the article below. Return JSON only.\n\n"
    + ARCHETYPE_DEFINITIONS
    + "## Voice — this is a news post, not a book report\n"
    'Never refer to "the article", "this paper", "the post", or "the author". Write about '
    "the news itself. Wrong: \"The article describes a new model.\" Right: \"Qwen released "
    'a 2.4T open-weight model."\n'
    "Lead with what happened. No preamble, no scene-setting.\n\n"
    "## Length — enforced, not advisory\n"
    "- headline_en: under 80 characters, states the news, no clickbait\n"
    "- summary_en: exactly 1-2 sentences. This is the only body text a reader sees before "
    "tapping to expand, so it must carry the news on its own. Lead with the concrete fact "
    "— what shipped, from whom, with which number that matters.\n"
    "- why_it_matters_en: exactly 1-2 sentences\n"
    "- leadership_en: exactly 1-2 sentences, for a non-engineer decision maker\n"
    "- uzbekistan_application_en: exactly 1 sentence, or empty if there is no honest one\n\n"
    "## Grounding\n"
    "Every claim must come from the article text. If a detail is absent — license, "
    'benchmark number, repo URL, hardware requirement — leave that field as "". Never '
    "infer, never fill from background knowledge. An empty field is correct; an invented "
    "one is a defect.\n"
    "- Headline rule: every proper noun in headline_en must appear verbatim in the article text. "
    "Never invent or hallucinate names.\n"
    "- local_deployable: true only if weights or code can actually be self-hosted\n"
    '- evidence_level: "vendor_claim_only" unless the article cites independent validation\n\n'
    "## Language\n"
    "Write in English only. Do not emit any other script or language, including single "
    "words. This output is translated in a later stage.\n\n"
    "ARTICLE\n"
    "Title: {title}\n"
    "Source: {source}\n"
    "---\n"
    "{text}\n"
)

# --- Editorial: Uzbek translation --------------------------------------------

TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline_uz": {"type": "string"},
        "summary_uz": {"type": "string"},
        "why_it_matters_uz": {"type": "string"},
        "leadership_uz": {"type": "string"},
        "uzbekistan_application_uz": {"type": "string"},
    },
    "required": [
        "headline_uz",
        "summary_uz",
        "why_it_matters_uz",
        "leadership_uz",
        "uzbekistan_application_uz",
    ],
}

#: Fields translated for every post regardless of archetype.
COMMON_TRANSLATED_FIELDS = (
    "headline_en",
    "summary_en",
    "why_it_matters_en",
    "leadership_en",
    "uzbekistan_application_en",
)


def archetype_fields(payload: dict) -> dict[str, str]:
    """The chosen archetype's detail block, flattened to top-level keys.

    Flat rather than nested because `translation_gates.validate_translation` joins the values
    of both dicts, and a nested dict stringifies into its own repr. The gates were built
    generic over field names; only the schema was not.
    """
    block = payload.get(f"{payload.get('archetype', '')}_details") or {}
    return {k: v for k, v in block.items() if isinstance(v, str) and v.strip()}


def translation_schema_for(fields: dict) -> dict:
    """A translation schema carrying exactly the fields the English stage produced.

    Deriving it rather than fixing it removes the opportunity to fill an irrelevant block
    instead of instructing against it.
    """
    props = {
        (k[:-3] + "_uz" if k.endswith("_en") else k): {"type": "string"} for k in fields
    }
    return {"type": "object", "properties": props, "required": list(props)}


TRANSLATION_PROMPT = (
    "Translate the fields below into Uzbek (Latin script). Return JSON only.\n\n"
    "## You are translating, not writing\n"
    "Do not add information, opinions, or sentences that are not in the source. Do not "
    "remove any. Keep the same number of sentences per field.\n\n"
    "## Keep in English\n"
    "Model names, product names, company names, metric names, file formats, and "
    "established technical terms: model, API, agent, framework, open-weight, weights, "
    "checkpoint, benchmark, context, token, inference, latency, fine-tuning, quantization, "
    "repo, MoE, embedding, prompt.\n"
    "Do not transliterate these into Cyrillic-style Uzbek spellings. 'framework' stays "
    "'framework', not 'freymvork'.\n\n"
    "## Language\n"
    "Output Uzbek Latin script only. Never emit Chinese, Russian, or any other script — "
    "not even a single character. If a term has no natural Uzbek equivalent, keep the "
    "English word.\n\n"
    "## Natural Uzbek\n"
    "Translate meaning, not word order. The result must read as though written by an "
    "Uzbek technical journalist, not as a machine rendering of English syntax.\n\n"
    "FIELDS TO TRANSLATE\n"
    "{fields}\n"
)

# Lightweight prompt for the fast triage pass (T1.17). Drops heavy taxonomy definitions
# to make the 185-run triage pass fast and focused on noise rejection.
TRIAGE_PROMPT_TEMPLATE = (
    "You are a fast technical triage editor for an AI engineering news digest.\n\n"
    "Classify the article below to filter out noise. Return JSON only conforming to the schema.\n\n"
    "Rules:\n"
    "- If the article contains no technical AI substance (e.g. general business, executive news, "
    "marketing, consumer gadgets, non-AI), primary_topic MUST be 'irrelevant'.\n"
    "- Score novelty, evidence, and production_readiness integers from 1 to 10.\n"
    "- If it is AI technical news, choose the primary_topic and maturity that best describes it.\n"
    "\n"
    "ARTICLE\n"
    "Title: {title}\n"
    "Source: {source}\n"
    "---\n"
    "{text}\n"
)

# Verbatim enum definitions and boundaries from CONTENT_SCHEMA.md §2 and §3 for deep classification
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


_model_digest_cache: dict[str, str] = {}


def fetch_model_digest(model_name: str, client: httpx.Client | None = None) -> str:
    """Fetch or resolve the 64-char model digest from Ollama tags.

    Cached per process per model (T1.15): /api/tags is called once per model, not
    once per classification. The cache lives for the process lifetime, which is correct
    because a model digest only changes when the operator explicitly pulls a new version,
    and the worker process would be restarted after that.
    """
    if model_name in _model_digest_cache:
        return _model_digest_cache[model_name]

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
                    digest = m.get("digest", "")
                    _model_digest_cache[model_name] = digest
                    return digest
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
    provider: str | None = None,
    ollama_model: str | None = None,
) -> tuple[dict, int, str]:
    """Dispatch an editorial call to a provider.

    Returns (payload, latency_ms, model_tag). Triage and classification always run on
    local Ollama; only the two editorial stages are routed, and they are routed
    independently — see EDITORIAL_EN_PROVIDER and TRANSLATION_PROVIDER.

    `ollama_model` matters: translation belongs on the fast model. gemma4:latest lost
    0/7 numbers in measurement, while gemma4:31b is the model that garbled Uzbek in the
    first digest. Defaulting the whole Ollama branch to the deep model would have sent
    translation to the wrong one.
    """
    if provider is None:
        provider = settings.LLM_PROVIDER
    if provider == "mimo":
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

    model = ollama_model or settings.OLLAMA_DEEP_MODEL
    payload, ms = ollama_chat(
        model=model,
        prompt=prompt,
        schema=schema,
        timeout=settings.OLLAMA_DEEP_TIMEOUT,
        num_predict=num_predict,
        client=client,
    )
    return payload, ms, model


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
    if any(d in url for d in PAPER_DOMAINS):
        return Maturity.PAPER_ONLY
    if article.source and article.source.connector == "hf":
        # The hf connector fetches the daily-papers feed and nothing else.
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
        article.id, claimed, ceiling, article.canonical_url[:80],
    )
    payload["maturity"] = ceiling
    payload["maturity_capped_from"] = claimed
    return payload


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
        return False, "Paper domain: excluded from ranking by maturity, so never triaged"

    return True, ""


def classify_text(
    title: str,
    source_name: str,
    text: str,
    model: str,
    timeout: int,
    num_predict: int = 400,
    client: httpx.Client | None = None,
    prompt_template: str = CLASSIFICATION_PROMPT_TEMPLATE,
) -> tuple[Classification, dict, int, str]:
    """Classify article text with Pydantic validation and 1-attempt recovery.

    Returns (classification_obj, raw_payload, latency_ms, digest).
    """
    truncated_text = text[:8000]
    prompt = prompt_template.format(
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
    """Triage logic using the fast model and lightweight triage prompt (T1.17)."""
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
            prompt_template=TRIAGE_PROMPT_TEMPLATE,
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

    # The source decides what a paper is; the model is not asked to re-derive it.
    raw_payload = apply_maturity_ceiling(article, raw_payload)
    classification = Classification.model_validate(raw_payload)

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

    raw_payload = apply_maturity_ceiling(article, raw_payload)
    classification = Classification.model_validate(raw_payload)

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



def analyse_for_digest_logic(
    article_ids: list[int],
    client: httpx.Client | None = None,
) -> list[Analysis]:
    """Two-stage editorial: English analysis for all items, then translation for all.

    Batched by stage rather than per article, so the model loads once per stage. Returns
    the translation analyses, since those are what rendering consumes.
    """
    articles = list(Article.objects.filter(id__in=article_ids).select_related("source"))

    # --- Stage 1: English -----------------------------------------------------
    en_by_article: dict[int, Analysis] = {}
    for art in articles:
        existing = (
            art.analyses.filter(stage=Analysis.Stage.EDITORIAL_EN).order_by("-created_at").first()
        )
        if existing and existing.payload.get("summary_en"):
            en_by_article[art.id] = existing
            continue
        try:
            payload, latency_ms, model_tag = _editorial_call(
                prompt=EDITORIAL_EN_PROMPT.format(
                    title=art.title,
                    source=art.source.name if art.source else "",
                    text=art.extracted_text[:8000],
                ),
                schema=EDITORIAL_EN_SCHEMA,
                model_cls=EditorialEn,
                num_predict=1500,
                client=client,
                provider=settings.EDITORIAL_EN_PROVIDER,
            )
            en_by_article[art.id] = _record(art, Analysis.Stage.EDITORIAL_EN, model_tag,
                                            payload, latency_ms,
                                            settings.EDITORIAL_EN_PROVIDER)
            log.info("English editorial done for article %s", art.id)
        except Exception as exc:
            log.error("English editorial failed for article %s (%s): %s", art.id, art.title, exc)

    # --- Stage 2: translation -------------------------------------------------
    created: list[Analysis] = []
    for art in articles:
        en = en_by_article.get(art.id)
        if en is None:
            continue
        existing = (
            art.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ).order_by("-created_at").first()
        )
        if existing and existing.payload.get("summary_uz"):
            created.append(existing)
            continue
        try:
            fields = {k: en.payload.get(k, "") for k in COMMON_TRANSLATED_FIELDS}
            fields.update(archetype_fields(en.payload))
            uz_schema = translation_schema_for(fields)
            uz_model = create_model(
                "TranslationDynamic",
                **{k: (str, ...) for k in uz_schema["properties"]},
            )
            payload, latency_ms, model_tag = _editorial_call(
                prompt=TRANSLATION_PROMPT.format(
                    fields=json.dumps(fields, ensure_ascii=False, indent=2)
                ),
                schema=uz_schema,
                model_cls=uz_model,
                num_predict=settings.TRANSLATION_NUM_PREDICT,
                client=client,
                provider=settings.TRANSLATION_PROVIDER,
                ollama_model=settings.OLLAMA_FAST_MODEL,
            )

            # Translation quality gates (T1.16)
            violations = translation_gates.validate_translation(fields, payload)
            if violations:
                log.warning(
                    "Translation gates failed for article %s: %s. Retrying once.",
                    art.id,
                    violations,
                )
                fields_json = json.dumps(fields, ensure_ascii=False, indent=2)
                retry_prompt = (
                    f"{TRANSLATION_PROMPT.format(fields=fields_json)}\n\n"
                    "IMPORTANT: Your previous output failed translation quality gates:\n"
                    + "\n".join(f"- {v}" for v in violations)
                    + "\nPlease fix these specific errors and return valid JSON."
                )
                try:
                    retry_payload, retry_ms, retry_model = editorial_chat(
                        prompt=retry_prompt,
                        schema=uz_schema,
                        num_predict=settings.TRANSLATION_NUM_PREDICT,
                        client=client,
                        provider=settings.TRANSLATION_PROVIDER,
                        ollama_model=settings.OLLAMA_FAST_MODEL,
                    )
                    uz_model.model_validate(retry_payload)
                    retry_violations = translation_gates.validate_translation(fields, retry_payload)
                    if retry_violations:
                        log.error(
                            "Translation gates failed permanently for article %s: %s.",
                            art.id,
                            retry_violations,
                        )
                        continue
                    payload = retry_payload
                    latency_ms += retry_ms
                    model_tag = retry_model
                except Exception as exc:
                    log.error(
                        "Translation gate recovery failed for article %s: %s.",
                        art.id,
                        exc,
                    )
                    continue

            created.append(_record(art, Analysis.Stage.EDITORIAL_UZ, model_tag,
                                   payload, latency_ms, settings.TRANSLATION_PROVIDER))
            log.info("Uzbek translation done for article %s", art.id)
        except Exception as exc:
            log.error("Translation failed for article %s (%s): %s", art.id, art.title, exc)

    return created


def _record(article, stage, model_tag: str, payload: dict, latency_ms: int,
            provider: str) -> Analysis:
    return Analysis.objects.create(
        article=article,
        stage=stage,
        model_tag=model_tag,
        # MiMo exposes no digest; only Ollama tags can be repointed silently. The provider
        # is per stage, so the global LLM_PROVIDER cannot answer this: with
        # LLM_PROVIDER=mimo it blanked the digest on translation rows, which are produced
        # by Ollama and are exactly the ones a repointed tag would corrupt unnoticed.
        model_digest=fetch_model_digest(model_tag) if provider != "mimo" else "",
        payload=payload,
        latency_ms=latency_ms,
    )


def _editorial_call(prompt: str, schema: dict, model_cls, num_predict: int, client=None,
                    provider: str | None = None, ollama_model: str | None = None):
    """One editorial call with validation retry and empty technical block check (T1.17)."""
    try:
        payload, ms, model_tag = editorial_chat(
            prompt, schema, num_predict, client, provider, ollama_model
        )
        model_cls.model_validate(payload)
    except (ValidationError, json.JSONDecodeError) as exc:
        log.warning("Editorial validation failed, retrying once: %s", exc)
        recovery = (
            f"{prompt}\n\nIMPORTANT: your previous output failed validation:\n{exc}\n"
            "Return valid JSON conforming strictly to the schema."
        )
        payload, ms, model_tag = editorial_chat(
            recovery, schema, max(num_predict, 2000), client, provider, ollama_model
        )
        model_cls.model_validate(payload)

    # Post-check for empty technical.what_was_built (T1.17)
    tech_built = payload.get("technical", {}).get("what_was_built", "").strip()
    if model_cls is EditorialEn and not tech_built:
        log.warning("Empty what_was_built in English editorial, retrying once.")
        recovery = (
            f"{prompt}\n\nIMPORTANT: The 'what_was_built' field in 'technical' was empty. "
            "You must provide a non-empty description of what was built or released."
        )
        try:
            retry_payload, retry_ms, retry_model = editorial_chat(
                recovery, schema, max(num_predict, 2000), client, provider, ollama_model
            )
            model_cls.model_validate(retry_payload)
            if retry_payload.get("technical", {}).get("what_was_built", "").strip():
                payload = retry_payload
                ms += retry_ms
                model_tag = retry_model
        except Exception as exc:
            log.debug("what_was_built recovery attempt failed: %s", exc)

    return payload, ms, model_tag
