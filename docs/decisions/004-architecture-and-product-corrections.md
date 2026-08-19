# ADR-004 — Architecture and product corrections

Date: 2026-08-17
Status: **Accepted**
Amends: ADR-003, `IMPLEMENTATION_PLAN.md` §4, `CONTENT_SCHEMA.md` §6–7

Two things prompted this: the project owner asked for the architecture to be checked against
industry practice, and the first end-to-end run exposed real defects
(`spike/FIRST_PIPELINE_RUN.md`).

---

## 1. The cascade is inverted

**Finding.** The standard pattern reserves the expensive model for a small fraction of
traffic. In the first real run, 78% of articles reached the deep model.

**Decision.** Apply deterministic filtering, canonical URL uniqueness, and measured text
deduplication before any LLM call. LLM work must scale with the small set of publishable
candidates rather than the full source intake.

## 2. Clustering uses one measured deterministic signal

The live-corpus experiment compared 17,020 article pairs. Exact character 5-gram Jaccard over
the first 6,000 characters of article text separated the decisive duplicate at `0.900` from
two consecutive releases at `0.110`. Title similarity scored `0.000` on both cases.

**Decision.** Exact text Jaccard is the complete clustering architecture for this project.
It has no model, external service, additional dependency, secondary tier, or planned fallback.

## 3. Deduplication contract

| Signal | Threshold | Source rule | Status |
|---|---:|---|---|
| exact character 5-gram Jaccard on article text | 0.80 | ignore source | measured and implemented |

The source rule is deliberate: the duplicate that reached the first digest arrived twice
through the same HN connector. The comparison runs only across the small candidate set, so the
straightforward exact implementation is operationally sufficient.

## 4. Batch classification, with a constraint the literature omits

The batching study (batch 25–100, >80% cost saving, under 2 pp accuracy loss across eight
models from four providers) was verified. Its models have far larger context windows than
this deployment.

Measured here: the server runs `gemma4:31b` at **`context_length = 32768`**. At the current
8000-character truncation, 25 articles is roughly 50,000 tokens — about 1.5× over.

| Batch size | Chars per article | Fits 32K |
|---|---|---|
| 10 | 8000 (current) | yes, comfortably |
| 15 | 8000 | yes, tight |
| 25 | must cut to ~4600 | marginal |
| 50+ | — | no |

Two further constraints:

- **`num_predict` must scale with batch size.** It is currently 400. A batch of 25 needs
  roughly 3000. Left at 400 the JSON array truncates mid-output and the entire batch is
  lost silently.
- **Blast radius multiplies.** One malformed batch loses 25 classifications instead of one.
  Against a server that returns 503 under load, that matters.

**Decision.** Batch at 10–15, not 25. Raise `num_predict` proportionally. Measure gold-set
precision after batching; GATE 1's ≥ 0.80 must hold.

Note the optimisation target differs from the sources: they optimise API cost, this project
runs a local model. Batching helps here by removing **round trips** against a server whose
measured concurrency ceiling is 2, not by saving tokens.

## 5. MiMo as the editorial model, for testing only

`gemma4:31b` produces unstable Uzbek (`spike/FIRST_PIPELINE_RUN.md` §3). Compared on the
same article that it garbled:

| Model | Latency | Output |
|---|---|---|
| `gemma4:31b` | ~12 s | "modelining **an'nagora an'nanash ownan** open-weight model ekanligini" |
| `mimo-v2.5` | 8.7 s | "Yangi Qwen3.8-2.4T-A95B-FP8 — bu 2.4 trillion parametrli kuchli open-weight model bo'lib, 95 milliardi faol ishlatiladi." |
| `mimo-v2.5-pro` | 11.4 s | "Qwen kompaniyasi yangi ochiq model — Qwen3.8-2.4T-A95B-FP8 ni taqdim etdi. FP8 kvantizatsiyasi yordamida model samaradorligi deyarli original darajada saqlanadi." |

Both MiMo models produce clean, natural Uzbek and keep technical terms in English as
instructed. `response_format: {"type": "json_object"}` returns valid JSON. `mimo-v2.5` is
already sufficient; `pro` is not required.

**Decision.** The editorial stage uses MiMo. Triage and classification stay on local Ollama,
where `gemma4` measured precision 0.83 and costs nothing.

That split keeps API usage at roughly **7 calls per day** — one per published item.

> **Terms note.** The MiMo Token Plan states it is for interactive use with coding and agent
> tools and "may not be used for automated scripts or application backends", with
> suspension or key revocation as stated consequences. This project is an automated
> backend. The concern was raised and the project owner accepted the risk explicitly for
> the testing phase, with the stated intention of moving to a local model or a
> backend-permitted API tier before release. Recorded here so the obligation is not lost.

**Provider selection is one `.env` variable** (`LLM_PROVIDER`), scoped to the editorial
stage, so reverting is a configuration change rather than a rewrite.

## 6. One post per news item

**New product requirement from the project owner, 2026-08-17.**

The digest currently publishes one channel message containing up to 7 items. It must instead
publish **one post per item**.

Reasons given: each subject area has its own audience, so a reader can follow, forward and
discuss a single item; and it is more convenient to read.

Consequences:

- `DigestItem.channel_message_id` becomes genuinely per-item. In the first run all 7 items
  stored `channel_message_id = 2`, because there was one message.
- Each post gets its own auto-forward in the discussion group, so each gets its own
  technical appendix and, in M2, its own feedback buttons and comment thread.
- Post format changes from a list to a self-contained card. Rate limits are not a concern
  at 7 posts.
- A digest becomes a batch of posts. `Digest` remains the unit of idempotency and of the
  `published` / `failed` decision: partial publication must be visible.

## 7. Each stage needs its own prompt

Also requested. Triage, classification and editorial currently share one prompt template
with the enum definitions. They have different jobs:

- triage — cheap reject, needs only enough to identify obvious noise
- classification — the real taxonomy decision, needs the full boundary definitions
- editorial — reader-facing Uzbek, needs voice and format instruction, not taxonomy

Each gets a dedicated, separately tuned prompt.

## 8. Post format and source expansion

Two open items, both requiring work before implementation:

- **Post format / UX.** Study established professional channels and platforms for what a
  good technical news post contains and how it is laid out. Produce options for the project
  owner to choose from before writing templates.
- **More sources.** Deferred until the post format is settled, so new sources are not
  wired into a format that is about to change.

---

## Sources

Feedly clustering · NewsCatcher deduplication · FrugalGPT and model-cascade routing ·
the batch-classification study (batch 25–100, >80% saving, <2 pp loss) · OpenClaw
tech-news digest (100+ sources, cosine ~0.85, `base + priority + recency + engagement`
scoring) · classifai ADR-0002 on rules-first with LLM fallback.

All URLs are recorded in the conversation record; the two load-bearing claims — the batching
study and the OpenClaw pipeline description — were fetched and verified directly rather than
taken from summaries.