"""Tests for ranking, candidate selection, digest composition, and template rendering."""

from datetime import date

import pytest
from django.db import IntegrityError

from apps.digest import ranking
from apps.digest.models import Analysis, Article, Digest, Source


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
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "reproducible_open_source",
            "novelty": 9,
            "evidence": 9,
            "production_readiness": 8,
            "reason": "Open model",
            "summary_uz": "30B ochiq model taqdim etildi.",
        },
        latency_ms=12000,
    )
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
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "ai_agents",
            "maturity": "live_product",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 9,
            "reason": "Agent update",
            "summary_uz": "Agent framework yangilandi.",
        },
        latency_ms=11000,
    )
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


def test_calculate_score_bonuses_and_penalties(db, source):
    # GitHub URL bonus (+0.10) and reproducible_open_source bonus (+0.15)
    art = Article.objects.create(
        source=source,
        canonical_url="https://github.com/test/repo",
        content_hash="h_score",
        title="Test Score Repo",
        extracted_text="Content " * 50,
    )
    analysis = Analysis.objects.create(
        article=art,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "production_engineering",
            "maturity": "reproducible_open_source",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
        },
        latency_ms=5000,
    )

    score = ranking.calculate_score(art, analysis)
    # Base (high scores) + open_source (+0.15) + github (+0.10) = well above 0.8
    assert score > 0.8


def test_low_evidence_penalty(db, source):
    """evidence <= 3 causes a -0.15 penalty."""
    art = Article.objects.create(
        source=source,
        canonical_url="https://example.com/vendor-claim",
        content_hash="h_low_ev",
        title="Vendor Claim Article",
        extracted_text="Content " * 50,
    )
    analysis_low = Analysis.objects.create(
        article=art,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "startups",
            "maturity": "live_product",
            "novelty": 5,
            "evidence": 2,
            "production_readiness": 5,
        },
        latency_ms=5000,
    )
    score_low = ranking.calculate_score(art, analysis_low)

    art2 = Article.objects.create(
        source=source,
        canonical_url="https://example.com/good-evidence",
        content_hash="h_hi_ev",
        title="Good Evidence Article",
        extracted_text="Content " * 50,
    )
    analysis_hi = Analysis.objects.create(
        article=art2,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "startups",
            "maturity": "live_product",
            "novelty": 5,
            "evidence": 8,
            "production_readiness": 5,
        },
        latency_ms=5000,
    )
    score_hi = ranking.calculate_score(art2, analysis_hi)
    assert score_hi > score_low


def test_hard_exclusion_and_candidate_selection(db, classified_articles):
    candidates = ranking.select_digest_candidates()
    # 4 articles created: a1 and a2 are valid; a3 (paper_only) and a4 (announcement_only) excluded
    assert len(candidates) == 2
    articles_in_digest = [c[0] for c in candidates]
    assert classified_articles[0] in articles_in_digest
    assert classified_articles[1] in articles_in_digest
    assert classified_articles[2] not in articles_in_digest
    assert classified_articles[3] not in articles_in_digest


def test_no_padding_rule(db, classified_articles):
    """If only 2 items qualify, the digest must have exactly 2 items (never pad to 7)."""
    digest = ranking.compose_digest(date(2026, 8, 14))
    assert digest.items.count() == 2
    assert digest.status == Digest.Status.COMPOSED


def test_compose_digest_idempotency_constraint(db, classified_articles):
    """Calling compose_digest twice for the same date must raise IntegrityError."""
    target_d = date(2026, 8, 14)
    ranking.compose_digest(target_d)

    with pytest.raises(IntegrityError):
        ranking.compose_digest(target_d)


def test_render_templates_snapshot(db, classified_articles):
    target_d = date(2026, 8, 14)
    digest = ranking.compose_digest(target_d)

    post_html = ranking.render_channel_post(digest)
    assert "2026-08-14" in post_html
    assert "Open Model 30B Released" in post_html
    assert "30B ochiq model taqdim etildi." in post_html
    assert "<b>" in post_html and "</b>" in post_html

    comment_html = ranking.render_group_comment(digest)
    assert "2026-08-14" in comment_html
    assert "Open Model 30B Released" in comment_html
