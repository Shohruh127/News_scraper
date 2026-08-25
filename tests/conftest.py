from datetime import date

import pytest

from apps.digest.models import Analysis, Article, Digest, DigestItem, Source
from tests.helpers import make_editorial


@pytest.fixture
def source(db):
    return Source.objects.create(
        name="test_source",
        connector=Source.Connector.RSS,
        url="https://example.com/rss",
        priority=80,
    )


@pytest.fixture
def classified_articles(db, source):
    """Create a set of classified articles with diverse topics and maturities."""
    articles = []

    # Art 1: frontier_models, reproducible_open_source (high score, +0.15 open bonus)
    a1 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art1",
        content_hash="h1",
        title="Open Model 30B Released",
        extracted_text="Text 1 " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=a1,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "reproducible_open_source",
            "novelty": 9,
            "evidence": 9,
            "production_readiness": 8,
            "reason": "Open model",
        },
        latency_ms=12000,
    )
    make_editorial(a1, summary_uz="30B ochiq model taqdim etildi.")
    articles.append(a1)

    # Art 2: ai_agents, live_product
    a2 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art2",
        content_hash="h2",
        title="Agent Framework V2",
        extracted_text="Text 2 " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=a2,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "ai_agents",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 9,
            "reason": "Agent update",
        },
        latency_ms=11000,
    )
    make_editorial(a2, summary_uz="MCP spetsifikatsiyasi yangilandi.")
    articles.append(a2)

    # Art 3: paper_only (hard excluded by ranking, NOT by triage)
    a3 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art3",
        content_hash="h3",
        title="Theoretical Paper on LLMs",
        extracted_text="Text 3 " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=a3,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "new_approaches",
            "maturity": "paper_only",
            "novelty": 10,
            "evidence": 10,
            "production_readiness": 1,
            "reason": "Just a paper",
        },
        latency_ms=10000,
    )
    articles.append(a3)

    # Art 4: announcement_only (hard excluded by ranking)
    a4 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/art4",
        content_hash="h4",
        title="Company Announces Future Product",
        extracted_text="Text 4 " * 50,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=a4,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "announcement_only",
            "novelty": 7,
            "evidence": 2,
            "production_readiness": 1,
            "reason": "Future announcement",
        },
        latency_ms=9000,
    )
    articles.append(a4)

    return articles


@pytest.fixture
def digest_with_item(db):
    source = Source.objects.create(
        name="test_source_item",
        connector=Source.Connector.RSS,
        url="https://test.example/rss",
    )
    article = Article.objects.create(
        source=source,
        canonical_url="https://test.example/item",
        content_hash="test-item-1",
        title="Test item",
        extracted_text="Body text for article",
        status=Article.Status.CLASSIFIED,
    )
    make_editorial(article)
    digest = Digest.objects.create(digest_date=date(2026, 8, 24), edition=Digest.Edition.MORNING)
    DigestItem.objects.create(digest=digest, article=article, position=1, score=0.9)
    return digest


@pytest.fixture
def digest_with_two_items(db):
    source = Source.objects.create(
        name="test_source_two_items",
        connector=Source.Connector.RSS,
        url="https://test2.example/rss",
    )
    a1 = Article.objects.create(
        source=source,
        canonical_url="https://test2.example/item-1",
        content_hash="test-item-two-1",
        title="First item",
        extracted_text="Body 1",
        status=Article.Status.CLASSIFIED,
    )
    make_editorial(a1)
    a2 = Article.objects.create(
        source=source,
        canonical_url="https://test2.example/item-2",
        content_hash="test-item-two-2",
        title="Second item",
        extracted_text="Body 2",
        status=Article.Status.CLASSIFIED,
    )
    make_editorial(a2)
    digest = Digest.objects.create(digest_date=date(2026, 8, 24), edition=Digest.Edition.MORNING)
    DigestItem.objects.create(digest=digest, article=a1, position=1, score=0.9)
    DigestItem.objects.create(digest=digest, article=a2, position=2, score=0.8)
    return digest
