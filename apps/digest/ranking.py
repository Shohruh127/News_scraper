"""Ranking algorithm, candidate selection, and digest composition.

Rules:
1. Weights are read from settings.RANKING_WEIGHTS (configuration, not code).
2. EXCLUDED_MATURITIES are strictly excluded from the digest.
3. At most DIGEST_MAX_PER_TOPIC per topic, at most DIGEST_MAX_ITEMS total.
4. Never pad: if only 2 items qualify, return a 2-item digest.
5. Composing twice fills an empty slot and never duplicates a block that already has items.
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

    max_items = getattr(settings, "DIGEST_MAX_ITEMS", 6)
    margin = getattr(settings, "DIGEST_SELECT_MARGIN", 2)
    # Select a margin above the block size. Rejecting a candidate whose translation failed
    # is not enough on its own: at six items a block, one rejection leaves a five-item block
    # and a hole in the roundup. The margin is what keeps the block full.
    select_limit = max_items + margin
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
        if len(selected) >= select_limit:
            break

    return selected


def compose_digest(
    digest_date: dt_date | None = None,
    edition: str = Digest.Edition.EVENING,
    candidates: list[tuple[Article, Analysis, float, list[Article]]] | None = None,
) -> Digest:
    """Compose digest and digest items for a specific date and edition.

    An empty, unpublished digest for this slot is filled rather than refused. It used to
    raise on the unique constraint, and `compose_and_publish` caught that, fetched the row
    and returned it — with the candidates it had just paid for silently discarded.
    Measured 2026-08-26: a stray `compose_and_publish` created an empty digest at 11:45,
    and when the real cycle finished at 12:20 it had written five English analyses and five
    Uzbek translations, dropped all five here, and published nothing. That is the
    2026-08-21 failure in a different shape: 815002e stopped an empty digest being marked
    PUBLISHED, but the slot still could not be refilled.

    A digest that already has items is returned untouched. Adding to it would repost what
    already went out, and a published one is a duplicate run — `compose_and_publish` guards
    that case earlier as well.
    """
    if digest_date is None:
        digest_date = timezone.localdate()

    with transaction.atomic():
        digest, created = Digest.objects.get_or_create(
            digest_date=digest_date,
            edition=edition,
            defaults={"status": Digest.Status.COMPOSED},
        )
        if not created and (digest.status == Digest.Status.PUBLISHED or digest.items.exists()):
            return digest

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


def render_roundup_post(digest: Digest) -> str:
    """Render the closing summary post for a completed drip block.

    Links back to the channel messages of every item that landed in that block.
    A failed item is skipped: Telegram links to missing messages 404.
    """
    from .models import DeliveryState

    channel_name = getattr(settings, "TELEGRAM_CHANNEL_USERNAME", "").lstrip("@")
    # Private-channel link format: https://t.me/c/<channel_id_without_-100>/<message_id>
    # Public-channel link format:  https://t.me/<username>/<message_id>
    channel_id = str(getattr(settings, "TELEGRAM_CHANNEL_ID", ""))
    c_prefix = channel_id.removeprefix("-100") if channel_id.startswith("-100") else ""

    items_data = []
    for item in (
        digest.items.select_related("article")
        .prefetch_related("article__analyses")
        .order_by("position")
    ):
        if item.channel_delivery_state != DeliveryState.SENT or not item.channel_message_id:
            continue
        if channel_name:
            channel_url = f"https://t.me/{channel_name}/{item.channel_message_id}"
        elif c_prefix:
            channel_url = f"https://t.me/c/{c_prefix}/{item.channel_message_id}"
        else:
            channel_url = f"https://t.me/{item.channel_message_id}"

        uz = (
            item.article.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ)
            .order_by("-created_at")
            .first()
        )
        headline = (
            uz.payload.get("headline_uz") if uz and uz.payload else ""
        ) or item.article.title
        items_data.append(
            {
                "position": item.position,
                "headline": headline,
                "channel_url": channel_url,
            }
        )

    edition_label = "Tonggi" if digest.edition == Digest.Edition.MORNING else "Kechki"
    # Format date in Uzbek / Latin (e.g. "24-avgust dayjesti")
    months = {
        1: "yanvar",
        2: "fevral",
        3: "mart",
        4: "aprel",
        5: "may",
        6: "iyun",
        7: "iyul",
        8: "avgust",
        9: "sentabr",
        10: "oktabr",
        11: "noyabr",
        12: "dekabr",
    }
    month_name = months.get(digest.digest_date.month, "")
    title = f"⚡️ {edition_label} dayjest ({digest.digest_date.day}-{month_name})"

    return render_to_string(
        "digest/roundup_post.html",
        {
            "title": title,
            "items": items_data,
            "digest": digest,
        },
    ).strip()


def _item_data(item: DigestItem) -> dict:
    """Build the template context for a single DigestItem.

    Shared by render_item_post and render_item_appendix so both templates see the
    same data shape and neither can drift out of sync.
    """
    # Reader-facing text and technical details come from the single-stage Uzbek editorial.
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

    # Rows written before 2026-08-26 carry `technical` on the English analysis. The fallback
    # is temporary: delete it once no EDITORIAL_EN-only article remains inside the 7-day
    # verification window.
    technical = uz_payload.get("technical")
    evidence_level = uz_payload.get("evidence_level")
    if technical is None or evidence_level is None:
        en = (
            item.article.analyses.filter(stage=Analysis.Stage.EDITORIAL_EN)
            .order_by("-created_at")
            .first()
        )
        en_payload = en.payload if en else {}
        if technical is None:
            technical = en_payload.get("technical", {})
        if evidence_level is None:
            evidence_level = en_payload.get("evidence_level", "vendor_claim_only")

    cls = (
        item.article.analyses.filter(stage=Analysis.Stage.CLASSIFICATION)
        .order_by("-created_at")
        .first()
        or uz
    )

    secondary_sources = [
        {
            "title": sec.title,
            "url": sec.canonical_url,
            "source_name": sec.source.name if sec.source else "",
        }
        for sec in item.secondary_articles.all()
    ]

    topic_str = str(cls.topic) if (cls and cls.topic) else "frontier_models"
    maturity_str = str(cls.maturity) if (cls and cls.maturity) else "live_product"

    return {
        "position": item.position,
        "title": item.article.title,
        "url": item.article.canonical_url,
        "source_name": item.article.source.name if item.article.source else "",
        "topic": topic_str,
        "maturity": maturity_str,
        # Uzbek fields. `summary_uz` is the pre-v2 name for the lead and is kept only so
        # translations stored before v2 still render.
        "headline_uz": uz_payload.get("headline_uz", item.article.title),
        "summary_uz": lead_uz,
        "lead_uz": lead_uz,
        "body_1_uz": uz_payload.get("body_1_uz", ""),
        "kicker_uz": uz_payload.get("kicker_uz", ""),
        # Technical appendix. Prose comes from the translation when it exists and from the
        # English otherwise, so digests stored before appendix translation still render.
        # URLs and the install command are never translated.
        "what_was_built": uz_payload.get("what_was_built_uz")
        or technical.get("what_was_built", ""),
        "architecture": uz_payload.get("architecture_uz") or technical.get("architecture", ""),
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


def render_item_post(item: DigestItem) -> str:
    """Render one channel post using post_format v2."""
    data = _item_data(item)
    from . import post_format

    max_chars = getattr(settings, "POST_MAX_CHARS", 700)
    max_sentences = getattr(settings, "POST_MAX_SENTENCES", 4)
    return post_format.render_item_post_v2(data, max_chars=max_chars, max_sentences=max_sentences)


def render_item_appendix(item: DigestItem) -> str:
    """Render a single technical appendix for one news item."""
    rendered = render_to_string("digest/item_appendix.html", _item_data(item))
    lines = [line.strip() for line in rendered.splitlines() if line.strip()]
    return "\n".join(lines)
