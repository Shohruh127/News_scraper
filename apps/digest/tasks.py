"""Celery tasks. Failure policy is ADR-002: alert, never auto-disable."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import connectors, extract
from .models import Article, Source

log = logging.getLogger(__name__)


@shared_task(name="digest.fetch_all_sources")
def fetch_all_sources() -> dict:
    """Fan out one task per enabled source. Degraded sources are still fetched."""
    ids = list(Source.objects.filter(enabled=True).values_list("id", flat=True))
    for source_id in ids:
        fetch_source.delay(source_id)
    log.info("dispatched %s fetch tasks", len(ids))
    return {"dispatched": len(ids)}


@shared_task(name="digest.fetch_source")
def fetch_source(source_id: int) -> dict:
    source = Source.objects.get(pk=source_id)
    try:
        items = connectors.fetch(source)
    except Exception as exc:
        _record_failure(source, exc)
        # Swallowed on purpose: one dead source must not abort the others.
        return {"source": source.name, "error": str(exc), "created": 0}

    created, skipped, failed = _store(source, items)
    _record_success(source)
    log.info("%s: %s new, %s duplicate, %s unusable", source.name, created, skipped, failed)
    return {
        "source": source.name,
        "fetched": len(items),
        "created": created,
        "duplicate": skipped,
        "unusable": failed,
    }


def _prefilter(source, items) -> tuple[list[dict], int, int]:
    """Drop what can be rejected without downloading the page.

    Extraction costs one HTTP request per item. A feed with a long archive — OpenAI's
    returns over a thousand entries — would otherwise trigger a thousand downloads on
    every run. Age and known-URL checks are free, so they go first.
    """
    cutoff = timezone.now() - timedelta(days=settings.ARTICLE_MAX_AGE_DAYS)

    fresh = []
    stale = 0
    for item in items:
        published = item.get("published_at")
        # Undated items (html listings) survive; their date is only known after
        # extraction, and there are few of them.
        if published is not None and published < cutoff:
            stale += 1
            continue
        fresh.append(item)

    # Deduplicate within the batch and against what is already stored, both keyed on
    # the canonical URL, which needs no network access.
    by_url = {extract.canonical_url(i["url"]): i for i in fresh}
    known = set(
        Article.objects.filter(canonical_url__in=list(by_url)).values_list(
            "canonical_url", flat=True
        )
    )
    todo = [item for url, item in by_url.items() if url not in known]

    already = len(fresh) - len(todo)
    log.info(
        "%s: %s items, %s stale, %s already known, %s to extract",
        source.name,
        len(items),
        stale,
        already,
        len(todo),
    )
    return todo, stale, already


def _store(source, items) -> tuple[int, int, int]:
    items, stale, already = _prefilter(source, items)
    created, skipped, failed = 0, stale + already, 0
    for item in items:
        try:
            fields = extract.normalize(item, source)
        except extract.ExtractionFailed as exc:
            log.debug("%s: %s", source.name, exc)
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch
            log.warning("%s: unexpected error on %s: %s", source.name, item.get("url"), exc)
            failed += 1
            continue

        try:
            with transaction.atomic():
                Article.objects.create(**fields)
            created += 1
        except IntegrityError:
            # The unique constraints on canonical_url and content_hash are the
            # deduplication mechanism. Hitting one is the normal path, not an error.
            skipped += 1
    return created, skipped, failed


def _record_success(source) -> None:
    source.last_fetched_at = timezone.now()
    source.consecutive_failures = 0
    source.is_degraded = False
    source.last_error = ""
    source.save(
        update_fields=["last_fetched_at", "consecutive_failures", "is_degraded", "last_error"]
    )


def _record_failure(source, exc: Exception) -> None:
    source.consecutive_failures += 1
    source.last_error = f"{type(exc).__name__}: {exc}"[:2000]
    source.last_fetched_at = timezone.now()

    if source.consecutive_failures >= settings.SOURCE_DEGRADED_AFTER:
        source.is_degraded = True
        _alert_once_per_day(source)

    # ADR-002: enabled is never touched here. Only a human disables a source.
    source.save(
        update_fields=[
            "consecutive_failures",
            "last_error",
            "last_fetched_at",
            "is_degraded",
            "last_alerted_on",
        ]
    )
    log.warning(
        "%s failed (%s consecutive): %s",
        source.name,
        source.consecutive_failures,
        source.last_error,
    )


def _alert_once_per_day(source) -> None:
    """Rate-limited so a permanently broken source does not flood the admin chat."""
    today = timezone.localdate()
    if source.last_alerted_on == today:
        return
    source.last_alerted_on = today
    msg = (
        f"Source <b>{source.name}</b> is degraded ({source.consecutive_failures} "
        f"consecutive failures).\nLast error: <code>{source.last_error}</code>"
    )
    log.error(
        "SOURCE DEGRADED: %s — %s consecutive failures — %s",
        source.name,
        source.consecutive_failures,
        source.last_error,
    )
    try:
        from . import publish

        publish.send_admin_alert(msg)
    except Exception as exc:
        log.warning("Could not dispatch admin alert for %s: %s", source.name, exc)


# --- LLM Tasks (on 'llm' queue) ---------------------------------------------

# --- LLM Tasks (on 'llm' queue) ---------------------------------------------


@shared_task(name="digest.triage_article")
def triage_article(article_id: int) -> dict:
    from . import llm

    article = Article.objects.get(pk=article_id)
    passed = llm.triage_article_logic(article)
    return {"article_id": article_id, "status": article.status, "passed": passed}


@shared_task(name="digest.classify_article")
def classify_article(article_id: int) -> dict:
    from . import llm

    article = Article.objects.get(pk=article_id)
    passed = llm.classify_article_logic(article)
    return {"article_id": article_id, "status": article.status, "passed": passed}


@shared_task(name="digest.analyse_for_digest")
def analyse_for_digest(article_ids: list[int]) -> dict:
    """Task on 'llm' queue to run deep editorial analysis for selected digest candidates."""
    from . import llm

    analyses = llm.analyse_for_digest_logic(article_ids)
    return {"analysed": len(analyses), "article_ids": article_ids}


@shared_task(name="digest.triage_and_classify")
def triage_and_classify(trigger_publish_chain: bool = True) -> dict:
    """Run all triage first, then all classification to pay the model swap cost once.

    Acquires an overlap lock so two evening runs cannot overlap.
    Triggers compose_and_publish at the end of the chain.
    """
    from . import llm

    # Overlap protection using Redis lock (T1.8)
    # Records holder identity so a stale lock is identifiable (T1.15).
    lock_client = None
    lock_acquired = True
    try:
        import os
        import platform

        import redis

        lock_client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
        holder = f"{platform.node()}:{os.getpid()}"
        lock_acquired = bool(
            lock_client.set(
                "news_radar:evening_pipeline", holder, nx=True,
                ex=settings.EVENING_LOCK_TTL,
            )
        )
    except Exception as exc:
        log.warning("Could not check Redis lock: %s", exc)

    if not lock_acquired:
        try:
            current_holder = lock_client.get("news_radar:evening_pipeline")
            holder_str = current_holder.decode() if current_holder else "unknown"
        except Exception:
            holder_str = "unknown"
        log.warning(
            "Evening pipeline already running (lock held by %s). Skipping.", holder_str
        )
        return {"status": "skipped", "reason": "lock_held"}

    def _refresh_lock():
        """Refresh lock TTL to prevent stale-lock deadlocks (T1.15)."""
        try:
            lock_client.expire("news_radar:evening_pipeline", settings.EVENING_LOCK_TTL)
        except Exception as exc:
            log.debug("Lock refresh failed: %s", exc)

    try:
        # Phase 1: Fast triage on all untriaged fetched articles
        to_triage = list(Article.objects.filter(status=Article.Status.FETCHED).order_by("id"))
        triaged_count, triage_passed = 0, 0
        log.info("Starting triage batch on %d articles", len(to_triage))
        for art in to_triage:
            passed = llm.triage_article_logic(art)
            triaged_count += 1
            if passed:
                triage_passed += 1
            if triaged_count % 20 == 0:
                _refresh_lock()

        log.info(
            "Triage finished: %d triaged, %d passed to classification",
            triaged_count,
            triage_passed,
        )

        _refresh_lock()

        # Phase 2: Deep classification on all survivors
        to_classify = list(Article.objects.filter(status=Article.Status.TRIAGED).order_by("id"))
        classified_count, classify_passed = 0, 0
        log.info("Starting classification batch on %d articles", len(to_classify))
        for art in to_classify:
            passed = llm.classify_article_logic(art)
            classified_count += 1
            if passed:
                classify_passed += 1
            if classified_count % 10 == 0:
                _refresh_lock()

        log.info(
            "Classification finished: %d classified, %d passed for digest",
            classified_count,
            classify_passed,
        )

        # Causal evening chain: trigger compose_and_publish
        if trigger_publish_chain:
            log.info("Triggering compose_and_publish task in evening chain")
            compose_and_publish.delay()

        return {
            "triaged": triaged_count,
            "triage_survivors": triage_passed,
            "classified": classified_count,
            "classify_survivors": classify_passed,
        }
    finally:
        if lock_client and lock_acquired:
            try:
                lock_client.delete("news_radar:evening_pipeline")
            except Exception as exc:
                log.debug("Error releasing lock: %s", exc)


@shared_task(name="digest.compose_and_publish")
def compose_and_publish(digest_date_str: str | None = None) -> dict:
    """Causal pipeline: select candidates -> editorial deep analysis -> compose -> publish."""
    from datetime import date as dt_date

    from . import llm, publish, ranking
    from .models import Digest

    if digest_date_str:
        target_date = dt_date.fromisoformat(digest_date_str)
    else:
        target_date = timezone.localdate()

    # Step 1: Check if already published
    existing = Digest.objects.filter(
        digest_date=target_date, status=Digest.Status.PUBLISHED
    ).first()
    if existing:
        log.info("Digest for %s is already published.", target_date)
        return {"status": "already_published", "digest_date": str(target_date)}

    try:
        # Step 2: Select candidates
        candidates = ranking.select_digest_candidates(target_date)
        candidate_article_ids = [c[0].id for c in candidates]

        # Step 3: Run Editorial Stage on selected candidates
        if candidate_article_ids:
            log.info(
                "Running editorial stage for %d candidate articles", len(candidate_article_ids)
            )
            llm.analyse_for_digest_logic(candidate_article_ids)

        # Step 4: Compose Digest
        try:
            digest = ranking.compose_digest(target_date)
        except IntegrityError:
            log.warning(
                "Digest for %s already exists. Using existing composed digest.", target_date
            )
            digest = Digest.objects.get(digest_date=target_date)

        # Step 5: Publish Digest
        res = publish.publish_digest(digest)
        return res

    except Exception as exc:
        log.error("Failed in compose_and_publish for %s: %s", target_date, exc)
        publish.send_admin_alert(f"Failed compose_and_publish for {target_date}: {exc}")
        return {"error": str(exc), "digest_date": str(target_date)}
