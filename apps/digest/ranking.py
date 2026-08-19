"""Ranking algorithm, candidate selection, and digest composition.

Rules:
1. Weights are read from settings.RANKING_WEIGHTS (configuration, not code).
2. EXCLUDED_MATURITIES are strictly excluded from the digest.
3. At most DIGEST_MAX_PER_TOPIC per topic, at most DIGEST_MAX_ITEMS total.
4. Never pad: if only 2 items qualify, return a 2-item digest.
5. Idempotency is enforced by the database unique constraint on Digest.digest_date.
"""

import logging
from collections import Counter
from datetime import date as dt_date
from datetime import datetime, timedelta
from datetime import time as dt_time

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from . import clustering
from .models import EXCLUDED_MATURITIES, Analysis, Article, Digest, DigestItem, Maturity, Topic
from .story_identity import subject_key

log = logging.getLogger(__name__)


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
    if "github.com" in article.canonical_url or (
        article.source and article.source.connector == "github"
    ):
        score += 0.10

    # Penalties — only fields available in M1
    if evidence <= 3:
        score -= 0.15

    return round(max(0.0, score), 4)


def select_digest_candidates(
    target_date: dt_date | None = None,
) -> list[tuple[Article, Analysis, float, list[Article]]]:
    """Select, score, cluster, and diversify top ranking articles for the digest.

    Enforces:
    - Relative cutoff to target_date
    - Strict exclusion of articles already published in any previous digest
    - Canonical URL dedup before topic diversification (same-source never merges)
    - Max per topic and total limits
    - Never pads.
    """
    if target_date is None:
        target_date = timezone.localdate()

    max_age_days = getattr(settings, "ARTICLE_MAX_AGE_DAYS", 7)
    end_of_day = timezone.make_aware(datetime.combine(target_date, dt_time.max))
    cutoff = end_of_day - timedelta(days=max_age_days)

    max_items = getattr(settings, "DIGEST_MAX_ITEMS", 7)
    max_per_topic = getattr(settings, "DIGEST_MAX_PER_TOPIC", 2)
    max_per_subject = getattr(settings, "DIGEST_MAX_PER_SUBJECT", 1)

    # Query classified articles strictly excluding already published articles
    articles = (
        Article.objects.filter(
            status=Article.Status.CLASSIFIED,
            digestitem__isnull=True,
            secondary_in_digest_items__isnull=True,
            fetched_at__gte=cutoff,
            fetched_at__lte=end_of_day,
        )
        .select_related("source")
        .prefetch_related("analyses")
    )

    scored_candidates: list[tuple[Article, Analysis, float]] = []
    for art in articles:
        # Prefer classification stage analysis; fallback to latest
        analysis = (
            art.analyses.filter(stage=Analysis.Stage.CLASSIFICATION).order_by("-created_at").first()
            or art.analyses.order_by("-created_at").first()
        )
        if not analysis:
            continue

        # Hard exclusion: excluded maturities & irrelevant topic never enter a digest
        if analysis.maturity in EXCLUDED_MATURITIES or analysis.topic == Topic.IRRELEVANT:
            continue

        score = calculate_score(art, analysis)
        scored_candidates.append((art, analysis, score))

    # Sort descending by score before canonical URL dedup
    scored_candidates.sort(key=lambda x: x[2], reverse=True)

    # Apply canonical URL dedup BEFORE topic diversification
    clustered_candidates = clustering.cluster_candidates(scored_candidates)

    # Diversification, applied to clusters so a merged story is counted once.
    topic_counts: Counter = Counter()
    subject_counts: Counter = Counter()
    selected: list[tuple[Article, Analysis, float, list[Article]]] = []

    for art, analysis, score, secondary_arts in clustered_candidates:
        topic = analysis.topic
        subject = (subject_key(art.canonical_url), topic)
        if topic_counts[topic] >= max_per_topic:
            continue
        if subject_counts[subject] >= max_per_subject:
            continue

        selected.append((art, analysis, score, secondary_arts))
        topic_counts[topic] += 1
        subject_counts[subject] += 1
        if len(selected) >= max_items:
            break

    return selected


