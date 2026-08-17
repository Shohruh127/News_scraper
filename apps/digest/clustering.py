"""Canonical URL story clustering module.

Rules (ADR-003 corrected):
1. Exact canonical URL matching across different sources.
2. Never cluster items from the same source (each release or post in a single source is distinct).
3. Fuzzy matching and semantic clustering are deferred to M2.
4. Highest scoring article in a cluster becomes the primary article; others become secondary.
5. One DigestItem represents the cluster and links all sources as evidence.
"""

import logging

from .models import Analysis, Article

log = logging.getLogger(__name__)


def cluster_candidates(
    candidates: list[tuple[Article, Analysis, float]],
) -> list[tuple[Article, Analysis, float, list[Article]]]:
    """Group cross-source duplicate stories sharing the same canonical URL.

    Input: list of (article, analysis, score)
    Output: list of (primary_article, analysis, score, [secondary_article, ...])
    """
    if not candidates:
        return []

    # Each cluster: list of (article, analysis, score)
    clusters: list[list[tuple[Article, Analysis, float]]] = []

    for cand in candidates:
        art, _analysis, _score = cand
        matched_cluster = None

        if art.canonical_url:
            for cluster in clusters:
                for c_art, _c_analysis, _c_score in cluster:
                    # Never cluster items from the same source
                    if art.source_id and c_art.source_id and art.source_id == c_art.source_id:
                        continue

                    # Exact canonical URL match across different sources
                    if art.canonical_url == c_art.canonical_url:
                        matched_cluster = cluster
                        log.info(
                            "Clustered cross-source '%s' with '%s' by canonical URL: %s",
                            art.title,
                            c_art.title,
                            art.canonical_url,
                        )
                        break

                if matched_cluster is not None:
                    break

        if matched_cluster is not None:
            matched_cluster.append(cand)
        else:
            clusters.append([cand])

    result: list[tuple[Article, Analysis, float, list[Article]]] = []
    for cluster in clusters:
        # Sort cluster members by score descending
        cluster.sort(key=lambda x: x[2], reverse=True)
        primary_art, primary_analysis, primary_score = cluster[0]
        secondary_arts = [c[0] for c in cluster[1:]]
        result.append((primary_art, primary_analysis, primary_score, secondary_arts))

    # Keep overall candidates sorted by primary score descending
    result.sort(key=lambda x: x[2], reverse=True)
    return result
