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

from . import artifacts, post_format, translation_gates
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

    # v2 fields
    lead_en: str = ""
    body_1_en: str = ""
    body_2_en: str = ""

    # v1 fields (kept for backward compatibility & historical records)
    headline_en: str = ""
    summary_en: str = ""
    leadership_en: str = ""

    why_it_matters_en: str = ""
    uzbekistan_application_en: str = ""
    technical: TechnicalDetails = Field(default_factory=TechnicalDetails)
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

    # v2 fields
    lead_uz: str = ""
    body_1_uz: str = ""
    body_2_uz: str = ""

    # v1 fields
    headline_uz: str = ""
    summary_uz: str = ""
    leadership_uz: str = ""

    why_it_matters_uz: str = ""
    uzbekistan_application_uz: str = ""


# --- Editorial: English analysis ---------------------------------------------
# --- Editorial: English analysis (Stage 1: Fact Extraction) ------------------

EDITORIAL_EN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lead_en": {"type": "string"},
        "body_1_en": {"type": "string"},
        "body_2_en": {"type": "string"},
        "why_it_matters_en": {"type": "string"},
        "uzbekistan_application_en": {"type": "string"},
        "archetype": {"type": "string", "enum": list(ARCHETYPES)},
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
                "hardware": {"type": "string"},
                "benchmarks": {"type": "string"},
                "limitations": {"type": "string"},
                "local_deployable": {"type": "boolean"},
            },
        },
    },
    "required": [
        "lead_en",
        "body_1_en",
        "why_it_matters_en",
    ],
}

EDITORIAL_EN_PROMPT = (
    "Extract the high-signal facts from the AI engineering article below for a short "
    "Telegram news post. Return JSON only.\n\n"
    "## Length\n"
    "The post is 2 to 3 sentences: lead_en, body_1_en and an optional body_2_en. Each of "
    "those fields is EXACTLY ONE sentence. Count sentences, not words.\n\n"
    "## Output Fields:\n"
    "- lead_en: one complete sentence with a finite main verb. Who did what. It must be "
    "a sentence, not a noun phrase and not a fragment; 'X is a tool that does Y' is "
    "wrong. Do not end it with a particle or a dangling conjunction. No markdown, no "
    "headline.\n"
    "- body_1_en: one sentence carrying the key number, benchmark, or technical spec.\n"
    "- body_2_en: one sentence of cause or context, or an empty string.\n"
    "- why_it_matters_en: one sentence on developer or engineering impact.\n"
    "- uzbekistan_application_en: one sentence, or an empty string.\n"
    "- archetype: 'release', 'company_product', 'research', 'agent_protocol', "
    "'risk_hardening', or 'policy'\n"
    "- evidence_level: 'vendor_claim_only' or 'multiple_evidence'\n"
    "- technical: an object with what_was_built, architecture, license, repo_url, "
    "api_url, install, hardware, benchmarks, limitations, local_deployable.\n"
    "  Copy each value VERBATIM from the article. If the article does not state it, "
    "return an empty string. Never guess a URL, a licence name, or an install command "
    "- these are published as live links.\n\n"
    "## Style Rules:\n"
    "1. NO FLUFF / NO HYPE: never use words like 'revolutionary', 'game-changer', "
    "'powerful'.\n"
    "2. MANDATORY NUMBERS: body_1_en must carry a concrete number.\n"
    "3. ONE SENTENCE PER FIELD. A field with two sentences is wrong.\n\n"
    "## Few-Shot Example:\n"
    'Input: "Mistral AI released Mistral-Large-2 with 123B parameters and 128k context, '
    'scoring 84% on MMLU. Weights are on GitHub under Apache-2.0."\n'
    "Output JSON:\n"
    "{{\n"
    '  "lead_en": "Mistral released Mistral-Large-2, an open-weight successor to its '
    'previous frontier model.",\n'
    '  "body_1_en": "The model has 123B parameters, a 128k context window, and scores '
    '84% on MMLU.",\n'
    '  "body_2_en": "Apache-2.0 licensing places it alongside the open-weight models it '
    'competes with.",\n'
    '  "why_it_matters_en": "Teams can self-host a frontier-class model without an API '
    'contract.",\n'
    '  "uzbekistan_application_en": "Local teams can run it on their own hardware for '
    'multilingual work.",\n'
    '  "archetype": "release",\n'
    '  "evidence_level": "vendor_claim_only",\n'
    '  "technical": {{\n'
    '    "what_was_built": "An open-weight large language model.",\n'
    '    "architecture": "123B parameters, 128k context window",\n'
    '    "license": "Apache-2.0",\n'
    '    "repo_url": "https://github.com/mistralai/mistral-large-2",\n'
    '    "api_url": "",\n'
    '    "install": "",\n'
    '    "hardware": "",\n'
    '    "benchmarks": "84% on MMLU",\n'
    '    "limitations": "",\n'
    '    "local_deployable": true\n'
    "  }}\n"
    "}}\n\n"
    "ARTICLE\n"
    "Title: {title}\n"
    "Source: {source}\n"
    "---\n"
    "{text}\n"
)

