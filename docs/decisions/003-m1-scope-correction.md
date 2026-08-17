# ADR-003 — Correcting the M1 scope so the plan stops contradicting itself

Date: 2026-08-14
Status: **Accepted**
Amends: `IMPLEMENTATION_PLAN.md` §4, `PROJECT_PLAN.md` §5

## Context

An external review of the T1.5–T1.8 implementation found that the code could not satisfy
`PROJECT_PLAN.md` §2 even when working perfectly. Verification confirmed the finding.

The MVP acceptance criteria in `PROJECT_PLAN.md` §2 require:

| Criterion | Buildable with the M1 scope as written? |
|---|---|
| 7 consecutive autonomous days | yes, once task routing is fixed |
| 2–7 items, each linked to its source | yes, once the date window is fixed |
| No `announcement_only` / `paper_only` | yes, already works |
| **Uzbek text readable without editing** | **no — nothing in M1 produces Uzbek** |
| **One story from several sources = one post** | **no — clustering was moved to M2.1** |
| Pipeline under 60 minutes | yes |
| Errors alert the admin | yes, already works |

Two criteria are unbuildable. `PROJECT_PLAN.md` §5 additionally promises a daily balance
that includes speech, robotics and fintech/govtech items, while M1 has no source for any
of those streams — seven of the twelve topics have no feed at all
(`spike/GOLD_SET_REVIEW.md` §4).

This is not an implementation defect. It is a planning defect: I cut the vertical slice
through the wrong plane. The slice removed capabilities (Uzbek generation, clustering,
topic coverage) while leaving the acceptance criteria that depend on them untouched.
An agent following that plan produces code that passes every task-level check and still
cannot produce the product.

## Decision

Move the **minimum working version** of three capabilities into M1. The advanced versions
stay in M2.

### 1. Editorial stage — the reason M1 exists at all

A new pipeline stage runs the deep model over **only the 2–7 selected items**, producing
`summary_uz` and the technical fields already specified in `CONTENT_SCHEMA.md` §5, using
strategy C (`decisions/../CONTENT_SCHEMA.md` §7).

`Analysis` gains a `stage` field: `triage` · `classification` · `editorial`.
Ranking reads `classification`. Rendering reads `editorial`. **A missing `summary_uz` is
an error, not a fallback to English.**

Cost: 5 items × ~30s. Negligible against the 60-minute budget.

### 2. Minimal clustering

Canonical URL, then `rapidfuzz` title similarity above a configurable threshold, applied
**before** topic diversification. One cluster becomes one `DigestItem` carrying several
source links.

Embeddings, entity resolution and cross-day clustering stay in M2.2. The M0.4 sample
already contains a live case: the Cerebras and OpenAI posts about Ultrafast mode
(`spike/GOLD_SET_REVIEW.md` §3, item 5).

### 3. Source coverage that matches the taxonomy

Every stream the digest promises must have at least one feed, or the promise is removed.

| Stream | M1 source | Connector |
|---|---|---|
| frontier_models | openai, deepmind, anthropic | rss, html |
| ai_agents | gh_langgraph, gh_mcp | github |
| new_approaches | hf_papers | hf |
| production_engineering | gh_ollama | github |
| speech_voice | **whisper + faster-whisper releases** | github |
| robotics | **NVIDIA developer blog** | rss |
| safety_security | **arXiv cs.CR** | rss |
| startups | hn | hn |

Three streams get **no dedicated feed in M1** and the plan must stop promising them:

- `fintech`, `govtech` — no English-language daily feed exists that is worth a connector.
  They appear opportunistically through HN and provider blogs.
- `technical_talks` — needs a YouTube/transcript connector. That is M2.

`PROJECT_PLAN.md` §5's daily balance is rewritten to promise only what has a source.

## What stays in M2

Story clustering with embeddings · independent benchmark verification · feedback learning ·
25–40 sources · health baselines · deployment · breaking-news path.

## Consequences

- M1 grows by roughly three days of work.
- `Analysis.stage` requires a migration; existing rows backfill to `classification`.
- The digest cannot be published in Uzbek until the editorial stage exists, so GATE 1
  cannot be reached earlier by skipping it.
- `PROJECT_PLAN.md` §2 stays unchanged. The plan bends to the product, not the reverse.

## Also corrected here

`IMPLEMENTATION_PLAN.md` T1.8 specified `misfire_grace_time` and `coalesce=True`. Both are
**APScheduler** options; neither exists in Celery Beat. They survived the ADR-001 switch to
Celery by oversight. Celery Beat has `expires`, which prevents a stale task from running
late but does not prevent two runs overlapping. Overlap protection needs an explicit lock.
