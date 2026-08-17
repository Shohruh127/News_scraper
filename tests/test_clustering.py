"""Tests for canonical URL story clustering and same-source non-clustering rule."""

import pytest

from apps.digest import clustering
from apps.digest.models import Analysis, Article, Source


@pytest.fixture
def source1(db):
    return Source.objects.create(
        name="test_src_hn",
        connector=Source.Connector.HN,
        url="https://news.ycombinator.com",
        priority=60,
    )


@pytest.fixture
def source2(db):
    return Source.objects.create(
        name="test_src_openai",
        connector=Source.Connector.RSS,
        url="https://openai.com/news/rss",
        priority=90,
    )


def test_canonical_url_deduplication_across_sources(source1, source2):
    """Articles from different sources sharing the exact canonical URL collapse into 1 cluster."""
    url = "https://openai.com/index/introducing-gpt-5"
    a1 = Article(
        id=1,
        source=source1,
        canonical_url=url,
        title="HN Post: GPT-5 Released",
    )
    an1 = Analysis(
        article=a1,
        stage=Analysis.Stage.CLASSIFICATION,
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 9,
            "evidence": 8,
            "production_readiness": 8,
        },
    )

    a2 = Article(
        id=2,
        source=source2,
        canonical_url=url,
        title="OpenAI Blog: Introducing GPT-5",
    )
    an2 = Analysis(
        article=a2,
        stage=Analysis.Stage.CLASSIFICATION,
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 9,
            "evidence": 9,
            "production_readiness": 9,
        },
    )

    candidates = [(a1, an1, 0.75), (a2, an2, 0.88)]
    clustered = clustering.cluster_candidates(candidates)

    assert len(clustered) == 1
    primary_art, _primary_an, primary_score, secondary_arts = clustered[0]
    # a2 has higher score (0.88), so it is primary
    assert primary_art == a2
    assert primary_score == 0.88
    assert len(secondary_arts) == 1
    assert secondary_arts[0] == a1


def test_same_source_articles_never_cluster(source2):
    """Two articles from the same source must never cluster (e.g. consecutive versions)."""
    a1 = Article(
        id=10,
        source=source2,
        canonical_url="https://github.com/ollama/ollama/releases/tag/v0.32.11",
        title="ollama/ollama v0.32.11",
    )
    an1 = Analysis(
        article=a1,
        stage=Analysis.Stage.CLASSIFICATION,
        payload={
            "primary_topic": "production_engineering",
            "maturity": "reproducible_open_source",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
    )

    a2 = Article(
        id=11,
        source=source2,
        canonical_url="https://github.com/ollama/ollama/releases/tag/v0.32.10",
        title="ollama/ollama v0.32.10",
    )
    an2 = Analysis(
        article=a2,
        stage=Analysis.Stage.CLASSIFICATION,
        payload={
            "primary_topic": "production_engineering",
            "maturity": "reproducible_open_source",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
    )

    candidates = [(a1, an1, 0.80), (a2, an2, 0.79)]
    clustered = clustering.cluster_candidates(candidates)

    assert len(clustered) == 2
    assert len(clustered[0][3]) == 0
    assert len(clustered[1][3]) == 0


def test_distinct_urls_do_not_cluster(source1, source2):
    """Articles with different canonical URLs remain separate items."""
    a1 = Article(
        id=20,
        source=source1,
        canonical_url="https://example.com/robotics-paper",
        title="New Quadruped Robot Policy Training",
    )
    an1 = Analysis(
        article=a1,
        stage=Analysis.Stage.CLASSIFICATION,
        payload={
            "primary_topic": "robotics",
            "maturity": "reproducible_open_source",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
    )

    a2 = Article(
        id=21,
        source=source2,
        canonical_url="https://example.com/whisper-streaming",
        title="Streaming Whisper Voice Pipeline Released",
    )
    an2 = Analysis(
        article=a2,
        stage=Analysis.Stage.CLASSIFICATION,
        payload={
            "primary_topic": "speech_voice",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
    )

    candidates = [(a1, an1, 0.82), (a2, an2, 0.79)]
    clustered = clustering.cluster_candidates(candidates)

    assert len(clustered) == 2
    assert len(clustered[0][3]) == 0
    assert len(clustered[1][3]) == 0


def test_empty_candidates():
    assert clustering.cluster_candidates([]) == []
