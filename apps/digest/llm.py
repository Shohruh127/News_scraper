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


class EditorialEn(BaseModel):
    """English analysis. Verified independently of translation (ADR-005).

    Four prose fields, and every one of them is published. Fields that were generated,
    translated and then rendered nowhere — `why_it_matters_en`, `uzbekistan_application_en`,
    `archetype`, `technical.hardware`, and the v1 `summary_en`/`leadership_en` — were removed
    on 2026-08-24. They cost output tokens on both LLM calls and split the model's attention
    across fields that never reached a reader.
    """

    headline_en: str = ""
    lead_en: str = ""
    body_1_en: str = ""
    kicker_en: str = ""

    technical: TechnicalDetails = Field(default_factory=TechnicalDetails)
    evidence_level: str = Field(default="vendor_claim_only")


class EditorialUz(BaseModel):
    """The published post, written in one call (2026-08-26 design).

    Replaces the EditorialEn -> Translation pair. The reader-facing fields are Uzbek; the
    `technical` block stays English because it is copied verbatim from the article and
    published as live links.
    """

    headline_uz: str = ""
    lead_uz: str = ""
    body_1_uz: str = ""
    kicker_uz: str = ""

    technical: TechnicalDetails = Field(default_factory=TechnicalDetails)
    evidence_level: str = Field(default="vendor_claim_only")


class Translation(BaseModel):
    """Uzbek rendering of the *_en fields. `technical` is not translated."""

    headline_uz: str = ""
    lead_uz: str = ""
    body_1_uz: str = ""
    kicker_uz: str = ""


# --- Editorial: English analysis ---------------------------------------------
# --- Editorial: English analysis (Stage 1: Fact Extraction) ------------------

EDITORIAL_EN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline_en": {"type": "string"},
        "lead_en": {"type": "string"},
        "body_1_en": {"type": "string"},
        "kicker_en": {"type": "string"},
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
    "required": [
        "headline_en",
        "lead_en",
        "body_1_en",
        "kicker_en",
    ],
}

#: The editorial instruction, chosen by the article's classified topic.
#:
#: Only the three sentence fields vary. `headline_en` is a label of at most eight words and
#: does not change with the kind of story, and neither do the style rules, the technical
#: block or the few-shot examples — those stay in the base prompt so six copies cannot drift
#: apart.
#:
#: The word counts are stated twice on purpose — here and in `## Output fields`. Measured
#: 2026-08-26: the risk block asked for "what the risk is and who it reaches" and produced a
#: 24-word lead against an 18-word cap. The content instruction sits before the field bullets
#: and wins, so the limit has to sit where the sentence is being decided. Keep the two copies
#: equal; `test_every_shape_block_states_its_word_limits` checks they are present, not that
#: they agree.
#:
#: This is not the archetype system removed on 2026-08-24. That one added output structure
#: (`*_details` blocks, six templates) with no data behind it — `license` was populated 0% of
#: the time. A shape adds no structure: the same four fields are produced for every group and
#: all four are always filled, so a group nobody hits is an unread dict entry.
SHAPE_GENERAL = "general"

