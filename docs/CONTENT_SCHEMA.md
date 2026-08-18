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

### `archetype` — the shape of the post

Measured 2026-08-18 on six real items: with no definitions the model scored 0/6 and filled six
irrelevant detail blocks, including `HIGH` severity for a change to a default sampling parameter.
With the definitions below it scored 5/6 and filled none. They are load-bearing.

| value | boundary |
|---|---|
| `release` | A named product or model shipped a new version. A changelog, a release note, a version number. This is the default for any version bump. |
| `agent_protocol` | A protocol or framework for connecting tools to models, where the news IS the connection mechanism. Not a runtime that happens to run agents. |
| `risk_hardening` | A risk, a weakness, or work done to reduce one. There must be something that can go wrong and someone acting on it. |
| `policy` | A rule issued by a government or standards body, with someone obliged to comply. Pricing is not policy. |
| `research` | A method or a finding with a claim and evidence, not a shipped artifact. |
| `company_product` | A company entering a market or making a commercial launch, where the company is the news rather than the version. |

Exactly one `<archetype>_details` block is filled. Every field inside it is optional in the
schema: 11 stored security articles contained one CVE identifier, no CVSS score and no
affected-version range, so a required field would be invented rather than found.

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

## 7. Uzbek output strategy — **C**

Decided 2026-08-14 from the T0.3 samples in `spike/LANGUAGE_QUALITY.md`. Chosen by
Claude, not by a native speaker — see the caveat at the end of this section.

**Strategy C: reason in English inside the response, emit the reader-facing Uzbek in a
structured `summary_uz` field. One call.**

Rejected: **A** (prompt and answer in Uzbek, 1 call) and **B** (English summary, then a
second call to translate, 2 calls).

### Why

Measured over 10 articles on the fast model:

| | A | B | **C** |
|---|---|---|---|
| Calls per article | 1 | 2 | **1** |
| Mean seconds | 7.55 | 7.39 | **7.23** |
| Mean output length | 604 | 703 | **549** |
| English technical terms kept | 25 | 38 | **39** |
| Suffix-stacking artifacts | 9 | 7 | **6** |

1. **C cannot hang.** It is the only strategy using `format` with a JSON schema, so
   grammar-constrained decoding forces the object closed. A and B are free-text — which
   is precisely what wedged the shared Ollama server during T0.3 and produced 503s for
   seven subsequent requests (§6). This alone would decide it.

2. **A invents things.** On the Ollama v0.32.11 changelog, A concluded the release
   "significantly raises the company's technological efficiency". Nothing in the source
   says that. C stayed with what the changelog actually lists. In a digest whose whole
   premise is filtering vapourware, a summariser that adds unearned claims is
   disqualifying.

3. **C keeps technical terms in English most consistently** — `service tier`,
   `output token`, `incident response`, `mixed-type tabular records`, `Mean Absolute
   Error (MAE)`. A transliterates instead (`freymvork`), which reads worse to the
   engineers who are half the audience. A also produced the Russianism `barabar` where
   Uzbek takes `baravar`.

4. **B costs two requests per item for no quality gain.** Against a server whose measured
   concurrency ceiling is 2 (§6), doubling request count is expensive: 50 classified
   articles become 100 requests.

5. **C is debuggable.** `reasoning_en` sits beside `summary_uz` in the same response, so
   a bad summary can be traced to either misreading the article or mistranslating it. A
   and B give no such signal.

### What this does not settle

Naturalness to a native ear is not something this analysis can judge. C has visible
defects: `sohaslari` for `sohalari`, and `generative modelsning` stacking an English
plural under an Uzbek suffix. These are worth fixing in the prompt.

**Recommended check, ~10 minutes:** read the three C samples in
`spike/LANGUAGE_QUALITY.md` for the Ultrafast, TailBooster and v0.32.11 articles. If they
read acceptably, C stands. If they do not, A is the fallback — but only with `num_predict`
capped, because A is free-text.

### Consequences for §5

The deep-analysis schema already emits `summary_uz`, `why_it_matters_uz`,
`leadership_uz` and `uzbekistan_application_uz` in one structured call, which is exactly
strategy C. No change is needed. Prompts must instruct the model to reason in English and
write only the `*_uz` fields in Uzbek, keeping technical terms in English.

---

## 8. Changing this document

Enum values are referenced by database rows, prompts, ranking rules and templates.
Adding a value is additive and safe. Removing or renaming one invalidates stored
`Analysis.payload` rows and requires an ADR plus a migration plan.
