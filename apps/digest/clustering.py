"""Story clustering: two tiers, and one story consumes one slot (ADR-004 §3).

Tier A - exact text: character 5-gram Jaccard over article text, threshold from settings.
Measured over 17,020 pairs of live articles (DEDUP_MEASUREMENT.md):

    Qwen3.8-2.4T-A95B-FP8  vs  Qwen3.8-2.4T-A95B     0.900   must merge
    ollama v0.32.10        vs  ollama v0.32.11       0.110   must not merge

A 0.79 gap, and exactly one merge across the whole corpus with zero false positives.
Titles score 0.000 on both cases. Canonical-URL duplicates never reach this module:
`Article.canonical_url` is UNIQUE, so the database rejects them at ingestion. Source is
deliberately ignored: the duplicate that reached the first published digest arrived twice
through `hn`.

Tier B - the same story written up twice (added 2026-09-10). Tier A cannot see it: on
2026-09-09 the AlphaGenome Atlas launch arrived as DeepMind's own post (12k characters)
and as Google's blog post via HN (2.7k), Jaccard 0.215 against a threshold of 0.80, and
the same story took two of the evening block's six slots - positions 1 and 2, the first
of which then failed for want of a photo. Measured on that day's 15 candidates, no text
signal separates that pair from the nearest different-story pair (two LG-TV articles at
0.123): the same event reads differently in every outlet. So the grouping is a judgement,
made once per composition by the classifier provider over the candidates' titles and
first lines (`llm.group_same_story`), and passed in here as `same_story_groups`; this
module stays a pure function of its inputs. A failed call leaves Tier A alone.

Within a cluster the primary is the highest-scoring member that has a photo, because a
post is a photo post and an item without one fails; the cluster keeps its best score.
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


def _has_photo(article: Article) -> bool:
    return bool((article.meta or {}).get("image_url"))


def cluster_candidates(
    candidates: list[tuple[Article, Analysis, float]],
    threshold: float | None = None,
    same_story_groups: list[list[int]] | None = None,
) -> list[tuple[Article, Analysis, float, list[Article]]]:
    """Group near-identical stories (Tier A) and stories judged the same (Tier B).

    Input:  [(article, analysis, score), ...], and optionally groups of article ids that
            report the same story (`llm.group_same_story`); ids not among the candidates
            are ignored, and a group that touches two Tier A clusters merges them.
    Output: [(primary_article, analysis, score, [secondary_article, ...]), ...]

    The primary is the highest-scoring member with a photo (any member if none has one);
    the rest become evidence links on the same DigestItem, so one story consumes one
    slot. The cluster keeps the best score among its members: the story is as strong as
    its best write-up, whichever copy carries the photo.
    """
    if not candidates:
        return []

    if threshold is None:
        threshold = settings.CLUSTER_JACCARD_THRESHOLD

    k = settings.CLUSTER_SHINGLE_SIZE
    limit = settings.CLUSTER_TEXT_CHARS
    # Shingle once per article; O(n^2) is bounded by the small post-filter candidate set.
    shingles = {art.id: _shingles(art.extracted_text or "", k, limit) for art, _, _ in candidates}
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

    if same_story_groups:
        known = {art.id for art, _, _ in candidates}
        for group in same_story_groups:
            ids = {i for i in group if i in known}
            touched = [c for c in clusters if any(m[0].id in ids for m in c)]
            if len(touched) < 2:
                continue
            merged = [m for c in touched for m in c]
            clusters = [c for c in clusters if c not in touched] + [merged]
            log.info("Same story (judged): %s", " / ".join(m[0].title[:40] for m in merged))

    result = []
    for cluster in clusters:
        best_score = max(c[2] for c in cluster)
        cluster.sort(key=lambda x: (_has_photo(x[0]), x[2]), reverse=True)
        primary_art, primary_analysis, _ = cluster[0]
        result.append((primary_art, primary_analysis, best_score, [c[0] for c in cluster[1:]]))

    result.sort(key=lambda x: x[2], reverse=True)
    if len(result) < len(candidates):
        log.info("Clustering: %s candidates -> %s stories", len(candidates), len(result))
    return result
