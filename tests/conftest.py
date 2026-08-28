from datetime import date

import pytest

from apps.digest.models import Analysis, Article, Digest, DigestItem, Source
from tests.helpers import make_editorial


@pytest.fixture(autouse=True)
def _no_real_broker():
    """Keep `.delay()` inside the test process. Autouse, because one call is enough.

    `CELERY_BROKER_URL` defaults to redis://127.0.0.1:6380/0, which is the port
    docker-compose publishes the stack's redis on. So a test that dispatched a task put a
    real message on the real queue and the running worker executed it against the running
    database — not the test one.

    Measured 2026-08-26: `tests/test_llm.py` calls `tasks.triage_and_classify()` with no
    arguments, so `trigger_publish_chain` took its default of True and fired
    `compose_and_publish.delay(edition=None)`. Two full-suite runs put two of them on the
    queue, and worker-publish composed a digest out of a half-classified backlog both
    times. On the server the same call would reach the live channel.

    Eager mode runs a dispatched task in this process against the test database, so a
    test that fires one still exercises it and nothing leaves pytest. Exceptions stay
    captured in the EagerResult rather than propagating, which is what `.delay()` did
    before.
    """
    from config.celery import app

    previous = app.conf.task_always_eager
    # Assigning app.conf.broker_url does NOT work: broker_url resolves through
    # config_from_object against Django settings on every read, so the assignment is
    # ignored and the connection still points at the live queue. task_always_eager is read
    # by apply_async itself and short-circuits before any broker connection is opened.
    app.conf.task_always_eager = True
    try:
        yield
    finally:
        app.conf.task_always_eager = previous


class FakePublishLockRedis:
    """In-memory stand-in for the client `publish_digest` opens to take its lock."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        value = self.store.get(key)
        return value.encode() if isinstance(value, str) else value

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture(autouse=True)
def fake_publish_lock(monkeypatch):
    """Keep the publish lock out of the stack's Redis. Autouse, because one leak is enough.

    `publish_digest` opens its own client with `redis.Redis.from_url(CELERY_BROKER_URL)`,
    which `_no_real_broker` does not cover: that fixture stops Celery dispatch, and this
    lock never goes through Celery. The URL resolves to redis://127.0.0.1:6380/0, the
    stack's own Redis, and the key is `news_radar:publish_lock:<digest.id>` - a test
    digest and a live digest with the same id are the same key.

    Measured 2026-08-28: one suite run without `TELEGRAM_CHANNEL_ID` raised after the lock
    was taken and left it for its full 300s TTL. Every publish test reusing that id then
    skipped and reported `items_sent: 0`, which read as three unrelated broken tests. The
    same collision can block the running stack from publishing.
    """
    import redis

    fake = FakePublishLockRedis()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url: fake))
    return fake


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