SHAPE_BLOCKS: dict[str, str] = {
    SHAPE_GENERAL: """An article states many facts. Pick the lead by this order, first match wins:
  1. A named thing shipped or changed - and who shipped it.
  2. A measured result - and who measured it.
  3. A rule, policy or restriction - and who must comply with it.
The lead is at most 18 words. If the article announces something with nothing shipped and
nothing measured, say so plainly in the lead. Do not dress an announcement up as a release.""",
    "release": """This is a release. The reader is deciding whether to use the thing.
  lead_en    who shipped what (<= 18 words)
  body_1_en  the number or specification that matters most - parameters, context window,
             version, throughput, benchmark score (<= 20 words)
  kicker_en  what the reader can now do that they could not before (<= 8 words)
If nothing actually shipped and the article only announces an intention, say that plainly
in the lead rather than dressing it up as a release.""",
    "agent": """This is an agent, harness, framework or integration. Do NOT lead with the
benchmark score; lead with the wiring - what talks to what, with what in between - because
that is what tells the reader whether it fits their stack.
  lead_en    what connects to what (<= 18 words)
  body_1_en  the wiring itself. For a product: the transport, runtime or permission it
             needs, and which hosts support it. For a harness or framework: the parts and
             the loop between them. A score is not the wiring - name the part that earned
             it. (<= 20 words)
  kicker_en  what it replaces or removes the need for (<= 8 words)""",
    "risk": """This is a risk or its mitigation. Do NOT lead with who published the advisory;
lead with the risk itself, because that is what the reader needs first.
  lead_en    what the risk is and who it reaches (<= 18 words)
  body_1_en  its scope or mechanism - which versions, which models, what an attacker gets,
             or how the mitigation works (<= 20 words)
  kicker_en  what the reader should do about it (<= 8 words)""",
    "research": """This is a finding. The question is not what shipped but how much the evidence
supports the claim.
  lead_en    what is being claimed, and by whom (<= 18 words)
  body_1_en  how strong the evidence is - dataset, sample size, the baseline compared
             against, whether code or weights exist today (<= 20 words)
  kicker_en  what it changes if it holds (<= 8 words)
Do not report a promised artifact as a shipped one.""",
    "product": """This is a company shipping a product. The reader is deciding whether to try it,
so price and availability decide more than a benchmark does.
  lead_en    who launched what, and what it does (<= 18 words)
  body_1_en  whether it is available today and what it costs - free tier, waitlist, pricing,
             region limits (<= 20 words)
  kicker_en  who it is useful to (<= 8 words)""",
    "robotics": """This is physical embodiment. The reader's question is what the machine can
actually do in the world.
  lead_en    what the robot or system can physically do (<= 18 words)
  body_1_en  the physical numbers - speed, payload, autonomy duration, success rate, and
             where it was tested (<= 20 words)
  kicker_en  what it means for real deployment (<= 8 words)""",
}

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


#: The Uzbek editorial instruction, chosen by the article's classified topic.
#:
#: Keyed by the same names as SHAPE_BLOCKS so `shape_for()` selects both and TOPIC_SHAPES
#: stays the single topic map. Two maps would drift.
#:
#: Only the three sentence fields vary. `headline_uz` is a label of at most eight words and
#: does not change with the kind of story — the English agent block leaked a thesis into the
#: headline on 2026-08-26 precisely because it argued about what matters.
#:
#: The word counts are stated here as well as in the output-field definitions. The block is
#: read first and wins; a limit stated only later arrives after the sentence is decided.
UZ_BLOCKS: dict[str, str] = {
    SHAPE_GENERAL: """Maqolada ko'p fakt bo'ladi. Lead'ni shu tartibda tanla, birinchi mos
kelgani g'olib:
  1. Nomi bor narsa chiqdi yoki o'zgardi - va kim chiqargani.
  2. O'lchangan natija - va kim o'lchagani.
  3. Qoida, siyosat yoki cheklov - va kimga tegishli ekani.
Agar maqola faqat e'lon bo'lsa, hech narsa chiqmagan va o'lchanmagan bo'lsa - buni ochiq
ayt. E'lonni relizga o'xshatib yozma.
  lead_uz 14 so'z, body_1_uz 16 so'z, kicker_uz 8 so'z dan oshmasin.""",
    "release": """Bu - yangi chiqqan model yoki vosita. O'quvchi bitta savolga javob
qidiradi: "buni ishlatsam, menda nima yangilik bo'ladi?"
  lead_uz    kim nimani chiqardi (<= 14 so'z)
  body_1_uz  eng muhim raqam yoki xususiyat - parametr soni, kontekst hajmi, versiya,
             tezlik, benchmark natijasi (<= 16 so'z)
  kicker_uz  o'quvchi endi nima qila oladi, ilgari qila olmagan (<= 8 so'z)
Agar hech narsa chiqmagan, faqat va'da qilingan bo'lsa - buni lead'da ochiq ayt.""",
    "agent": """Bu - agent, harness, framework yoki integratsiya. O'quvchi "bu mening
tizimimga tushadimi?" deb o'qiydi. Benchmark bali bilan boshlama; ulanish bilan boshla -
nima nima bilan gaplashadi, orasida nima turadi.
  lead_uz    nima nima bilan ulanadi (<= 14 so'z)
  body_1_uz  ulanishning o'zi. Mahsulot bo'lsa: qaysi transport, runtime yoki ruxsat
             kerak, qaysi hostlar qo'llab-quvvatlaydi. Harness yoki framework bo'lsa:
             qismlari va ular orasidagi aylanma. Ball - ulanish emas; ballni keltirgan
             qismni nomla. (<= 16 so'z)
  kicker_uz  nimaning o'rnini bosadi yoki nimani keraksiz qiladi (<= 8 so'z)""",
    "risk": """Bu - zaiflik yoki uni bartaraf etish. O'quvchi "menga tegadimi va nima
qilishim kerak?" deb o'qiydi. Kim e'lon qilgani bilan boshlama; xavfning o'zi bilan boshla.
  lead_uz    xavf nima va kimga tegadi (<= 14 so'z)
  body_1_uz  qamrovi yoki mexanizmi - qaysi versiyalar, qaysi modellar, buzg'unchi nima
             qo'lga kiritadi, yoki himoya qanday ishlaydi (<= 16 so'z)
  kicker_uz  o'quvchi nima qilishi kerak (<= 8 so'z)""",
    "research": """Bu - topilma. Savol "nima chiqdi" emas, "dalil da'voni qanchalik
quvvatlaydi".
  lead_uz    nima da'vo qilinyapti va kim tomonidan (<= 14 so'z)
  body_1_uz  dalil qanchalik kuchli - qaysi ma'lumot to'plami, nechta namuna, nima bilan
             solishtirilgan, kod yoki weights bugun bormi (<= 16 so'z)
  kicker_uz  bu tasdiqlansa nima o'zgaradi (<= 8 so'z)
Va'da qilingan kodni chiqqan kod sifatida yozma.""",
    "product": """Bu - kompaniya mahsulot chiqardi. O'quvchi "sinab ko'ra olamanmi va
qanchaga?" deb o'qiydi. Bu yerda narx va mavjudlik benchmarkdan muhimroq.
  lead_uz    kim nimani ishga tushirdi va u nima qiladi (<= 14 so'z)
  body_1_uz  bugun mavjudmi va qancha turadi - bepul daraja, navbat, narx, hudud
             cheklovi (<= 16 so'z)
  kicker_uz  bu kimga foydali (<= 8 so'z)""",
    "robotics": """Bu - jismoniy tizim. O'quvchining savoli: "mashina haqiqiy dunyoda nima
qila oladi?"
  lead_uz    robot yoki tizim jismonan nima qila oladi (<= 14 so'z)
  body_1_uz  jismoniy raqamlar - tezlik, yuk, avtonom ishlash vaqti, muvaffaqiyat foizi,
             va qayerda sinalgani (<= 16 so'z)
  kicker_uz  bu haqiqiy joylashtirish uchun nimani anglatadi (<= 8 so'z)""",
}