# --- Editorial: Uzbek translation (Stage 2: Copywriter) ----------------------

TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lead_uz": {"type": "string"},
        "body_1_uz": {"type": "string"},
        "body_2_uz": {"type": "string"},
        "why_it_matters_uz": {"type": "string"},
        "uzbekistan_application_uz": {"type": "string"},
    },
    "required": [
        "lead_uz",
        "body_1_uz",
        "why_it_matters_uz",
    ],
}

#: Fields translated for every post regardless of archetype.
COMMON_TRANSLATED_FIELDS = (
    "lead_en",
    "body_1_en",
    "body_2_en",
    "why_it_matters_en",
    "uzbekistan_application_en",
)

#: Technical fields that are prose and therefore translated.
TECHNICAL_PROSE_FIELDS = (
    "what_was_built",
    "architecture",
    "limitations",
    "benchmarks",
    "hardware",
)


def technical_fields(payload: dict) -> dict[str, str]:
    """The technical block's prose, suffixed `_en` so the dynamic schema yields `_uz`."""
    block = payload.get("technical") or {}
    return {
        f"{name}_en": block[name]
        for name in TECHNICAL_PROSE_FIELDS
        if isinstance(block.get(name), str) and block[name].strip()
    }


def archetype_fields(payload: dict) -> dict[str, str]:
    """The chosen archetype's detail block, flattened to top-level keys."""
    block = payload.get(f"{payload.get('archetype', '')}_details") or {}
    return {k: v for k, v in block.items() if isinstance(v, str) and v.strip()}


def translation_schema_for(fields: dict) -> dict:
    """A translation schema carrying exactly the fields the English stage produced."""
    props = {(k[:-3] + "_uz" if k.endswith("_en") else k): {"type": "string"} for k in fields}
    return {"type": "object", "properties": props, "required": list(props)}


