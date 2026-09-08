"""Celery tasks. Failure policy is ADR-002: alert, never auto-disable."""

import json
import logging
from datetime import date as dt_date
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import connectors, extract
from .models import Article, DeliveryState, Source

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

        if publish.send_admin_alert(msg):
            source.last_alerted_on = today
        else:
            log.warning("Admin alert for %s was not delivered; will retry", source.name)
    except Exception as exc:
        log.warning("Could not dispatch admin alert for %s: %s", source.name, exc)


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


#: A triage message whose slot is older than this is beat replaying an entry it missed
#: during an outage, not a worker picking the message up late: the LLM stage has run for
#: hours, never for half a day. Measured 2026-09-08 -- see slot_for.
STALE_SLOT_AFTER = timedelta(hours=12)

#: Beat dispatches at or after the slot minute and the worker shares its clock, so a message
#: never arrives before its slot; the grace covers clocks a few seconds apart.
SLOT_GRACE = timedelta(minutes=5)

TRIAGE_TASK = "digest.triage_and_classify"


def slot_for(edition: str, now=None):
    """The scheduled time the triage message for `edition` belongs to.

    That is the latest occurrence, at or before now, of the beat entry that dispatches
    `digest.triage_and_classify` with this edition, read from the code's own schedule in
    `config/celery.py`. The edition is never re-derived from the clock; only its date is
    fixed here, at the head of the chain. None when no entry schedules the edition (a
    manual run), in which case the chain composes for today as it always has.

    Why the date cannot wait for compose time: on 2026-09-08 the stack came up at 14:57
    after missing the previous evening's entry. django-celery-beat treats an entry whose
    `last_run_at` is behind its schedule as due and dispatches it once at startup, so it
    replayed the 2026-09-07 evening entry at once; `compose_and_publish` stamped it with
    *today's* date, composed the 2026-09-08 evening slot at 14:58 with one item, and the
    genuine 18:00 run then found the slot published and composed nothing. A genuine
    message the worker picks up after midnight would do the same to the next day.

    Reads one hour and one minute from the entry: the triage entries are simple daily
    crontabs, and tests/test_slot_guard.py pins them to that shape.
    """
    from celery import current_app

    now = timezone.localtime(now)
    for entry in current_app.conf.beat_schedule.values():
        if entry.get("task") != TRIAGE_TASK or entry.get("kwargs", {}).get("edition") != edition:
            continue
        schedule = entry["schedule"]
        slot = now.replace(
            hour=min(schedule.hour), minute=min(schedule.minute), second=0, microsecond=0
        )
        if slot > now + SLOT_GRACE:
            slot -= timedelta(days=1)
        return slot
    return None


