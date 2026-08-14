# Content Schema

Version: 1.0
Date: 2026-08-14
Status: **frozen** — changing an enum value requires an ADR

This is the single source of truth for every LLM contract in the system. `llm.py` must
match this document exactly. Prompts must embed the definitions below **verbatim** —
M0.1 measured that the definitions, not the model size, decide accuracy (~1/10 topic
accuracy without them, ~9.5/10 with them).

---

## 1. Why definitions are written this way

Ollama constrains decoding with XGrammar, so a value is always **inside** the enum. It is
never guaranteed to be the **right** enum member. The grammar enforces shape; only the
definition enforces meaning.

Every definition below therefore states **how the category differs from the one it is
most often confused with**, not what it is in isolation. A self-describing definition
("frontier_models: articles about frontier models") gives the model nothing to
discriminate on, and it will pick an arbitrary attractor — measured behaviour, not theory.

---

## 2. `primary_topic`

Exactly one value. Twelve members.

| Value | Definition, with its boundary |
|---|---|
| `frontier_models` | A **specific named model** is released, updated, or given new capabilities. Not a technique — that is `new_approaches`. Not a tool that runs models — that is `production_engineering`. |
| `ai_agents` | A system where an LLM **takes actions through tools**: agent frameworks, tool calling, MCP/A2A, multi-agent orchestration, coding or browser agents. Not any paper that merely uses the word "agent". Not a tool release that happens to support agents — that is `production_engineering`. |
| `new_approaches` | A new **method, architecture, training technique, or inference technique**. This is the default for research papers. Not a named model release. |
| `speech_voice` | Audio is an input or an output: STT, TTS, voice agents, diarization, audio models. |
| `robotics` | **Physical embodiment**: robots, control policies, embodied AI. |
| `fintech` | Financial technology: payments, banking, lending, financial infrastructure. |
| `govtech` | Government digital services and public administration systems. |
| `production_engineering` | Infrastructure, serving, deployment, and **developer tooling — including releases and changelogs of such tools**. An Ollama, vLLM or LangGraph changelog belongs here even when it mentions agents or models. |
| `startups` | A **company shipping a deployed commercial product**. Not any article that mentions a company. Not a model release from a large lab — that is `frontier_models`. |
| `technical_talks` | A **recorded presentation**: conference talk, demo, technical video. |
| `safety_security` | Alignment, jailbreaks, model or agent security, permissions, red-teaming. Not general research into model behaviour — that is `new_approaches`. |
| `irrelevant` | **Everything else**: executive appointments, funding rounds, partnerships, marketing, opinion pieces, consumer gadgets, general business news. |

Mandatory rule in every prompt:

> If the article contains no technical substance, `primary_topic` MUST be `irrelevant`.
> Do not force a technical category onto a business story.

### Observed confusions this table is built to prevent

Measured in M0.1 with the definition-free prompt:

| Article | Wrong answer | Correct | Trap closed by |
|---|---|---|---|
| "OpenAI appoints Chief Revenue Officer" | `ai_agents`, `startups` | `irrelevant` | the mandatory rule |
| PixSDS (a text-to-3D method) | `safety_security` | `new_approaches` | `safety_security` boundary |
| Instruction tuning and confidence | `safety_security` | `new_approaches` | same |
| TailBooster (data augmentation) | `ai_agents` | `new_approaches` | `ai_agents` boundary |
| Ollama v0.32.10 changelog | `new_approaches`, `startups` | `production_engineering` | "including changelogs" |

---

## 3. `maturity`

What actually exists **right now**. Six members, ordered from strongest to weakest evidence.

| Value | Definition, with its boundary |
|---|---|
| `production_deployment` | Running in a **named real organisation**, with reported results. Not "could be deployed". |
| `live_product` | A publicly usable product or API **available today**. A changelog for an already-shipped tool is `live_product`, not `production_deployment`. |
| `reproducible_open_source` | Code or weights are **downloadable today at a working link**. |
| `public_pilot` | Limited preview, waitlist, or restricted access. |
| `announcement_only` | Announced, but **nothing usable has been released**. |
| `paper_only` | A research paper or preprint. |

### The `paper_only` / `reproducible_open_source` boundary

This must appear verbatim in the prompt. M0.1 measured a regression here on **both**
models: two arXiv papers that merely mentioned releasing code were labelled
`reproducible_open_source`.

> A paper is `paper_only` even when it promises code, says "code will be released",
> or links a repository that does not yet exist. `reproducible_open_source` requires
> a link that resolves to real artifacts **today**. Excellent results do not raise
> maturity — only shipped artifacts do.

### Publication gate

`announcement_only` and `paper_only` are **hard-excluded** from every digest
(`PROJECT_PLAN.md` §5, T1.6). They may be stored, never published.

---

## 4. Classification schema — triage and classify

Used by both the 8B triage pass and the 31B classification pass. Identical schema; the
difference is only which model runs it.

```json
{
  "type": "object",
  "properties": {
    "primary_topic": {"type": "string", "enum": [
      "frontier_models", "ai_agents", "new_approaches", "speech_voice", "robotics",
      "fintech", "govtech", "production_engineering", "startups", "technical_talks",
      "safety_security", "irrelevant"]},
    "maturity": {"type": "string", "enum": [
      "production_deployment", "live_product", "reproducible_open_source",
      "public_pilot", "announcement_only", "paper_only"]},
    "novelty":              {"type": "integer", "minimum": 1, "maximum": 10},
    "evidence":             {"type": "integer", "minimum": 1, "maximum": 10},
    "production_readiness": {"type": "integer", "minimum": 1, "maximum": 10},
    "reason":               {"type": "string"}
  },
  "required": ["primary_topic", "maturity", "novelty", "evidence",
               "production_readiness", "reason"]
}
```