TRANSLATION_PROMPT = (
    "Quyidagi faktlar asosida O'zbekistondagi AI muhandislari uchun qisqa Telegram posti "
    "yoz (Latin script). Faqat JSON qaytar.\n\n"
    "## Post tuzilishi\n"
    "Post 2 yoki 3 ta gapdan iborat: lead_uz, body_1_uz va ixtiyoriy body_2_uz. "
    "Har bir maydon AYNAN BITTA gap. Gaplarni sana, so'zlarni emas.\n"
    "Postni yakuniy izoh yoki xulosa jumlasi bilan tugatma - faqat faktlar.\n\n"
    "## Qat'iy qoidalar:\n"
    "1. lead_uz: aynan 1 ta to'liq gap. Kim nima qilganini bildiradi va kesim bilan "
    "tugaydi. Uni yuklama bilan tugatma ('ham', 'esa') va gapni chala qoldirma.\n"
    "2. body_1_uz: aynan 1 ta gap. Asosiy raqam, mezon yoki texnik faktni ifodalaydi.\n"
    "3. body_2_uz: aynan 1 ta gap - sabab yoki kontekst - yoki bo'sh satr.\n"
    "4. Barcha raqamlar, versiyalar (masalan, v0.32.12, $12K, 95.3%) tarjimada aniq "
    "saqlansin.\n"
    "5. TAQIQLANGAN SO'ZLAR: 'inqilobiy', 'ulkan yutuq', 'hayratlanarli',\n"
    "   'o'yinni o'zgartiruvchi', 'ma'lum bo'lishicha', 'xabar berishicha'.\n\n"
    # PROVISIONAL Uzbek. The owner is the native speaker and these two examples are his
    # to write - see docs/superpowers/specs/2026-08-20-post-format-gold-examples.md
    # sections 1-2. They obey the rules above so the prompt is not self-contradictory in
    # the meantime, but replace them with his gold text before this reaches the channel.
    "## Few-Shot Namunalar:\n"
    "Misol 1 (Model relizi):\n"
    'Kiruvchi faktlar: {{"lead_en": "Mistral released Mistral-Large-2 with 123B '
    'parameters.", "body_1_en": "The model features 128k context and 84% score on '
    'MMLU.", "body_2_en": "Apache-2.0 licensing allows commercial use."}}\n'
    "Chiquvchi JSON:\n"
    "{{\n"
    '  "lead_uz": "Mistral jamoasi 123B parametrli yangi Mistral-Large-2 modelini '
    'ochiq taqdim etdi.",\n'
    '  "body_1_uz": "Model 128k kontekstga ega bo\'lib, MMLU testida 84% natija '
    "ko'rsatgan.\",\n"
    '  "body_2_uz": "Apache-2.0 litsenziyasi uni tijorat loyihalarida ham ishlatishga '
    'ruxsat beradi."\n'
    "}}\n\n"
    "Misol 2 (Dasturiy vosita / Keys):\n"
    'Kiruvchi faktlar: {{"lead_en": "Asana replaced outdated testing system in 2 weeks '
    'for $12K with OpenAI Codex.", "body_1_en": "The project finished in two weeks '
    'instead of estimated 5 years and $6M.", "body_2_en": "Parallel coding agents did '
    'the migration."}}\n'
    "Chiquvchi JSON:\n"
    "{{\n"
    '  "lead_uz": "Asana jamoasi OpenAI Codex yordamida eskirgan sinov tizimini ikki '
    'haftada $12K ga almashtirdi.",\n'
    '  "body_1_uz": "Avval 5 yil va $6M deb taxmin qilingan ish ikki haftada '
    'yakunlandi.",\n'
    '  "body_2_uz": "Ko\'chirishni yollangan jamoa emas, parallel kodlovchi agentlar '
    'bajardi."\n'
    "}}\n\n"
    "## Lug'at va atamalar (Glossary):\n"
    "- 'US', 'U.S.', 'United States' -> 'AQSH'\n"
    "- 'classifier' -> 'klassifikator'\n"
    "- 'mitigate', 'fix', 'address', 'resolve' -> 'bartaraf etadi'\n"
    "- 'vulnerability' -> 'zaiflik'\n"
    "- 'bypass' -> 'chetlab o'tish'\n"
    "- 'security' -> 'xavfsizlik'\n\n"
    "## Grammatika va to'g'ri yozish:\n"
    "1. Faqat adabiy va to'g'ri o'zbek tili so'zlaridan foydalanilsin. Soxta, noto'g'ri yoki "
    "buzilgan so'zlar (masalan: 'sinifikatori', 'takib') qat'iyan taqiqlanadi.\n"
    "2. Har bir gap mantiqiy va grammatik jihatdan to'liq bo'lsin (Ega + To'ldiruvchi + Kesim).\n\n"
    "## Ingliz tilida qoladigan atamalar\n"
    "Model nomlari, mahsulot nomlari, kompaniya nomlari, mezon nomlari va quyidagilar: "
    "model, API, agent, framework, benchmark, context, token, inference, latency, "
    "prompt, repo, open-source, weights, open-weight, toolchain.\n\n"
    "## Oddiy matn formati\n"
    "Hech qaysi maydonda markdown qalinlik (**so'z**), yulduzcha, teskari qo'shtirnoq "
    "yoki ro'yxat belgilaridan foydalanma. Toza oddiy matn qaytar.\n\n"
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
) -> tuple[dict, int]:
    """Chat completion against any OpenAI-compatible endpoint. Returns (payload, latency_ms).

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
    """OpenAI-compatible chat completion against MiMo. Returns (parsed_payload, latency_ms)."""
    return _openai_chat(
        base_url=settings.MIMO_BASE_URL,
        api_key=settings.MIMO_API_KEY,
        model=model,
        prompt=prompt,
        schema=schema,
        timeout=timeout,
        max_tokens=max_tokens,
        client=client,
    )


def gateway_chat(
    model: str,
    prompt: str,
    schema: dict | None = None,
    timeout: int | None = None,
    max_tokens: int = 1500,
    client: httpx.Client | None = None,
) -> tuple[dict, int]:
    """Chat completion against the internal LLM gateway. Returns (payload, latency_ms).

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
        client=client,
    )


