"""Ranking algorithm, candidate selection, and digest composition.

Rules:
1. Weights are read from settings.RANKING_WEIGHTS (configuration, not code).
2. EXCLUDED_MATURITIES are strictly excluded from the digest.
3. At most DIGEST_MAX_PER_TOPIC per topic, at most DIGEST_MAX_ITEMS total.
4. Never pad: if only 2 items qualify, return a 2-item digest.
5. Idempotency is enforced by the database unique constraint on Digest.digest_date.
"""

import html
from collections import Counter
from datetime import date as dt_date
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import EXCLUDED_MATURITIES, Analysis, Article, Digest, DigestItem, Maturity, Topic


def calculate_score(article: Article, analysis: Analysis) -> float:
    """Calculate weighted ranking score with bonuses and penalties.

    Weights are read from settings.RANKING_WEIGHTS. The M1 classification
    schema (CONTENT_SCHEMA.md §4) provides: novelty, evidence,
    production_readiness as integers 1-10.  No other numeric dimensions exist
    in M1; the deep-analysis fields (technical.*, evidence_level) are M2 only
    and must NOT be referenced here.
    """
    weights = getattr(
        settings,
        "RANKING_WEIGHTS",
        {
            "novelty": 0.35,
            "evidence": 0.30,
            "production_readiness": 0.15,
            "source_credibility": 0.10,
            "audience_relevance": 0.10,
        },
    )

    payload = analysis.payload or {}
    novelty = float(payload.get("novelty", 5))
    evidence = float(payload.get("evidence", 5))
    readiness = float(payload.get("production_readiness", 5))

    # Base normalized dimensions (each 0.0 to weight_i)
    w_novelty = (novelty / 10.0) * weights.get("novelty", 0.35)
    w_evidence = (evidence / 10.0) * weights.get("evidence", 0.30)
    w_readiness = (readiness / 10.0) * weights.get("production_readiness", 0.15)

    # Source credibility from source priority (0 to 100)
    src_priority = article.source.priority if article.source else 50
    w_source = (src_priority / 100.0) * weights.get("source_credibility", 0.10)

    # Audience relevance (1.0 for technical topics, 0.0 for irrelevant)
    topic_rel = 1.0 if analysis.topic != Topic.IRRELEVANT else 0.0
    w_audience = topic_rel * weights.get("audience_relevance", 0.10)

    score = w_novelty + w_evidence + w_readiness + w_source + w_audience

    # Bonuses — only fields available in M1
    if analysis.maturity == Maturity.REPRODUCIBLE_OPEN_SOURCE:
        score += 0.15
    if (
        "github.com" in article.canonical_url
        or (article.source and article.source.connector == "github")
    ):
        score += 0.10

    # Penalties — only fields available in M1
    if evidence <= 3:
        score -= 0.15

    return round(max(0.0, score), 4)


def select_digest_candidates(
    target_date: dt_date | None = None,
) -> list[tuple[Article, Analysis, float]]:
    """Select and diversify top ranking articles for the digest.

    Enforces max per topic and total limits. Never pads.
    """
    if target_date is None:
        target_date = timezone.localdate()

    max_age_days = getattr(settings, "ARTICLE_MAX_AGE_DAYS", 7)
    cutoff = timezone.now() - timedelta(days=max_age_days)

    max_items = getattr(settings, "DIGEST_MAX_ITEMS", 7)
    max_per_topic = getattr(settings, "DIGEST_MAX_PER_TOPIC", 2)

    # Query classified articles
    articles = (
        Article.objects.filter(
            status=Article.Status.CLASSIFIED,
            fetched_at__gte=cutoff,
        )
        .select_related("source")
        .prefetch_related("analyses")
    )

    scored_candidates: list[tuple[Article, Analysis, float]] = []
    for art in articles:
        analysis = art.analyses.order_by("-created_at").first()
        if not analysis:
            continue

        # Hard exclusion: excluded maturities & irrelevant topic never enter a digest
        if analysis.maturity in EXCLUDED_MATURITIES or analysis.topic == Topic.IRRELEVANT:
            continue

        score = calculate_score(art, analysis)
        scored_candidates.append((art, analysis, score))

    # Sort descending by score
    scored_candidates.sort(key=lambda x: x[2], reverse=True)

    # Apply topic diversification limit
    topic_counts: Counter = Counter()
    selected: list[tuple[Article, Analysis, float]] = []

    for art, analysis, score in scored_candidates:
        topic = analysis.topic
        if topic_counts[topic] < max_per_topic:
            selected.append((art, analysis, score))
            topic_counts[topic] += 1
            if len(selected) >= max_items:
                break

    return selected