### `relevant` was removed

Version 0 of this schema carried a `relevant` boolean. M0.1 measured it doing no work:
the 8B returned `true` for 8 of 10 articles, **including ones whose own `reason` field
said the article had no technical content**.

The signal already exists twice over — `primary_topic == "irrelevant"` and the numeric
scores, both of which were well calibrated in the same run. A third, weaker signal only
adds a way to disagree with itself.

**Rejection is decided by `primary_topic == "irrelevant"` plus the score thresholds.**

### Numeric dimensions

These were well calibrated even without definitions, but the definitions stay in the
prompt because they cost nothing on the 8B.

| Field | 1 | 10 |
|---|---|---|
| `novelty` | rehash of known news | genuinely new capability or result |
| `evidence` | vendor claim only | reproducible artifacts: weights, repo, independent eval |
| `production_readiness` | paper or announcement | deployed and documented |

---

## 5. Deep-analysis schema — 31B, top 3–5 items only

```json
{
  "type": "object",
  "properties": {
    "summary_uz":       {"type": "string"},
    "why_it_matters_uz":{"type": "string"},
    "leadership_uz":    {"type": "string"},
    "technical": {
      "type": "object",
      "properties": {
        "what_was_built":  {"type": "string"},
        "architecture":    {"type": "string"},
        "license":         {"type": "string"},
        "repo_url":        {"type": "string"},
        "api_url":         {"type": "string"},
        "hardware":        {"type": "string"},
        "install":         {"type": "string"},
        "benchmarks":      {"type": "string"},
        "limitations":     {"type": "string"},
        "local_deployable":{"type": "boolean"}
      },
      "required": ["what_was_built", "limitations", "local_deployable"]
    },
    "uzbekistan_application_uz": {"type": "string"},
    "evidence_level": {"type": "string", "enum": ["vendor_claim_only", "multiple_evidence"]}
  },
  "required": ["summary_uz", "why_it_matters_uz", "leadership_uz",
               "technical", "uzbekistan_application_uz", "evidence_level"]
}
```

Empty string means "not found in the source". The model must never invent a URL, a
license, or a benchmark number. Any field it cannot ground in the article text stays empty.

`evidence_level` is `multiple_evidence` only when an independent source confirms the
vendor's claim. In M1 this is always `vendor_claim_only`; the verification layer arrives
in M2.3.

---

## 6. Models

| Role | Tag | Digest | p50 | p95 |
|---|---|---|---|---|
| Triage | `gemma4:latest` (8.0B, Q4_K_M) | `c6eb396dbd5992bb` | 5.59s | 6.20s |
| Classify + deep | `gemma4:31b` (31.3B, Q4_K_M) | `6316f0629137b426` | 11.93s | 27.52s |

Request settings: `stream: false`, `format` = the schema above, `options.temperature = 0`,
**`options.num_predict` set on every call**.
At temperature 0 the output is reproducible for identical input — verified in M0.1.

### `num_predict` is mandatory

Measured in T0.3, 2026-08-14. A free-text Uzbek generation request to `gemma4:31b` on a
short article never terminated. With `stream: false` nothing is visible until the call
completes, so it consumed the full 600-second timeout and returned zero characters.
While it was stuck it held the server's slot, and the next seven requests came back
`503 Service Unavailable`. The server recovered on its own afterwards.

Without a token cap a degenerate generation loop runs until the context window is
exhausted. On a 31B model at a 20480-token context that is minutes of GPU time for one
article, and on a **shared organisation server** it denies service to everyone else.

Caps to use:

| Call | `num_predict` |
|---|---|
| Triage / classification | 400 |
| Uzbek summary | 500 |
| Deep analysis | 1200 |

Client timeouts must be well below the point where a stuck request matters: 60s fast
tier, 180s deep tier. A timeout is recoverable; a wedged server is not.

Structured output (`format`) makes a runaway far less likely — the grammar forces the
object to close — which is why every classification call in M0.1 finished normally while
the free-text language call did not. `num_predict` is still set on all of them, because
"less likely" is not a guarantee and the failure is expensive.

`Analysis.model_digest` records the digest on every call, because on this server the 8B
model has no tag other than `latest` and a repointed tag must be detectable.

Concurrency: the `llm` Celery queue runs at **2**. Measured: 8 parallel requests are
slower than running them one at a time.

Batching: run **all** triage, then **all** classification. A model swap costs ~11s, so
alternating per article would pay it on every article.

---

## 7. Uzbek output strategy

**PENDING — T0.3.** Three strategies were generated and are awaiting human scoring in
`docs/spike/LANGUAGE_QUALITY.md`:

- **A** — prompt in Uzbek, answer in Uzbek. 1 call.
- **B** — summarise in English, second call translates. 2 calls.
- **C** — reason in English, emit `summary_uz` in a structured field. 1 call.

Section 5 above assumes a single call producing Uzbek directly, which matches A and C.
If strategy B wins, the deep-analysis schema splits into an English generation step plus
a translation step, and the per-item cost roughly doubles.

**This section must be completed before T1.6.**

---

## 8. Changing this document

Enum values are referenced by database rows, prompts, ranking rules and templates.
Adding a value is additive and safe. Removing or renaming one invalidates stored
`Analysis.payload` rows and requires an ADR plus a migration plan.