def _gateway_alias(ollama_model: str | None) -> str:
    """Map an Ollama tag onto a gateway tier alias.

    The fast/deep distinction already travels through this module as an Ollama tag, so the
    gateway branch reads that rather than introducing a second way to say the same thing.
    Anything that is not explicitly the fast model gets the smart tier.
    """
    if ollama_model and ollama_model == settings.OLLAMA_FAST_MODEL:
        return settings.GATEWAY_FAST_MODEL
    return settings.GATEWAY_SMART_MODEL


def _mimo_model_for(ollama_model: str | None) -> str:
    """Pick the MiMo model matching the tier the caller asked for."""
    if ollama_model and ollama_model == settings.OLLAMA_FAST_MODEL:
        return settings.MIMO_FAST_MODEL
    return settings.MIMO_DEEP_MODEL


def classifier_chat(
    model: str,
    prompt: str,
    schema: dict,
    timeout: int,
    num_predict: int,
    client: httpx.Client | None = None,
) -> tuple[dict, int, str]:
    """Dispatch a triage or classification call. Returns (payload, latency_ms, model_tag).

    `model` is always an Ollama tag: it is what the caller knows, and it also encodes the
    fast/deep tier the other providers need. The returned tag is the one that actually ran,
    so provenance stays truthful when the call did not go to Ollama.

    CLASSIFIER_PROVIDER covers both stages together because they share a backend; there is
    no measurement saying triage and classification want different providers.
    """
    provider = settings.CLASSIFIER_PROVIDER
    if provider == "gateway":
        alias = _gateway_alias(model)
        payload, ms = gateway_chat(
            model=alias,
            prompt=prompt,
            schema=schema,
            max_tokens=num_predict,
            client=client,
        )
        return payload, ms, alias
    if provider == "mimo":
        mimo_model = _mimo_model_for(model)
        payload, ms = mimo_chat(
            model=mimo_model,
            prompt=prompt,
            schema=schema,
            timeout=settings.MIMO_TIMEOUT,
            max_tokens=num_predict,
            client=client,
        )
        return payload, ms, mimo_model

    payload, ms = ollama_chat(
        model=model,
        prompt=prompt,
        schema=schema,
        timeout=timeout,
        num_predict=num_predict,
        client=client,
    )
    return payload, ms, model


