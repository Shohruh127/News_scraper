# Deduplication signal measurement

Date: 2026-08-17
Data: 185 live articles, 17,020 pairs
Implementation: `apps/digest/clustering.py`

## Question

Which deterministic similarity signal separates a real duplicate from two distinct releases?

| Case | Required outcome |
|---|---|
| Qwen3.8 FP8 and base model cards, both arriving through HN | merge |
| Ollama v0.32.10 and v0.32.11 changelogs | keep apart |

## Result

| Signal | Qwen pair | Ollama pair | Gap |
|---|---:|---:|---:|
| title word 2-grams | 0.000 | 0.000 | none |
| **text character 5-grams** | **0.900** | **0.110** | **0.79** |
| text word 3-grams | 0.804 | 0.008 | 0.80 |

The full sweep at character-5 Jaccard `>= 0.80` merged exactly the Qwen pair and produced
no observed false positives.

## Decision

1. Use the first 6,000 characters of normalized article text.
2. Compare exact character 5-gram sets with Jaccard similarity.
3. Merge at the measured `0.80` threshold.
4. Ignore source identity at this stage because the decisive duplicate arrived twice through
   the same aggregator source.
5. Keep the exact comparison as the complete clustering architecture for the measured project
   volume; add no model, index, service, dependency, or fallback clustering path.

The comparison is bounded by the small post-filter candidate set, so its straightforward
quadratic loop is acceptable and easier to verify than another subsystem.