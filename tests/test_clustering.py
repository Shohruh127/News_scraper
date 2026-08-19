"""Clustering Tier A: character 5-gram Jaccard over text (ADR-004 §3).

The two decisive cases come from live data and are recorded in
docs/spike/DEDUP_MEASUREMENT.md. They are reproduced here as fixtures so a future change
to the signal or threshold has to break a test that states *why* the value was chosen.
"""

import pytest

from apps.digest import clustering
from apps.digest.models import Analysis, Article, Source

pytestmark = pytest.mark.django_db


# Two HuggingFace model cards for quantisation variants of one model: the boilerplate is
# identical and only the model name differs. Measured Jaccard 0.900.
CARD = (
    "Instructions to use Qwen/{name} with libraries, inference providers, notebooks, "
    "and local apps. Follow these links to get started. Library support includes "
    "Transformers, vLLM, SGLang and Docker. The model uses a Mixture-of-Experts "
    "architecture with FP8 quantised weights and a native context length of 262144 "
    "tokens, extensible beyond one million. It offers improvements on coding, "
    "professional work, research and long-horizon agentic tasks. " * 6
)

# Two consecutive release notes: titles nearly identical, content genuinely different.
# Measured Jaccard 0.110.
RELEASE_A = (
    "Models that do not set a repeat_penalty now default to 1.0 instead of 1.1, matching "
    "other engines and speeding up speculative decoding. Faster prefill on NVFP4 MLX "
    "models with a global scale. Fixed blob verification being skipped when an OCI "
    "manifest config and layer share a digest. " * 6
)
RELEASE_B = (
    "ollama launch dsh now supports DeepSeek Harness, an open-source agent harness. "
    "ollama launch muse now supports Muse Code, an agentic coding CLI. The "
    "OpenAI-compatible Responses API now supports web search. Template updates for "
    "Muse Glimmer. " * 6
)


@pytest.fixture
def src_a(db):
    return Source.objects.create(name="src_a", connector="hn", url="https://a.example")


@pytest.fixture
def src_b(db):
    return Source.objects.create(name="src_b", connector="rss", url="https://b.example")


def make(source, url, title, text, n):
    art = Article.objects.create(
        source=source,
        canonical_url=url,
        content_hash=f"{n:064d}",
        title=title,
        extracted_text=text,
        status=Article.Status.CLASSIFIED,
    )
    an = Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 7,
            "production_readiness": 8,
            "reason": "x",
        },
        latency_ms=1000,
    )
    return art, an


def test_near_identical_text_merges_even_within_one_source(src_a):
    """The duplicate that reached the first published digest arrived twice through `hn`.

    ADR-003's same-source exclusion would have blocked it, which is why Tier A ignores
    the source entirely.
    """
    a1, an1 = make(
        src_a,
        "https://hf.co/Qwen/A95B-FP8",
        "Qwen3.8-2.4T",
        CARD.format(name="Qwen3.8-2.4T-A95B-FP8"),
        1,
    )
    a2, an2 = make(
        src_a, "https://hf.co/Qwen/A95B", "Qwen3.8-2.4T", CARD.format(name="Qwen3.8-2.4T-A95B"), 2
    )

    assert clustering.text_similarity(a1, a2) >= 0.80

    clusters = clustering.cluster_candidates([(a1, an1, 0.90), (a2, an2, 0.80)])
    assert len(clusters) == 1
    primary, _, score, secondary = clusters[0]
    assert primary.id == a1.id, "the higher-scoring member becomes primary"
    assert [s.id for s in secondary] == [a2.id]
    assert score == 0.90


def test_consecutive_releases_stay_separate(src_a):
    """Titles differ only by a version number, but the changelogs are different news."""
    a1, an1 = make(src_a, "https://gh/r/v10", "ollama/ollama v0.32.10", RELEASE_A, 3)
    a2, an2 = make(src_a, "https://gh/r/v11", "ollama/ollama v0.32.11", RELEASE_B, 4)

    assert clustering.text_similarity(a1, a2) < 0.80

    clusters = clustering.cluster_candidates([(a1, an1, 0.90), (a2, an2, 0.85)])
    assert len(clusters) == 2, "two distinct releases must consume two slots"


def test_titles_alone_would_not_have_worked(src_a):
    """Recorded so the reason for choosing text over titles is not lost.

    Both decisive cases score 0.000 on title shingles: the duplicate has identical
    titles that carry no distinguishing content, and the two releases have near-identical
    titles despite being different news.
    """
    a1, _ = make(src_a, "https://gh/r/v10", "ollama/ollama v0.32.10", RELEASE_A, 5)
    a2, _ = make(src_a, "https://gh/r/v11", "ollama/ollama v0.32.11", RELEASE_B, 6)

    title_sim = clustering._jaccard(
        clustering._shingles(a1.title, 5, 6000),
        clustering._shingles(a2.title, 5, 6000),
    )
    # Titles look almost the same while the content does not.
    assert title_sim > 0.80
    assert clustering.text_similarity(a1, a2) < 0.20


def test_different_stories_across_sources_stay_separate(src_a, src_b):
    a1, an1 = make(src_a, "https://a/1", "Qwen release", CARD.format(name="X"), 7)
    a2, an2 = make(src_b, "https://b/1", "Ollama release", RELEASE_A, 8)

    clusters = clustering.cluster_candidates([(a1, an1, 0.9), (a2, an2, 0.8)])
    assert len(clusters) == 2


def test_empty_candidates():
    assert clustering.cluster_candidates([]) == []


def test_article_without_text_never_clusters(src_a):
    """Jaccard on an empty set is 0.0, not 1.0 — two empty articles must not merge."""
    a1, an1 = make(src_a, "https://a/x", "One", "", 9)
    a2, an2 = make(src_a, "https://a/y", "Two", "", 10)
    assert clustering.text_similarity(a1, a2) == 0.0
    assert len(clustering.cluster_candidates([(a1, an1, 0.5), (a2, an2, 0.4)])) == 2
