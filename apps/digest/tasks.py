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
    return {"source": source.name, "fetched": len(items), "created": created,
            "duplicate": skipped, "unusable": failed}


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
        Article.objects.filter(canonical_url__in=list(by_url))
        .values_list("canonical_url", flat=True)
    )
    todo = [item for url, item in by_url.items() if url not in known]

    already = len(fresh) - len(todo)
    log.info("%s: %s items, %s stale, %s already known, %s to extract",
             source.name, len(items), stale, already, len(todo))
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
    source.save(update_fields=["last_fetched_at", "consecutive_failures",
                               "is_degraded", "last_error"])


def _record_failure(source, exc: Exception) -> None:
    source.consecutive_failures += 1
    source.last_error = f"{type(exc).__name__}: {exc}"[:2000]
    source.last_fetched_at = timezone.now()

    if source.consecutive_failures >= settings.SOURCE_DEGRADED_AFTER:
        source.is_degraded = True
        _alert_once_per_day(source)

    # ADR-002: enabled is never touched here. Only a human disables a source.
    source.save(update_fields=["consecutive_failures", "last_error", "last_fetched_at",
                               "is_degraded", "last_alerted_on"])
    log.warning("%s failed (%s consecutive): %s",
                source.name, source.consecutive_failures, source.last_error)


def _alert_once_per_day(source) -> None:
    """Rate-limited so a permanently broken source does not flood the admin chat."""
    today = timezone.localdate()
    if source.last_alerted_on == today:
        return
    source.last_alerted_on = today
    log.error("SOURCE DEGRADED: %s — %s consecutive failures — %s",
              source.name, source.consecutive_failures, source.last_error)
    # Telegram delivery arrives with publish.py in T1.7.