def compose_digest(
    digest_date: dt_date | None = None,
    candidates: list[tuple[Article, Analysis, float, list[Article]]] | None = None,
) -> Digest:
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

        selected = candidates if candidates is not None else select_digest_candidates(digest_date)
        for pos, (article, _analysis, score, secondary_arts) in enumerate(selected, start=1):
            item = DigestItem.objects.create(
                digest=digest,
                article=article,
                position=pos,
                score=score,
            )
            if secondary_arts:
                item.secondary_articles.set(secondary_arts)

    return digest


def render_channel_post(digest: Digest) -> str:
    """Render Telegram channel post HTML (leadership/overview format).

    Requires an editorial analysis with non-empty summary_uz for every item.
    Missing Uzbek is a strict error, never a fallback to English.
    """
    items_data = []
    for item in (
        digest.items.select_related("article", "article__source")
        .prefetch_related("secondary_articles", "secondary_articles__source")
        .all()
    ):
        # The reader-facing text comes from the translation stage only (ADR-005).
        editorial = (
            item.article.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ)
            .order_by("-created_at")
            .first()
        )
        payload = editorial.payload if editorial else {}
        summary_uz = payload.get("summary_uz", "").strip()

        if not summary_uz:
            raise ValueError(
                f"DigestItem #{item.position} (article ID {item.article_id}: "
                f"'{item.article.title}') lacks an editorial_uz analysis with non-empty "
                "'summary_uz'. English fallback is prohibited."
            )

        # Classification info for tags
        cls_analysis = (
            item.article.analyses.filter(stage=Analysis.Stage.CLASSIFICATION)
            .order_by("-created_at")
            .first()
            or editorial
        )
        topic = cls_analysis.topic if cls_analysis else "ai"
        maturity = cls_analysis.maturity if cls_analysis else "product"

        # Clustered secondary sources
        secondary_sources = [
            {
                "title": sec.title,
                "url": sec.canonical_url,
                "source_name": sec.source.name if sec.source else "",
            }
            for sec in item.secondary_articles.all()
        ]

        src_name = item.article.source.name if item.article.source else ""
        items_data.append(
            {
                "position": item.position,
                "title": item.article.title,
                "url": item.article.canonical_url,
                "source_name": src_name,
                "topic": str(topic),
                "maturity": str(maturity),
                "summary_uz": summary_uz,
                "score": item.score,
                "secondary_sources": secondary_sources,
            }
        )

    return render_to_string(
        "digest/channel_post.html",
        {
            "digest_date": digest.digest_date,
            "items": items_data,
            "total_items": len(items_data),
        },
    ).strip()


def render_group_comment(digest: Digest) -> str:
    """Render Telegram linked group comment HTML (technical appendix format).

    Requires an editorial analysis for every item.
    """
    items_data = []
    for item in (
        digest.items.select_related("article", "article__source")
        .prefetch_related("secondary_articles", "secondary_articles__source")
        .all()
    ):
        # The technical appendix reads the English stage: repo URLs, licences and install
        # commands are English artefacts and are deliberately not translated (ADR-005).
        editorial = (
            item.article.analyses.filter(stage=Analysis.Stage.EDITORIAL_EN)
            .order_by("-created_at")
            .first()
        )
        if not editorial:
            raise ValueError(
                f"DigestItem #{item.position} (article ID {item.article_id}: "
                f"'{item.article.title}') lacks an editorial_en analysis for "
                "technical appendix rendering."
            )

        payload = editorial.payload or {}
        technical = payload.get("technical", {})

        cls_analysis = (
            item.article.analyses.filter(stage=Analysis.Stage.CLASSIFICATION)
            .order_by("-created_at")
            .first()
            or editorial
        )

        secondary_sources = [
            {
                "title": sec.title,
                "url": sec.canonical_url,
                "source_name": sec.source.name if sec.source else "",
            }
            for sec in item.secondary_articles.all()
        ]

        items_data.append(
            {
                "position": item.position,
                "title": item.article.title,
                "url": item.article.canonical_url,
                "topic": str(cls_analysis.topic if cls_analysis else ""),
                "maturity": str(cls_analysis.maturity if cls_analysis else ""),
                "what_was_built": technical.get("what_was_built", ""),
                "architecture": technical.get("architecture", ""),
                "license": technical.get("license", ""),
                "repo_url": technical.get("repo_url", ""),
                "api_url": technical.get("api_url", ""),
                "hardware": technical.get("hardware", ""),
                "install": technical.get("install", ""),
                "benchmarks": technical.get("benchmarks", ""),
                "limitations": technical.get("limitations", ""),
                "local_deployable": technical.get("local_deployable", False),
                "uzbekistan_application_uz": payload.get("uzbekistan_application_uz", ""),
                "why_it_matters_uz": payload.get("why_it_matters_uz", ""),
                "secondary_sources": secondary_sources,
            }
        )

    return render_to_string(
        "digest/group_comment.html",
        {
            "digest_date": digest.digest_date,
            "items": items_data,
            "total_items": len(items_data),
        },
    ).strip()


