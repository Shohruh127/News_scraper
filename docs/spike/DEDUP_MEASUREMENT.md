# Deduplication Signal Measurement

Date: 2026-08-17
Script: `spikes/probe_dedup.py`
Data: the 185 live articles in the working database, 17,020 pairs

## Question

Which similarity signal separates a real duplicate from two distinct releases?

Two cases from live data decide it:

| Case | Articles | Required outcome |
|---|---|---|
| Qwen pair | `huggingface.co/Qwen/Qwen3.8-2.4T-A95B-FP8` and `.../Qwen3.8-2.4T-A95B` — two quantisation variants of one model, near-identical model-card boilerplate, **both from `hn`** | **merge** |
| Ollama pair | `ollama/ollama v0.32.10` and `v0.32.11` — consecutive releases, different changelogs, same source | **must not merge** |

## Result

| Signal | Qwen pair (merge) | Ollama pair (keep apart) | Gap |
|---|---|---|---|
| title, word 2-grams | 0.000 | 0.000 | none — blind |
| **text, char 5-grams** | **0.900** | **0.110** | **0.79** |
| text, word 3-grams | 0.804 | 0.008 | 0.80 |

Full sweep at `text_char5 >= 0.80` over all 17,020 pairs: **exactly one pair merged** — the
Qwen pair. Zero false positives.

## What this settles

1. **Titles are the wrong field.** Both decisive cases score 0.000 on titles. Removing
   fuzzy title matching (ADR-003) was correct, but the stated reason was wrong: the problem
   was not that clustering is unnecessary, it was that the signal was measured on the wrong
   field. Release-numbered titles look similar while their content differs; variant model
   cards look different while their content is identical.

2. **The separation is wide enough to need no tuning.** Any threshold between roughly 0.2
   and 0.9 produces the same decision on both cases. A 0.79 gap means this signal is stable
   across data drift, unlike the title threshold where no value worked.

3. **The same-source rule must not apply at this tier.** The Qwen duplicate arrived twice
   through `hn`. ADR-003's "never cluster two articles from the same source" would block it.
   When text similarity is 0.90, the source is irrelevant.

## MinHash

MinHash estimates exactly this quantity — Jaccard over shingles. It is therefore the right
tool, but it is an **indexing** optimisation, not a different signal.

| Approach | Why | When |
|---|---|---|
| Exact Jaccard, char 5-grams | 17k pairs runs in seconds, no dependency, no model | **now** |
| MinHash + LSH | Same signal, sub-linear candidate generation | past ~10k articles |

Feedly uses LSH at 80% similarity and reports 80% of their intake is duplicate; their volume
is hundreds of thousands per day. At 185 per day the indexing problem does not exist yet.

## Two tiers are needed, and only one is measured

`text_char5` catches **near-identical text**. It does not catch the same story written
independently by two outlets:

```
"Accelerating GPT-5.6 Sol Ultrafast"                            (Cerebras)
"Previewing Ultrafast mode: GPT-5.6 Sol at up to 14X the speed"  (OpenAI)
```

These share an announcement, not wording. Verbatim overlap is low, so char-5 Jaccard will not
merge them. That case needs semantic similarity.

| Tier | Signal | Threshold | Source rule | Status |
|---|---|---|---|---|
| A | char 5-gram Jaccard on text | 0.80 | ignore source | **measured, ready** |
| B | embedding cosine | ~0.85 | cross-source only | not built |

NewsCatcher uses 0.95 cosine for *duplicates*; OpenClaw uses ~0.85 for *same story*. Those
are different jobs and both thresholds are needed. Tier A replaces the 0.95 duplicate tier
more cheaply, because verbatim duplicates do not need a model.

## Recommendation

Implement Tier A now: char 5-gram Jaccard over the first 6000 characters of
`extracted_text`, threshold 0.80, applied before topic diversification, source ignored.
No new dependency.

Tier B stays open. It is the remaining gap against `PROJECT_PLAN.md` §2's "one story from
several sources is one post".