EDITORIAL_UZ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline_uz": {"type": "string"},
        "lead_uz": {"type": "string"},
        "body_1_uz": {"type": "string"},
        "kicker_uz": {"type": "string"},
        "evidence_level": {
            "type": "string",
            "enum": ["vendor_claim_only", "multiple_evidence"],
        },
        "technical": EDITORIAL_EN_SCHEMA["properties"]["technical"],
    },
    "required": ["headline_uz", "lead_uz", "body_1_uz", "kicker_uz"],
}


EDITORIAL_UZ_PROMPT = """You are writing one Telegram post for a channel read by working
AI engineers and technical decision-makers in Uzbekistan. They skim. Give them the fact,
not the announcement.

The four reader-facing fields are written in UZBEK (Latin script). The `technical` object
is copied from the article and stays in ENGLISH. Return JSON only.

## Shape
A headline label plus EXACTLY THREE sentences: lead_uz, body_1_uz, kicker_uz.
One sentence per field. A field holding two sentences is wrong.

## What this story needs
The block below decides the content of the three sentences only. It does not change
headline_uz, which stays a plain label of what happened for every kind of story - never a
claim about what matters. Where the block gives a word count, that count is the limit.

{block}

## Output fields
- headline_uz: a short label naming what happened, AT MOST 8 UZBEK WORDS. NOT a sentence -
  no final full stop, no verb required. Only the first word and proper nouns are
  capitalised; English Title Case is wrong.
- lead_uz: one complete Uzbek sentence with a finite verb, AT MOST 14 UZBEK WORDS. Who did
  what. Do not end it with a particle such as 'ham' or 'esa'. It must not repeat the
  headline.
- body_1_uz: one Uzbek sentence, AT MOST 16 UZBEK WORDS. NEVER invent a number, and never
  restate what the lead already said. One fact, not a list.
- kicker_uz: one short Uzbek sentence, AT MOST 8 UZBEK WORDS, saying what this changes for
  a developer. No cliches, no hype, no restating the lead.
- evidence_level: 'vendor_claim_only' or 'multiple_evidence'
- technical: an object with what_was_built, architecture, license, repo_url, api_url,
  install, benchmarks, limitations, local_deployable. Copy each value VERBATIM from the
  article, in English. If the article does not state it, return an empty string - EXCEPT
  local_deployable, which is a boolean: return false when the article does not say the
  thing can be run locally. Never guess a URL, a licence name, or an install command -
  these are published as live links.

## Style rules
1. NO FLUFF / NO HYPE: never use 'inqilobiy', 'ulkan yutuq', 'hayratlanarli',
   'o'yinni o'zgartiruvchi', 'ma'lum bo'lishicha', 'xabar berishicha'.
2. Prefer the specific to the general.
3. ONE SENTENCE PER FIELD.
4. Keep every number, version and price exactly as the article states it.
5. Model names, product names, company names, benchmark names and these terms stay in
   English: model, API, agent, framework, benchmark, context, token, inference, latency,
   prompt, repo, open-source, weights, open-weight, toolchain.
6. Write literary Uzbek. Invented or broken words are forbidden. Never transliterate a
   term that has a real Uzbek equivalent, and never calque one that should stay English.
7. Plain text only: no markdown bold, no asterisks, no backticks, no list markers.

## Namunalar

Misol 1 - model relizi, raqamlar bor:
Maqola: "Mistral AI released Mistral-Large-2 with 123B parameters and 128k context,
scoring 84% on MMLU. Weights are on GitHub under Apache-2.0."
Chiquvchi JSON:
{{
  "headline_uz": "Mistral-Large-2 open-weight modeli chiqdi",
  "lead_uz": "Mistral jamoasi 123B parametrli Mistral-Large-2 modelini ochiq taqdim etdi.",
  "body_1_uz": "Model 128k kontekstga ega va MMLU testida 84% natija ko'rsatdi.",
  "kicker_uz": "API shartnomasisiz kuchli model.",
  "evidence_level": "vendor_claim_only",
  "technical": {{
    "what_was_built": "An open-weight large language model.",
    "architecture": "123B parameters, 128k context window",
    "license": "Apache-2.0",
    "repo_url": "https://github.com/mistralai/mistral-large-2",
    "api_url": "", "install": "", "benchmarks": "84% on MMLU", "limitations": "",
    "local_deployable": true
  }}
}}

Misol 2 - mahsulot e'loni, raqam yo'q. body_1_uz nima qilishiga e'tibor ber: maqolada
raqam yo'q, shuning uchun u mexanizmni nomlaydi. Raqam to'qimaydi va lead'ni takrorlamaydi.
Maqola: "Replit is opening a free tier of its agent, powered by GPT-5.6 Luna. The free
tier runs planning and experimentation in the same workspace where code is written. No
pricing or usage limits were published."
Chiquvchi JSON:
{{
  "headline_uz": "Replit bepul agent darajasini ochdi",
  "lead_uz": "Replit GPT-5.6 Luna asosidagi kodlash agentining bepul darajasini ochdi.",
  "body_1_uz": "Rejalashtirish va tajriba kod yoziladigan bir ish maydonida ishlaydi.",
  "kicker_uz": "Agentni sinash endi byudjet talab qilmaydi.",
  "evidence_level": "vendor_claim_only",
  "technical": {{
    "what_was_built": "A free tier of a coding agent.",
    "architecture": "", "license": "", "repo_url": "", "api_url": "", "install": "",
    "benchmarks": "",
    "limitations": "No pricing or usage limits were published.",
    "local_deployable": false
  }}
}}

ARTICLE
Title: {title}
Source: {source}
---
{text}
"""


