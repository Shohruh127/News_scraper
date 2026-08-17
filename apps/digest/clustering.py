"""Story clustering — Tier A (ADR-004 §3, measured in docs/spike/DEDUP_MEASUREMENT.md).

Canonical-URL duplicates never reach this module: `Article.canonical_url` is UNIQUE, so
the database rejects them at ingestion. Clustering by URL here was dead code — it could
not fire. The signal that actually works is text similarity.

Tier A: character 5-gram Jaccard over the article text, threshold from settings.
Measured over 17,020 pairs of live articles:

    Qwen3.8-2.4T-A95B-FP8  vs  Qwen3.8-2.4T-A95B     0.900   must merge
    ollama v0.32.10        vs  ollama v0.32.11       0.110   must not merge

A 0.79 gap, and exactly one merge across the whole corpus with zero false positives.
Titles score 0.000 on both cases and are useless for this.

Source is deliberately ignored. The duplicate that reached the first published digest
arrived twice through `hn`; ADR-003's same-source exclusion would have blocked it.

Tier B — embedding cosine ~0.85 for the same story written independently by two outlets —
is not built. It remains the open gap against PROJECT_PLAN.md §2.
"""

import logging
import re

from django.conf import settings

from .models import Analysis, Article

log = logging.getLogger(__name__)


def _shingles(text: str, k: int, limit: int) -> set[str]:
    """Character k-gram set over normalised text."""
    t = re.sub(r"\s+", " ", text[:limit].lower()).strip()
    if len(t) < k:
        return set()
    return {t[i : i + k] for i in range(len(t) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def text_similarity(a: Article, b: Article) -> float:
    """Tier A similarity between two articles. 0.0 when either has no usable text."""
    k = settings.CLUSTER_SHINGLE_SIZE
    limit = settings.CLUSTER_TEXT_CHARS
    return _jaccard(
        _shingles(a.extracted_text or "", k, limit),
        _shingles(b.extracted_text or "", k, limit),
    )


def cluster_candidates(
    candidates: list[tuple[Article, Analysis, float]],
    threshold: float | None = None,
) -> list[tuple[Article, Analysis, float, list[Article]]]:
    """Group near-identical stories.

    Input:  [(article, analysis, score), ...]
    Output: [(primary_article, analysis, score, [secondary_article, ...]), ...]

    The highest-scoring member becomes the primary; the rest become evidence links on the
    same DigestItem, so one story consumes one slot.
    """
    if not candidates:
        return []

    if threshold is None:
        threshold = settings.CLUSTER_JACCARD_THRESHOLD

    k = settings.CLUSTER_SHINGLE_SIZE
    limit = settings.CLUSTER_TEXT_CHARS
    # Shingle each article once; the comparison is O(n^2) but n is the candidate count,
    # not the corpus. MinHash/LSH only becomes worthwhile past ~10k (ADR-004 §3).
    shingles = {
        art.id: _shingles(art.extracted_text or "", k, limit) for art, _, _ in candidates
    }

    clusters: list[list[tuple[Article, Analysis, float]]] = []
    for cand in candidates:
        art = cand[0]
        placed = False
        for cluster in clusters:
            for member, _, _ in cluster:
                score = _jaccard(shingles[art.id], shingles[member.id])
                if score >= threshold:
                    log.info(
                        "Clustered '%s' with '%s' (jaccard %.3f, sources %s/%s)",
                        art.title[:50],
                        member.title[:50],
                        score,
                        art.source_id,
                        member.source_id,
                    )
                    cluster.append(cand)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            clusters.append([cand])

    result = []
    for cluster in clusters:
        cluster.sort(key=lambda x: x[2], reverse=True)
        primary_art, primary_analysis, primary_score = cluster[0]
        result.append((primary_art, primary_analysis, primary_score, [c[0] for c in cluster[1:]]))

    result.sort(key=lambda x: x[2], reverse=True)
    if len(result) < len(candidates):
        log.info("Clustering: %s candidates -> %s stories", len(candidates), len(result))
    return result