@shared_task(name="digest.triage_and_classify")
def triage_and_classify(trigger_publish_chain: bool = True, edition: str | None = None) -> dict:
    """Run all triage first, then all classification to pay the model swap cost once.

    Acquires an overlap lock so two evening runs cannot overlap.
    Triggers compose_and_publish at the end of the chain.

    `edition` is carried through to that chained call. Without it compose_and_publish
    derives the edition from the clock at the moment it runs, so a morning cycle whose
    LLM stage ran past 14:00 would publish into the evening slot.

    The slot's *date* is fixed here too, before anything else runs: `slot_for` says
    which occurrence of the edition's beat entry this message belongs to, and a slot
    older than STALE_SLOT_AFTER is refused outright, with an admin alert, because it is
    beat replaying an entry it missed during an outage. Composing it would stamp it
    with today's date and take today's slot (2026-09-08). The trade-off is that the
    missed edition is skipped rather than published a day late: a day-late block
    would drip ahead of the fresh one and push today's news back by twelve hours.
    """
    from . import llm, publish

    slot = slot_for(edition) if edition else None
    digest_date_str = slot.date().isoformat() if slot else None
    if slot is not None:
        age = timezone.localtime() - slot
        if age > STALE_SLOT_AFTER:
            hours = age.total_seconds() / 3600
            message = (
                f"Refusing a stale {edition} triage message: its slot was "
                f"{slot:%Y-%m-%d %H:%M}, {hours:.1f}h ago. Beat replays an entry it missed "
                f"during an outage; composing it now would stamp today's date on it and "
                f"take today's {edition} slot. The missed edition is skipped."
            )
            log.error(message)
            publish.send_admin_alert(message)
            return {
                "status": "stale_slot",
                "edition": edition,
                "slot": slot.isoformat(),
                "age_hours": round(hours, 1),
            }

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
                "news_radar:evening_pipeline",
                holder,
                nx=True,
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
        log.warning("Evening pipeline already running (lock held by %s). Skipping.", holder_str)
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
            log.info(
                "Triggering compose_and_publish for %s (%s)", digest_date_str or "today", edition
            )
            compose_and_publish.delay(digest_date_str=digest_date_str, edition=edition)

        return {
            "digest_date": digest_date_str,
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
def compose_and_publish(
    digest_date_str: str | None = None,
    edition: str | None = None,
) -> dict:
    """Causal pipeline: select candidates -> editorial deep analysis -> compose -> publish."""
    from . import llm, publish, ranking, verification
    from .models import Digest

    if digest_date_str:
        target_date = dt_date.fromisoformat(digest_date_str)
    else:
        target_date = timezone.localdate()

    if not edition:
        current_hour = timezone.localtime().hour
        edition = Digest.Edition.MORNING if current_hour < 14 else Digest.Edition.EVENING

    # Step 1: Check if already published
    existing = Digest.objects.filter(
        digest_date=target_date, edition=edition, status=Digest.Status.PUBLISHED
    ).first()
    if existing:
        log.info("Digest for %s (%s) is already published.", target_date, edition)
        return {"status": "already_published", "digest_date": str(target_date), "edition": edition}

    try:
        # Step 2: Select candidates
        candidates = ranking.select_digest_candidates(target_date)
        candidate_article_ids = [c[0].id for c in candidates]

        # Step 3: Run the editorial stage on the selected candidates.
        #
        # The return value is the set of articles that produced a usable translation, and
        # discarding it is what let an item whose editorial failed reach the renderer, raise
        # ValueError and fail the whole digest. Selection carries a margin (see
        # DIGEST_SELECT_MARGIN) so dropping one or two still fills the block.
        if candidate_article_ids:
            log.info(
                "Running editorial stage for %d candidate articles", len(candidate_article_ids)
            )
            analyses = llm.analyse_for_digest_logic(candidate_article_ids)
            translated = {a.article_id for a in analyses}
            dropped = [cid for cid in candidate_article_ids if cid not in translated]
            if dropped:
                log.warning(
                    "Dropping %d candidates with no usable translation: %s",
                    len(dropped),
                    dropped,
                )
            max_items = getattr(settings, "DIGEST_MAX_ITEMS", 6)

            candidates = [c for c in candidates if c[0].id in translated][:max_items]

        # Step 4: Compose Digest. compose_digest owns the already-exists case now: it fills
        # an empty slot and leaves a slot that already has items alone. Catching
        # IntegrityError here meant a re-run threw away every candidate it had just paid
        # the editorial and translation stages for.
        digest = ranking.compose_digest(target_date, edition=edition, candidates=candidates)

        # Step 5: Promote corroborated benchmark evidence when explicitly enabled.
        if settings.BENCHMARK_VERIFICATION_ENABLED:
            verification.apply_cluster_evidence(digest)

        # Step 6: Trigger the drip. Item #1 goes out immediately; items #2-#6 follow on the
        # two-hour schedule. The manual publish_digest bulk path stays available for operators.
        if getattr(settings, "PUBLISHING_ENABLED", False):
            publish_next_item.delay(digest.id)
            res = {"status": "drip_started", "digest_id": digest.id}
        else:
            log.info("[KILL SWITCH ACTIVE] Suppressed drip publish for digest %s", digest.id)
            res = {"status": "suppressed", "digest_id": digest.id}

        # Step 7: Record pipeline freshness in Redis
        try:
            broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
            import redis

            r = redis.from_url(broker_url, socket_timeout=3.0)
            r.set(
                "news_radar:last_pipeline_run",
                json.dumps(
                    {
                        "completed_at": timezone.now().isoformat(),
                        "digest_date": str(target_date),
                        "edition": edition,
                        "status": res.get("status", "unknown"),
                        "items_sent": res.get("items_sent", 0),
                    }
                ),
            )
        except Exception as exc:
            log.warning("Failed to record pipeline freshness: %s", exc)

        return res

    except Exception as exc:
        log.error("Failed in compose_and_publish for %s (%s): %s", target_date, edition, exc)
        publish.send_admin_alert(f"Failed compose_and_publish for {target_date} ({edition}): {exc}")
        return {"error": str(exc), "digest_date": str(target_date), "edition": edition}


#: Delivery states that still owe the block an attempt.
#:
#: FAILED is NOT here, and that is the whole point. It is terminal in
#: publish.TERMINAL_DELIVERY_STATES, so listing it here made the two disagree: the drip
#: re-sent a permanently failed item at every tick forever, and because the item never left
#: the "unfinished" set, the block never completed and its roundup never fired.
UNFINISHED_DELIVERY_STATES = (DeliveryState.PENDING, DeliveryState.SENDING)


@shared_task(name="digest.publish_next_item")
def publish_next_item(digest_id: int | None = None) -> dict:
    """Find the oldest active digest, send its next unposted item, and refresh its status.

    Triggered:
    1. Immediately by compose_and_publish so item #1 lands when the block is ready (ADR-004 §6).
    2. Every two hours by Celery beat so items #2-#6 drip out across the window.
    3. Manually by an operator specifying digest_id to resume a specific block.

    When every item in the block is sent, triggers publish_roundup.delay(digest.id).
    """
    from . import publish
    from .models import Digest

    if digest_id is not None:
        digest = Digest.objects.filter(id=digest_id).first()
        if not digest:
            log.warning("publish_next_item called with unknown digest_id=%s", digest_id)
            return {"status": "idle", "reason": "unknown_digest"}
    else:
        # FIFO by composition time, so a morning block delayed by a slow LLM stage finishes
        # before the evening block starts. Ordering on `edition` would do the opposite:
        # TextChoices sort alphabetically, and "evening" < "morning".
        digest = (
            Digest.objects.filter(items__channel_delivery_state__in=UNFINISHED_DELIVERY_STATES)
            .distinct()
            .order_by("composed_at")
            .first()
        )
        if not digest:
            log.info("No active digest has pending items.")
            return {"status": "idle", "reason": "no_pending_items"}

    # Lowest position that still owes an attempt. FAILED and UNKNOWN are stepped past:
    # publish_digest_item refuses to retry UNKNOWN automatically, and a FAILED item has
    # already had its attempt, so retrying either here would stall the rest of the block.
    item = (
        digest.items.filter(channel_delivery_state__in=UNFINISHED_DELIVERY_STATES)
        .order_by("position")
        .first()
    )
    if not item:
        publish.refresh_digest_status(digest)
        return {"status": "idle", "reason": "digest_finished", "digest_id": digest.id}

    log.info(
        "Drip publishing item #%d (%s) for digest %s (%s)",
        item.position,
        item.article.title,
        digest.digest_date,
        digest.edition,
    )
    res = publish.publish_digest_item(item)
    new_status = publish.refresh_digest_status(digest)

    # Nothing left to attempt means the block is done, so the roundup goes out. Causal, not
    # scheduled: a clock-driven roundup would index a block whose last post was still queued.
    has_remaining = digest.items.filter(
        channel_delivery_state__in=UNFINISHED_DELIVERY_STATES
    ).exists()
    if not has_remaining:
        try:
            publish_roundup.delay(digest.id)
        except Exception as exc:
            # Never silent: a swallowed dispatch failure means the block's index is missing
            # from the channel with nothing in the log to say why.
            log.error("Could not dispatch the roundup for digest %s: %s", digest.id, exc)

    return {
        "status": res.get("status", "unknown"),
        "digest_id": digest.id,
        "item_id": item.id,
        "position": item.position,
        "digest_status": new_status,
    }


@shared_task(name="digest.publish_roundup")
def publish_roundup(digest_id: int) -> dict:
    from . import publish
    from .models import Digest

    digest = Digest.objects.filter(id=digest_id).first()
    if not digest:
        log.warning("publish_roundup task called with unknown digest_id=%s", digest_id)
        return {"status": "idle", "reason": "unknown_digest"}
    return publish.publish_roundup(digest)


@shared_task(name="digest.heartbeat")
def record_heartbeat(service_name: str) -> dict:
    """Record worker heartbeat in Redis with 120s TTL."""
    import redis

    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        r = redis.from_url(broker_url, socket_timeout=3.0)
        key = f"news_radar:heartbeat:{service_name}"
        now_iso = timezone.now().isoformat()
        r.set(key, now_iso, ex=120)
        return {"service": service_name, "heartbeat_at": now_iso}
    except Exception as exc:
        log.warning("Failed to record heartbeat for %s: %s", service_name, exc)
        return {"service": service_name, "error": str(exc)}


@shared_task(name="digest.dispatch_worker_heartbeats")
def dispatch_worker_heartbeats() -> dict:
    """Dispatch heartbeats to all worker queues and record beat heartbeat directly."""
    # Beat heartbeat
    record_heartbeat("beat")

    # Worker queue heartbeats
    record_heartbeat.apply_async(args=["worker-fetch"], queue="fetch")
    record_heartbeat.apply_async(args=["worker-llm"], queue="llm")
    record_heartbeat.apply_async(args=["worker-publish"], queue="publish")

    return {"dispatched": ["beat", "worker-fetch", "worker-llm", "worker-publish"]}