EDITORIAL_EN_PROMPT = """You are writing one Telegram post for a channel read by working
AI engineers and technical decision-makers in Uzbekistan. They skim. Give them the fact,
not the announcement. Return JSON only.

## Shape
A headline label plus EXACTLY THREE sentences: lead_en, body_1_en, kicker_en.
One sentence per field. A field holding two sentences is wrong.

## What this story needs
The block below decides the content of the three sentences only. It does not change
headline_en, which stays a plain label of what happened for every kind of story - never a
claim about what matters. Where the block gives a word count, that count is the limit.

{shape}

## Output fields
- headline_en: a short label naming what happened, at most 8 words. NOT a sentence and
  NOT a summary - no final full stop, no verb required. 'Qwen 3.8 27B tops the
  open-weight index' is right; 'Alibaba has released a new model today' is wrong.
- lead_en: one complete sentence with a finite main verb, AT MOST 18 WORDS. Who did what,
  chosen by the order above. A sentence, not a noun phrase and not a fragment; 'X is a
  tool that does Y' is wrong. Do not end it with a particle or a dangling conjunction.
  It must not repeat the headline - the headline names the event, the lead says who did it.
- body_1_en: the single most specific verifiable fact the article states, AT MOST 20 WORDS.
  A number, version, price or benchmark when the article gives one; the concrete mechanism
  when it does not. NEVER invent a number, and never restate what the lead already said.
  One fact, not a list of every condition it holds under.
- kicker_en: one short closing remark, at most 8 words, saying what this changes for a
  developer. No cliches, no hype, no restating the lead.
- evidence_level: 'vendor_claim_only' or 'multiple_evidence'
- technical: an object with what_was_built, architecture, license, repo_url, api_url,
  install, benchmarks, limitations, local_deployable. Copy each value VERBATIM from the
  article. If the article does not state it, return an empty string - EXCEPT
  local_deployable, which is a boolean: return false when the article does not say the
  thing can be run locally. Never guess a URL, a licence name, or an install command -
  these are published as live links.

## Style rules
1. NO FLUFF / NO HYPE: never use words like 'revolutionary', 'game-changer', 'powerful'.
2. Prefer the specific to the general. 'cut CI time from 40 to 6 minutes' beats
   'improved performance'.
3. ONE SENTENCE PER FIELD.

## Example 1 - a release, with numbers
Input: "Mistral AI released Mistral-Large-2 with 123B parameters and 128k context,
scoring 84% on MMLU. Weights are on GitHub under Apache-2.0."
Output JSON:
{{
  "headline_en": "Mistral-Large-2 ships with open weights",
  "lead_en": "Mistral released Mistral-Large-2, an open-weight frontier model.",
  "body_1_en": "The model has 123B parameters, a 128k context window, and scores 84% on MMLU.",
  "kicker_en": "A frontier model without an API contract.",
  "evidence_level": "vendor_claim_only",
  "technical": {{
    "what_was_built": "An open-weight large language model.",
    "architecture": "123B parameters, 128k context window",
    "license": "Apache-2.0",
    "repo_url": "https://github.com/mistralai/mistral-large-2",
    "api_url": "", "install": "", "benchmarks": "84% on MMLU", "limitations": "",
    "local_deployable": true
  }}
}}

## Example 2 - a product announcement, no numbers stated
Note what body_1_en does here: the article gives no figure, so it names the mechanism
instead. It does not invent one, and it does not repeat the lead.
Input: "Replit is opening a free tier of its agent, powered by GPT-5.6 Luna. The free
tier runs planning and experimentation in the same workspace where code is written. No
pricing or usage limits were published."
Output JSON:
{{
  "headline_en": "Replit opens a free agent tier",
  "lead_en": "Replit opened a free tier of its coding agent, running on GPT-5.6 Luna.",
  "body_1_en": "Planning and experimentation run in the same workspace as the code.",
  "kicker_en": "Trying an agent no longer needs a budget.",
  "evidence_level": "vendor_claim_only",
  "technical": {{
    "what_was_built": "A free tier of a coding agent.",
    "architecture": "", "license": "", "repo_url": "", "api_url": "", "install": "",
    "benchmarks": "",
    "limitations": "No pricing or usage limits were published.",
    "local_deployable": false
  }}
}}

ARTICLE
Title: {title}
Source: {source}
---
{text}
"""