def compose_digest(digest_date: dt_date | None = None) -> Digest:
    """Compose digest and digest items for a specific date.

    A second call for the same date will fail on the unique constraint of Digest.digest_date.
    """
    if digest_date is None:
        digest_date = timezone.localdate()

    with transaction.atomic():
        digest = Digest.objects.create(
            digest_date=digest_date,
            status=Digest.Status.COMPOSED,
        )

        candidates = select_digest_candidates(digest_date)
        for pos, (article, _analysis, score) in enumerate(candidates, start=1):
            DigestItem.objects.create(
                digest=digest,
                article=article,
                position=pos,
                score=score,
            )

    return digest


def render_channel_post(digest: Digest) -> str:
    """Render Telegram channel post HTML (leadership/overview format)."""
    items_data = []
    for item in digest.items.select_related("article", "article__source").all():
        analysis = item.article.analyses.order_by("-created_at").first()
        payload = analysis.payload if analysis else {}
        summary_uz = payload.get("summary_uz") or payload.get("reason") or item.article.title
        topic = analysis.topic if analysis else "ai"
        maturity = analysis.maturity if analysis else "product"

        items_data.append({
            "position": item.position,
            "title": html.escape(item.article.title),
            "url": item.article.canonical_url,
            "source_name": html.escape(item.article.source.name if item.article.source else ""),
            "topic": html.escape(str(topic)),
            "maturity": html.escape(str(maturity)),
            "summary_uz": html.escape(summary_uz),
            "score": item.score,
        })

    return render_to_string(
        "digest/channel_post.html",
        {
            "digest_date": digest.digest_date,
            "items": items_data,
            "total_items": len(items_data),
        },
    ).strip()


def render_group_comment(digest: Digest) -> str:
    """Render Telegram linked group comment HTML (technical appendix format)."""
    items_data = []
    for item in digest.items.select_related("article", "article__source").all():
        analysis = item.article.analyses.order_by("-created_at").first()
        payload = analysis.payload if analysis else {}
        technical = payload.get("technical", {})

        items_data.append({
            "position": item.position,
            "title": html.escape(item.article.title),
            "url": item.article.canonical_url,
            "topic": html.escape(str(analysis.topic if analysis else "")),
            "maturity": html.escape(str(analysis.maturity if analysis else "")),
            "what_was_built": html.escape(technical.get("what_was_built", "")),
            "architecture": html.escape(technical.get("architecture", "")),
            "license": html.escape(technical.get("license", "")),
            "repo_url": technical.get("repo_url", ""),
            "api_url": technical.get("api_url", ""),
            "hardware": html.escape(technical.get("hardware", "")),
            "install": html.escape(technical.get("install", "")),
            "benchmarks": html.escape(technical.get("benchmarks", "")),
            "limitations": html.escape(technical.get("limitations", "")),
            "local_deployable": technical.get("local_deployable", False),
            "uzbekistan_application_uz": html.escape(payload.get("uzbekistan_application_uz", "")),
            "reasoning_en": html.escape(payload.get("reasoning_en", "")),
        })

    return render_to_string(
        "digest/group_comment.html",
        {
            "digest_date": digest.digest_date,
            "items": items_data,
            "total_items": len(items_data),
        },
    ).strip()
