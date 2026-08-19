"""Model tests. These check the constraints that enforce invariants, not the ORM itself."""

import datetime as dt

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.digest.models import (
    EXCLUDED_MATURITIES,
    Analysis,
    Article,
    Digest,
    DigestItem,
    Feedback,
    Maturity,
    Source,
    Topic,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def source():
    return Source.objects.create(
        name="openai", connector=Source.Connector.RSS, url="https://openai.com/news/rss.xml"
    )


def make_article(source, n=1):
    return Article.objects.create(
        source=source,
        canonical_url=f"https://example.com/a{n}",
        content_hash=f"{n:064d}",
        title=f"Article {n}",
        published_at=timezone.now(),
        extracted_text="x" * 500,
    )


def test_canonical_url_is_unique(source):
    make_article(source, 1)
    with pytest.raises(IntegrityError), transaction.atomic():
        Article.objects.create(
            source=source,
            canonical_url="https://example.com/a1",
            content_hash=f"{99:064d}",
            title="dup",
            extracted_text="y" * 500,
        )


def test_content_hash_is_unique(source):
    """Same text under a different URL must not create a second article."""
    make_article(source, 1)
    with pytest.raises(IntegrityError), transaction.atomic():
        Article.objects.create(
            source=source,
            canonical_url="https://other.com/x",
            content_hash=f"{1:064d}",
            title="same text",
            extracted_text="x" * 500,
        )


def test_digest_date_is_unique():
    """Idempotency is enforced by the database, not by an `if` two workers could both pass."""
    today = dt.date(2026, 8, 14)
    Digest.objects.create(digest_date=today)
    with pytest.raises(IntegrityError), transaction.atomic():
        Digest.objects.create(digest_date=today)


def test_article_cannot_appear_twice_in_one_digest(source):
    d = Digest.objects.create(digest_date=dt.date(2026, 8, 14))
    a = make_article(source, 1)
    DigestItem.objects.create(digest=d, article=a, position=1, score=8.0)
    with pytest.raises(IntegrityError), transaction.atomic():
        DigestItem.objects.create(digest=d, article=a, position=2, score=7.0)


def test_one_reaction_per_user_per_item(source):
    d = Digest.objects.create(digest_date=dt.date(2026, 8, 14))
    item = DigestItem.objects.create(
        digest=d, article=make_article(source, 1), position=1, score=8.0
    )
    Feedback.objects.create(digest_item=item, user_id=42, reaction=Feedback.Reaction.USEFUL)
    with pytest.raises(IntegrityError), transaction.atomic():
        Feedback.objects.create(digest_item=item, user_id=42, reaction=Feedback.Reaction.NOT_USEFUL)


def test_analysis_exposes_topic_and_maturity(source):
    a = make_article(source, 1)
    an = Analysis.objects.create(
        article=a,
        model_tag="gemma4:latest",
        model_digest="c6eb396dbd5992bb",
        payload={
            "primary_topic": "ai_agents",
            "maturity": "live_product",
            "novelty": 7,
            "evidence": 6,
            "production_readiness": 8,
            "reason": "x",
        },
        latency_ms=5590,
    )
    assert an.topic == Topic.AI_AGENTS
    assert an.maturity == Maturity.LIVE_PRODUCT


def test_excluded_maturities_match_schema():
    """CONTENT_SCHEMA.md §3: these are stored but never published."""
    assert EXCLUDED_MATURITIES == {Maturity.ANNOUNCEMENT_ONLY, Maturity.PAPER_ONLY}


def test_topic_enum_matches_schema():
    """Twelve members, exactly as frozen in CONTENT_SCHEMA.md §2."""
    assert len(Topic.values) == 12
    assert "irrelevant" in Topic.values
    assert "relevant" not in Topic.values  # removed in schema v1


def test_source_defaults_are_healthy(source):
    assert source.enabled is True
    assert source.is_degraded is False
    assert source.consecutive_failures == 0


def test_digest_item_sent_as_photo_default(source):
    """DigestItem.sent_as_photo defaults to False until confirmed by Telegram."""
    d = Digest.objects.create(digest_date=dt.date(2026, 8, 20))
    a = make_article(source, 100)
    item = DigestItem.objects.create(digest=d, article=a, position=1, score=0.9)
    assert item.sent_as_photo is False
