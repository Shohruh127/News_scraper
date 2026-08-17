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
traffic. In the first real run, 78% of articles reached `gemma4:31b`.

The governing rule, from the sources below: **LLM call count must scale with the number of
items published, not with the number of articles collected.** A correct pipeline makes
roughly the same number of LLM calls whether it watches 12 sources or 100.

| | Current, 12 sources | Correct, 12 sources | Correct, 100 sources |
|---|---|---|---|
| Raw items/day | ~300 | ~300 | ~1500 |
| After Tier 0 | 185 | ~50 | ~250 |
| **LLM calls** | **~270** | **~10** | **~15** |
| LLM wall time | ~65 min | ~15 min | ~25 min |

**Decision.** Restructure into tiers where the cheap tier contains **no LLM**:

```
Tier 0  deterministic, milliseconds, no LLM
          canonical URL + content hash            [built]
          char 5-gram Jaccard >= 0.80             [measured, not built]
          embedding cosine ~0.85                  [not built]
          7-day window, length, blocklist         [built]
          scoring: base + priority + recency      [partly built]
Tier 1  8B triage, batched
Tier 2  31B classification, batched
Editorial  deep model, 2–7 items, not batched
```

The current "cheap" tier is itself an LLM at ~6 s per article. A cheap tier must be rules
and vector arithmetic.

## 2. Embeddings are the foundation, not an optimisation

**This was the most expensive mistake in the plan.** ADR-003 deferred embeddings to M2.2 as
a "proven need" item. They are not an optimisation — one local embedding model, running in
milliseconds on CPU, does three jobs at once:

1. duplicate detection where verbatim overlap is low
2. cross-source story clustering — the unmet `PROJECT_PLAN.md` §2 criterion
3. cheap relevance pre-filtering by proximity to labelled gold-set examples, with no LLM

Deferring them forced fuzzy title matching, which failed on measurement, which led to
dropping clustering from M1 entirely, which left the MVP criterion unmet and deduplication
at exact-match recall. One wrong call produced a chain of four.

**Decision.** Embeddings move into M1.

## 3. Deduplication has two tiers

Measured in `spike/DEDUP_MEASUREMENT.md` on 185 live articles and 17,020 pairs.

| Tier | Signal | Threshold | Source rule | Status |
|---|---|---|---|---|
| A | char 5-gram Jaccard on text | 0.80 | **ignore source** | measured |
| B | embedding cosine | ~0.85 | cross-source only | to build |

Tier A separates the two decisive live cases by a margin of 0.79 and produced zero false
positives. It supersedes ADR-003's "never cluster two articles from the same source" **at
this tier**: the duplicate that reached the first digest arrived twice through `hn`.

MinHash/LSH estimates the same Jaccard quantity. It is an indexing optimisation for volume
past roughly 10k articles, not a different signal, and is not needed now.

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