def editorial_chat(
    prompt: str,
    schema: dict,
    num_predict: int,
    client: httpx.Client | None = None,
    provider: str | None = None,
    ollama_model: str | None = None,
) -> tuple[dict, int, str]:
    """Dispatch an editorial call to a provider.

    Returns (payload, latency_ms, model_tag). The two editorial stages are routed
    independently — see EDITORIAL_EN_PROVIDER and TRANSLATION_PROVIDER. Triage and
    classification have their own switch, CLASSIFIER_PROVIDER, via classifier_chat.

    `ollama_model` matters: translation belongs on the fast model. gemma4:latest lost
    0/7 numbers in measurement, while gemma4:31b is the model that garbled Uzbek in the
    first digest. Defaulting the whole Ollama branch to the deep model would have sent
    translation to the wrong one.
    """
    if provider is None:
        provider = settings.LLM_PROVIDER
    if provider == "gateway":
        alias = _gateway_alias(ollama_model)
        payload, ms = gateway_chat(
            model=alias,
            prompt=prompt,
            schema=schema,
            max_tokens=num_predict,
            client=client,
        )
        return payload, ms, alias
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
    model: str,
    timeout: int,
    num_predict: int = 400,
    client: httpx.Client | None = None,
    prompt_template: str = CLASSIFICATION_PROMPT_TEMPLATE,
) -> tuple[Classification, dict, int, str, str]:
    """Classify article text with Pydantic validation and 1-attempt recovery.

    Returns (classification_obj, raw_payload, latency_ms, digest, model_tag). `model_tag`
    is what actually served the call, which is not `model` unless CLASSIFIER_PROVIDER is
    ollama; the caller records it, so provenance must not assume the requested tag.
    """
    truncated_text = text[:8000]
    prompt = prompt_template.format(
        title=title,
        source=source_name,
        text=truncated_text,
    )

    # Only Ollama exposes /api/tags, and only an Ollama tag can be repointed silently, so
    # the drift-detection digest is meaningless for the other providers.
    on_ollama = settings.CLASSIFIER_PROVIDER == "ollama"
    digest = fetch_model_digest(model, client=client) if on_ollama else ""
    latency_ms = 0

    try:
        raw_payload, latency_ms, model_tag = classifier_chat(
            model=model,
            prompt=prompt,
            schema=CLASSIFICATION_SCHEMA,
            timeout=timeout,
            num_predict=num_predict,
            client=client,
        )
        classification = Classification.model_validate(raw_payload)
        return classification, raw_payload, latency_ms, digest, model_tag
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
        raw_payload, latency_retry_ms, model_tag = classifier_chat(
            model=model,
            prompt=recovery_prompt,
            schema=CLASSIFICATION_SCHEMA,
            timeout=timeout,
            num_predict=max(num_predict, 1500),
            client=client,
        )
        classification = Classification.model_validate(raw_payload)
        return classification, raw_payload, latency_ms + latency_retry_ms, digest, model_tag


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
        classification, raw_payload, latency_ms, digest, model_tag = classify_text(
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
        model_tag=model_tag,
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
        classification, raw_payload, latency_ms, digest, model_tag = classify_text(
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
        model_tag=model_tag,
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
        if existing and (existing.payload.get("lead_en") or existing.payload.get("summary_en")):
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
                num_predict=settings.EDITORIAL_NUM_PREDICT,
                client=client,
                provider=settings.EDITORIAL_EN_PROVIDER,
            )
            en_by_article[art.id] = _record(
                art,
                Analysis.Stage.EDITORIAL_EN,
                model_tag,
                payload,
                latency_ms,
                settings.EDITORIAL_EN_PROVIDER,
            )
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
        if existing and (existing.payload.get("lead_uz") or existing.payload.get("summary_uz")):
            created.append(existing)
            continue
        try:
            fields = {
                k: en.payload.get(k, "")
                for k in COMMON_TRANSLATED_FIELDS
                if isinstance(en.payload.get(k), str) and en.payload[k].strip()
            }
            fields.update(archetype_fields(en.payload))
            fields.update(technical_fields(en.payload))
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

            payload = _normalize_uz_payload(payload)

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
                    retry_payload = _normalize_uz_payload(retry_payload)
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

            created.append(
                _record(
                    art,
                    Analysis.Stage.EDITORIAL_UZ,
                    model_tag,
                    payload,
                    latency_ms,
                    settings.TRANSLATION_PROVIDER,
                )
            )
            log.info("Uzbek translation done for article %s", art.id)
        except Exception as exc:
            log.error("Translation failed for article %s (%s): %s", art.id, art.title, exc)

    return created


def _record(
    article, stage, model_tag: str, payload: dict, latency_ms: int, provider: str
) -> Analysis:
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


def _editorial_call(
    prompt: str,
    schema: dict,
    model_cls,
    num_predict: int,
    client=None,
    provider: str | None = None,
    ollama_model: str | None = None,
):
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

    # Post-check for empty lead_en in English editorial
    lead_en = payload.get("lead_en", "").strip()
    if model_cls is EditorialEn and not lead_en:
        log.warning("Empty lead_en in English editorial, retrying once.")
        recovery = (
            f"{prompt}\n\nIMPORTANT: The 'lead_en' field was empty. "
            "You must provide a non-empty 1-sentence lead with action verb link anchor."
        )
        try:
            retry_payload, retry_ms, retry_model = editorial_chat(
                recovery, schema, max(num_predict, 2000), client, provider, ollama_model
            )
            model_cls.model_validate(retry_payload)
            if retry_payload.get("lead_en", "").strip():
                payload = retry_payload
                ms += retry_ms
                model_tag = retry_model
        except Exception as exc:
            log.debug("lead_en recovery attempt failed: %s", exc)

    return payload, ms, model_tag