#: Translated fields every post has. Anything else ending in `_uz` came from an archetype block.
_COMMON_UZ_KEYS = frozenset(
    {
        "headline_uz",
        "summary_uz",
        "why_it_matters_uz",
        "leadership_uz",
        "uzbekistan_application_uz",
        "lead_uz",
        "body_1_uz",
        "body_2_uz",
        "kicker_uz",
        "link_anchor_uz",
    }
)


def _item_data(item: DigestItem) -> dict:
    """Build the template context for a single DigestItem.

    Shared by render_item_post and render_item_appendix so both templates see the
    same data shape and neither can drift out of sync.
    """
    # Reader-facing text comes from the translation stage only (ADR-005).
    uz = (
        item.article.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ)
        .order_by("-created_at")
        .first()
    )
    uz_payload = uz.payload if uz else {}
    lead_uz = uz_payload.get("lead_uz", "").strip() or uz_payload.get("summary_uz", "").strip()
    if not lead_uz:
        raise ValueError(
            f"DigestItem #{item.position} (article {item.article_id}: "
            f"'{item.article.title}') lacks editorial_uz with non-empty 'lead_uz' or 'summary_uz'."
        )

    # English analysis for the technical appendix.
    en = (
        item.article.analyses.filter(stage=Analysis.Stage.EDITORIAL_EN)
        .order_by("-created_at")
        .first()
    )
    if not en:
        raise ValueError(
            f"DigestItem #{item.position} (article {item.article_id}: "
            f"'{item.article.title}') lacks editorial_en analysis."
        )
    en_payload = en.payload or {}
    technical = en_payload.get("technical", {})

    cls = (
        item.article.analyses.filter(stage=Analysis.Stage.CLASSIFICATION)
        .order_by("-created_at")
        .first()
        or en
    )

    secondary_sources = [
        {
            "title": sec.title,
            "url": sec.canonical_url,
            "source_name": sec.source.name if sec.source else "",
        }
        for sec in item.secondary_articles.all()
    ]

    evidence_level = en_payload.get("evidence_level", "vendor_claim_only")
    maturity = str(cls.maturity if cls else "")
    kicker_uz = uz_payload.get("kicker_uz", "")
    # Suppress kicker only when vendor_claim_only AND announcement_only
    if evidence_level == "vendor_claim_only" and maturity == "announcement_only":
        kicker_uz = ""

    topic_str = str(cls.topic) if (cls and cls.topic) else "frontier_models"
    maturity_str = str(cls.maturity) if (cls and cls.maturity) else "live_product"

    return {
        "position": item.position,
        "title": item.article.title,
        "url": item.article.canonical_url,
        "source_name": item.article.source.name if item.article.source else "",
        "topic": topic_str,
        "maturity": maturity_str,
        # The archetype lives in the English payload; its translated detail lines live in the
        # Uzbek one, flattened there by the translation stage.
        "archetype": en_payload.get("archetype", ""),
        "detail": {
            k: v for k, v in uz_payload.items() if k.endswith("_uz") and k not in _COMMON_UZ_KEYS
        },
        # Uzbek fields
        "headline_uz": uz_payload.get("headline_uz", item.article.title),
        "summary_uz": lead_uz,
        "lead_uz": lead_uz,
        "body_1_uz": uz_payload.get("body_1_uz", ""),
        "body_2_uz": uz_payload.get("body_2_uz", ""),
        "kicker_uz": kicker_uz,
        "link_anchor_uz": uz_payload.get("link_anchor_uz", ""),
        "why_it_matters_uz": uz_payload.get("why_it_matters_uz", ""),
        "leadership_uz": uz_payload.get("leadership_uz", ""),
        "uzbekistan_application_uz": (
            uz_payload.get("uzbekistan_application_uz", "")
            or en_payload.get("uzbekistan_application_uz", "")
        ),
        # Technical appendix. Prose comes from the translation when it exists and from the
        # English otherwise, so digests stored before appendix translation still render.
        # URLs and the install command are never translated.
        "what_was_built": uz_payload.get("what_was_built_uz")
        or technical.get("what_was_built", ""),
        "architecture": uz_payload.get("architecture_uz") or technical.get("architecture", ""),
        "hardware": uz_payload.get("hardware_uz") or technical.get("hardware", ""),
        "benchmarks": uz_payload.get("benchmarks_uz") or technical.get("benchmarks", ""),
        "limitations": uz_payload.get("limitations_uz") or technical.get("limitations", ""),
        "license": technical.get("license", ""),
        "repo_url": technical.get("repo_url", ""),
        "api_url": technical.get("api_url", ""),
        "install": technical.get("install", ""),
        "local_deployable": technical.get("local_deployable", False),
        "evidence_level": evidence_level,
        # Clustering
        "secondary_sources": secondary_sources,
        "score": item.score,
    }