# --- Editorial: Uzbek translation (Stage 2: Copywriter) ----------------------

TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline_uz": {"type": "string"},
        "lead_uz": {"type": "string"},
        "body_1_uz": {"type": "string"},
        "kicker_uz": {"type": "string"},
    },
    "required": [
        "headline_uz",
        "lead_uz",
        "body_1_uz",
        "kicker_uz",
    ],
}

#: The prose fields of a post, in reading order. Every one of them is published; nothing is
#: translated that a reader will not see.
COMMON_TRANSLATED_FIELDS = (
    "headline_en",
    "lead_en",
    "body_1_en",
    "kicker_en",
)

#: Technical fields that are prose and therefore translated. `hardware` was dropped on
#: 2026-08-24 with the other fields `item_appendix.html` does not render.
TECHNICAL_PROSE_FIELDS = (
    "what_was_built",
    "architecture",
    "limitations",
    "benchmarks",
)


def technical_fields(payload: dict) -> dict[str, str]:
    """The technical block's prose, suffixed `_en` so the dynamic schema yields `_uz`."""
    block = payload.get("technical") or {}
    return {
        f"{name}_en": block[name]
        for name in TECHNICAL_PROSE_FIELDS
        if isinstance(block.get(name), str) and block[name].strip()
    }


def translation_schema_for(fields: dict) -> dict:
    """A translation schema carrying exactly the fields the English stage produced."""
    props = {(k[:-3] + "_uz" if k.endswith("_en") else k): {"type": "string"} for k in fields}
    return {"type": "object", "properties": props, "required": list(props)}