#: archetype -> template. A value missing from this map falls back to the plain post, which is
#: the rule the whole feature rests on: the archetype block is an enhancement, and its absence
#: simplifies the layout rather than losing the post.
ARCHETYPE_TEMPLATES = {
    "release": "digest/item_release.html",
    "agent_protocol": "digest/item_agent_protocol.html",
    "risk_hardening": "digest/item_risk_hardening.html",
    "policy": "digest/item_policy.html",
    "research": "digest/item_research.html",
    "company_product": "digest/item_company_product.html",
}

#: The field each template cannot render without. Absent -> fall back.
ARCHETYPE_REQUIRED = {
    "release": ("what_changed_uz",),
    "agent_protocol": ("connects_uz",),
    "risk_hardening": ("risk_uz", "mitigation_uz"),
    "policy": ("who_issued_uz", "who_must_comply_uz"),
    "research": ("claim_uz",),
    "company_product": ("what_they_do_uz",),
}


def render_item_post(item: DigestItem) -> str:
    """Render one channel post, choosing v2 post_format or legacy archetype template."""
    data = _item_data(item)
    if getattr(settings, "POST_FORMAT_V2_ENABLED", False):
        from . import post_format

        max_chars = getattr(settings, "POST_MAX_CHARS", 900)
        return post_format.render_item_post_v2(data, max_chars=max_chars)

    archetype = data.get("archetype", "")
    template = ARCHETYPE_TEMPLATES.get(archetype)

    if template is None:
        if archetype:
            log.info(
                "Unknown archetype %r on item #%s; using the plain post", archetype, item.position
            )
        return render_to_string("digest/item_post.html", data).strip()

    missing = [f for f in ARCHETYPE_REQUIRED[archetype] if not data["detail"].get(f)]
    if missing:
        log.warning(
            "Archetype %s on item #%s lacks %s; using the plain post",
            archetype,
            item.position,
            ", ".join(missing),
        )
        return render_to_string("digest/item_post.html", data).strip()

    return render_to_string(template, data).strip()


def render_item_appendix(item: DigestItem) -> str:
    """Render a single technical appendix for one news item."""
    return render_to_string("digest/item_appendix.html", _item_data(item)).strip()