TRANSLATION_PROMPT = (
    "Quyidagi faktlar asosida O'zbekistondagi AI muhandislari uchun qisqa Telegram posti "
    "yoz (Latin script). Faqat JSON qaytar.\n\n"
    "## Post tuzilishi\n"
    "Post: headline_uz sarlavhasi, keyin AYNAN UCHTA gap - lead_uz, body_1_uz va kicker_uz. "
    "Har bir maydon aynan bitta gap (headline_uz bundan mustasno - u sarlavha, gap emas). "
    "Gaplarni sana, so'zlarni emas.\n\n"
    "## Qat'iy qoidalar:\n"
    "1. headline_uz: qisqa sarlavha, ko'pi bilan 8 so'z. Bu GAP EMAS - nuqta qo'yilmaydi, "
    "kesim shart emas. Faqat birinchi harf va atoqli otlar bosh harfda; inglizcha Title "
    "Case ishlatilmaydi.\n"
    "2. lead_uz: aynan 1 ta to'liq gap. Kim nima qilganini bildiradi va kesim bilan "
    "tugaydi. Uni yuklama bilan tugatma ('ham', 'esa') va gapni chala qoldirma.\n"
    "3. body_1_uz: aynan 1 ta gap. Eng aniq, tekshirib bo'ladigan faktni beradi.\n"
    "4. kicker_uz: aynan 1 ta qisqa gap, ko'pi bilan 8 so'z. Bu fakt dasturchi uchun nimani "
    "o'zgartirishini aytadi. Lead'ni takrorlama, shior yozma.\n"
    "5. Barcha raqamlar, versiyalar (masalan, v0.32.12, $12K, 95.3%) tarjimada aniq "
    "saqlansin.\n"
    "6. TAQIQLANGAN SO'ZLAR: 'inqilobiy', 'ulkan yutuq', 'hayratlanarli',\n"
    "   'o'yinni o'zgartiruvchi', 'ma'lum bo'lishicha', 'xabar berishicha'.\n\n"
    # PROVISIONAL Uzbek. The owner is the native speaker and these two examples are his
    # to write - see docs/superpowers/specs/2026-08-20-post-format-gold-examples.md
    # sections 1-2. They obey the rules above so the prompt is not self-contradictory in
    # the meantime, but replace them with his gold text before this reaches the channel.
    "## Few-Shot Namunalar:\n"
    "Misol 1 (Model relizi):\n"
    'Kiruvchi faktlar: {{"headline_en": "Mistral-Large-2 ships with open weights", '
    '"lead_en": "Mistral released Mistral-Large-2 with 123B parameters.", '
    '"body_1_en": "The model features 128k context and 84% score on MMLU.", '
    '"kicker_en": "A frontier model without an API contract."}}\n'
    "Chiquvchi JSON:\n"
    "{{\n"
    '  "headline_uz": "Mistral-Large-2 ochiq kod bilan chiqdi",\n'
    '  "lead_uz": "Mistral jamoasi 123B parametrli yangi Mistral-Large-2 modelini '
    'ochiq taqdim etdi.",\n'
    '  "body_1_uz": "Model 128k kontekstga ega bo\'lib, MMLU testida 84% natija '
    "ko'rsatgan.\",\n"
    '  "kicker_uz": "API shartnomasisiz kuchli model."\n'
    "}}\n\n"
    "Misol 2 (Dasturiy vosita / Keys):\n"
    'Kiruvchi faktlar: {{"headline_en": "Asana replaces legacy testing with Codex", '
    '"lead_en": "Asana replaced outdated testing system in 2 weeks for $12K with OpenAI Codex.", '
    '"body_1_en": "The project finished in two weeks instead of estimated 5 years and $6M.", '
    '"kicker_en": "Coding agents migrate legacy systems in weeks."}}\n'
    "Chiquvchi JSON:\n"
    "{{\n"
    '  "headline_uz": "Asana test tizimini Codex bilan yangiladi",\n'
    '  "lead_uz": "Asana jamoasi OpenAI Codex yordamida eskirgan sinov tizimini ikki '
    'haftada $12K ga almashtirdi.",\n'
    '  "body_1_uz": "Avval 5 yil va $6M deb taxmin qilingan ish ikki haftada '
    'yakunlandi.",\n'
    '  "kicker_uz": "Eski tizimlar endi haftalarda ko\'chiriladi."\n'
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

The readers want engineering, not the business around it.

Answer relevant=true if ANY ONE of these holds. They are independent — one is enough, and
you do not need the others.

  A. The headline names a specific model, tool, library, protocol, API, dataset or product.
  B. It reports something shipped, released, opened, updated, deprecated or priced.
  C. It reports an operational or engineering action taken with real systems — disrupting,
     detecting, mitigating, hardening, migrating, scaling — even when nothing is named.
  D. It reports a concrete technical finding, benchmark or measurement.

Answer relevant=false only when none of A-D holds and the headline is about the business
around the work: money raised, valuations, acquisitions or share deals; hiring, appointments
or someone speaking at an event; partnerships and collaborations; policy positions, lobbying,
regulation or court cases; opinion, speculation or "will X happen" questions; company
retrospectives and anniversary posts; advertising and monetisation; consumer lifestyle
gadgets.

On rule A, do not also ask whether the headline is "about" the named thing in the right way.
A question about it, a complaint about it, or a report of a problem with it all count. That
judgement belongs to the classification stage, which reads the article.

  "Who is behind the stealth model Ox Alpha?"        A: names Ox Alpha       -> true
  "Instinct's AI assistant raises privacy concerns"  A: names the assistant  -> true
  "Disrupting a covert influence campaign"           C: an action taken      -> true
  "Nvidia partners with a data centre developer"     none of A-D             -> false
  "Hugging Face in talks to be acquired for $13B"    none of A-D             -> false

If one of A-D holds but you cannot tell how significant it is, keep it: letting one extra
through costs one call, while dropping a real release loses it for good.

reason: at most 10 words, naming what decided it.

Title: {title}
Source: {source}
"""


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
    )


def gateway_chat(
    model: str,
    prompt: str,
    schema: dict | None = None,
    timeout: int | None = None,
    max_tokens: int = 1500,
    client: httpx.Client | None = None,
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
        client=client,
    )


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
    raise RuntimeError(f"unknown LLM provider {provider!r}; expected 'gateway' or 'mimo'")


def _dispatch(
    provider: str,
    tier: str,
    prompt: str,
    schema: dict,
    num_predict: int,
    client: httpx.Client | None = None,
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
        )

    if not settings.MIMO_API_KEY or not settings.MIMO_BASE_URL:
        raise RuntimeError("provider is mimo but MIMO_API_KEY/MIMO_BASE_URL are unset")
    return mimo_chat(
        model=model,
        prompt=prompt,
        schema=schema,
        timeout=settings.MIMO_TIMEOUT,
        max_tokens=num_predict,
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
    return _dispatch(
        provider=settings.CLASSIFIER_PROVIDER,
        tier=tier,
        prompt=prompt,
        schema=schema,
        num_predict=num_predict,
        client=client,
    )


def editorial_chat(
    prompt: str,
    schema: dict,
    num_predict: int,
    client: httpx.Client | None = None,
    provider: str | None = None,
    tier: str = TIER_DEEP,
) -> ChatResult:
    """Dispatch an editorial call.

    The two editorial stages are routed independently — see EDITORIAL_EN_PROVIDER and
    TRANSLATION_PROVIDER. Triage and classification have their own switch via
    classifier_chat.

    `tier` matters: translation belongs on the fast tier. Measured 2026-08-17, the fast
    model lost 0/7 numbers while the deep one garbled Uzbek in the first digest. Defaulting
    every editorial call to the deep tier would send translation to the wrong one.
    """
    return _dispatch(
        provider=provider or settings.LLM_PROVIDER,
        tier=tier,
        prompt=prompt,
        schema=schema,
        num_predict=num_predict,
        client=client,
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
) -> list[Analysis]:
    """Read each article and write its Uzbek post in one call.

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
        if existing and existing.payload.get("lead_uz"):
            created.append(existing)
            continue

        try:
            result = editorial_uz_for_article(art, client=client)

            violations = translation_gates.validate_against_source(
                article_title=art.title,
                article_text=art.extracted_text or "",
                uz_fields=result.payload,
                technical=result.payload.get("technical"),
            )
            if violations:
                log.warning(
                    "Uzbek gates failed for article %s: %s. Retrying once.", art.id, violations
                )
                result = _retry_editorial_uz(art, violations, result, client)

            created.append(_record_analysis(art, Analysis.Stage.EDITORIAL_UZ, result))
            log.info("Uzbek post done for article %s", art.id)
        except Exception as exc:
            log.error("Uzbek editorial failed for article %s (%s): %s", art.id, art.title, exc)

    return created


def _retry_editorial_uz(art, violations, first, client):
    """One retry naming the violations, then keep whichever attempt is clean.

    A permanently failing article is dropped by compose_and_publish, which filters
    candidates on a usable row; DIGEST_SELECT_MARGIN covers the hole.
    """
    block_key = shape_for(_classified_topic(art))
    retry_prompt = (
        EDITORIAL_UZ_PROMPT.format(
            block=UZ_BLOCKS[block_key],
            title=art.title,
            source=art.source.name if art.source else "",
            text=(art.extracted_text or "")[:8000],
        )
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
    still = translation_gates.validate_against_source(
        article_title=art.title,
        article_text=art.extracted_text or "",
        uz_fields=retry.payload,
        technical=retry.payload.get("technical"),
    )
    if still:
        log.error("Uzbek gates failed permanently for article %s: %s", art.id, still)
    return _combine(first, retry)


def editorial_uz_for_article(article: Article, client: httpx.Client | None = None) -> ChatResult:
    """Read the article and write the Uzbek post in one call (2026-08-26 design).

    Phase 1: nothing calls this from the pipeline. `analyse_for_digest_logic` still runs the
    two-stage flow, so this can be measured against it before anything is replaced.

    No Analysis row is written here. The caller decides whether the result is worth storing,
    which keeps the eval command from polluting the pipeline's data.
    """
    block_key = shape_for(_classified_topic(article))
    result = _editorial_call(
        prompt=EDITORIAL_UZ_PROMPT.format(
            block=UZ_BLOCKS[block_key],
            title=article.title,
            source=article.source.name if article.source else "",
            text=(article.extracted_text or "")[:8000],
        ),
        schema=EDITORIAL_UZ_SCHEMA,
        model_cls=EditorialUz,
        num_predict=settings.EDITORIAL_NUM_PREDICT,
        client=client,
        provider=settings.EDITORIAL_UZ_PROVIDER,
        tier=TIER_DEEP,
    )
    # The prompt forbids markdown; normalize mechanically rather than trusting the model.
    return result._replace(payload=_normalize_uz_payload(result.payload))


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


def _editorial_call(
    prompt: str,
    schema: dict,
    model_cls,
    num_predict: int,
    client=None,
    provider: str | None = None,
    tier: str = TIER_DEEP,
) -> ChatResult:
    """One editorial call with validation retry and empty technical block check (T1.17).

    Every attempt's cost is folded into the returned result, so a stage that retried twice
    is not recorded as having cost one call.
    """
    first: ChatResult | None = None
    try:
        first = editorial_chat(prompt, schema, num_predict, client, provider, tier)
        model_cls.model_validate(first.payload)
        result = first
    except (ValidationError, json.JSONDecodeError) as exc:
        log.warning("Editorial validation failed, retrying once: %s", exc)
        recovery = (
            f"{prompt}\n\nIMPORTANT: your previous output failed validation:\n{exc}\n"
            "Return valid JSON conforming strictly to the schema."
        )
        retry = editorial_chat(recovery, schema, max(num_predict, 2000), client, provider, tier)
        model_cls.model_validate(retry.payload)
        result = _combine(first, retry)

    # Post-check for empty lead_en in English editorial
    if model_cls is EditorialEn and not result.payload.get("lead_en", "").strip():
        log.warning("Empty lead_en in English editorial, retrying once.")
        recovery = (
            f"{prompt}\n\nIMPORTANT: The 'lead_en' field was empty. "
            "You must provide a non-empty 1-sentence lead with action verb link anchor."
        )
        try:
            retry = editorial_chat(recovery, schema, max(num_predict, 2000), client, provider, tier)
            model_cls.model_validate(retry.payload)
            if retry.payload.get("lead_en", "").strip():
                result = _combine(result, retry)
        except Exception as exc:
            # The failed attempt still cost tokens, but the provider raised before
            # reporting them, so there is nothing to add.
            log.debug("lead_en recovery attempt failed: %s", exc)

    return result
