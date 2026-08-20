"""Telegram publishing module using direct Bot API via httpx.

Rules (T1.7, T1.9):
1. No aiogram in M1 — Celery tasks are synchronous, sendMessage is plain HTTP POST.
2. Kill switch: when settings.PUBLISHING_ENABLED is False, compose and store but send nothing.
   Leaves digest status as COMPOSED, writes no message IDs, sets no published_at.
3. Technical appendix is posted as a reply to the auto-forwarded message in the linked group.
   Missing forward marks digest as FAILED, alerts admin, and stops.
4. Forward matching requires origin channel matching TELEGRAM_CHANNEL_ID and message ID.
5. Degraded-source alerts are delivered to TELEGRAM_ADMIN_CHAT_ID.
"""

import logging
import os
import platform
import time
from typing import Any

import httpx
import redis
import trafilatura
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import media, ranking
from .models import DeliveryState, Digest, DigestItem
from .telegram_updates import wait_for_group_forward

log = logging.getLogger(__name__)


def _bot_url(method: str) -> str:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    return f"https://api.telegram.org/bot{token}/{method}"


def send_message(
    chat_id: str | int,
    text: str,
    reply_to_message_id: int | None = None,
    reply_markup: dict | None = None,
    disable_preview: bool = False,
    client: httpx.Client | None = None,
) -> dict:
    """Send HTML message to Telegram. Respects PUBLISHING_ENABLED kill switch."""
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        log.info(
            "[KILL SWITCH ACTIVE] Telegram sendMessage suppressed for chat %s. Preview: %s",
            chat_id,
            text[:100],
        )
        return {"suppressed": True}

    if disable_preview:
        preview_options = {"is_disabled": True}
    elif getattr(settings, "TELEGRAM_LINK_PREVIEW", False):
        preview_options = {
            "is_disabled": False,
            "prefer_small_media": True,
            "show_above_text": False,
        }
    else:
        preview_options = {"is_disabled": True}

    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": preview_options,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    try:
        r = client.post(_bot_url("sendMessage"), json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if close_client:
            client.close()


def send_photo(
    chat_id: str | int,
    photo_url: str,
    caption: str,
    reply_to_message_id: int | None = None,
    reply_markup: dict | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Send photo with caption to Telegram using photo URL.

    Respects PUBLISHING_ENABLED kill switch.
    """
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        log.info(
            "[KILL SWITCH ACTIVE] Telegram sendPhoto suppressed for chat %s. Preview: %s",
            chat_id,
            caption[:100],
        )
        return {"suppressed": True}

    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = str(reply_to_message_id)
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    try:
        r = client.post(_bot_url("sendPhoto"), json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if close_client:
            client.close()


def find_group_forward_message_id(
    channel_message_id: int,
    client=None,
    max_retries: int = 4,
    retry_delay: float = 1.5,
) -> int | None:
    """Find the bot's Redis handoff for one channel post."""
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        return None
    return wait_for_group_forward(
        getattr(settings, "TELEGRAM_CHANNEL_ID", ""),
        channel_message_id,
        client=client,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )


def edit_message(
    chat_id: str | int,
    message_id: int,
    new_text: str,
    sent_as_photo: bool = False,
    client: httpx.Client | None = None,
) -> dict:
    """Edit message text via editMessageCaption (if photo) or editMessageText."""
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        method_name = "editMessageCaption" if sent_as_photo else "editMessageText"
        log.info("[KILL SWITCH ACTIVE] %s suppressed for msg %s", method_name, message_id)
        return {"suppressed": True}

    method = "editMessageCaption" if sent_as_photo else "editMessageText"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "parse_mode": "HTML",
    }
    if sent_as_photo:
        payload["caption"] = new_text
    else:
        payload["text"] = new_text

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    try:
        r = client.post(_bot_url(method), json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if close_client:
            client.close()


def delete_message(
    chat_id: str | int,
    message_id: int,
    client: httpx.Client | None = None,
) -> dict:
    """Delete message via deleteMessage."""
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        log.info("[KILL SWITCH ACTIVE] deleteMessage suppressed for msg %s", message_id)
        return {"suppressed": True}

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
    }
    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    try:
        r = client.post(_bot_url("deleteMessage"), json=payload)
        r.raise_for_status()
        return r.json()
    finally:
        if close_client:
            client.close()


def send_admin_alert(text: str, client: httpx.Client | None = None) -> bool:
    """Send an administrative alert. Returns True only if Telegram accepted it."""
    admin_chat_id = getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "")
    if not admin_chat_id:
        log.warning("TELEGRAM_ADMIN_CHAT_ID not configured; alert dropped: %s", text)
        return False

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not configured; alert dropped: %s", text)
        return False

    close_client = False
    if client is None:
        client = httpx.Client(timeout=15)
        close_client = True

    try:
        r = client.post(
            _bot_url("sendMessage"),
            json={
                "chat_id": admin_chat_id,
                "text": f"🚨 <b>News Radar Alert</b>\n\n{text}",
                "parse_mode": "HTML",
            },
        )
        if r.status_code != 200:
            log.warning("Failed to send admin alert: %s %s", r.status_code, r.text)
            return False
        return True
    except Exception as exc:
        log.error("Exception sending admin alert: %s", exc)
        return False
    finally:
        if close_client:
            client.close()


def publish_digest(
    digest: Digest,
    client: httpx.Client | None = None,
    *,
    republish: bool = False,
) -> dict:
    """Publish each digest item as its own channel post with its own group appendix.

    Per-item publishing (T1.14):
    - Each DigestItem gets its own sendMessage → its own channel_message_id.
    - Each post's auto-forward in the linked group is found, and a per-item appendix is
      sent as a reply → its own group_message_id.
    - Partial failure: if any item fails, the digest is marked FAILED and the admin alert
      names the failed items. Already-sent posts are NOT rolled back.
    - A small delay between sends respects Telegram's rate limit (~20 msg/min to a channel).
    - Idempotent by default: an item that already carries a channel_message_id is skipped, so a
      second call resumes a partial run instead of posting the digest again. Measured 2026-08-19,
      before this guard existed: 61 of the 82 live channel messages had no database record.
      Pass republish=True only to deliberately re-send posts that were deleted by hand.
    """
    # Kill switch: when PUBLISHING_ENABLED is False, compose and store but send nothing.
    # Leave digest status as COMPOSED, do not set published_at, write no message IDs.
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        log.info(
            "[KILL SWITCH ACTIVE] Suppressed for digest %s. Status stays 'composed'.",
            digest.digest_date,
        )
        return {
            "digest_id": digest.id,
            "digest_date": str(digest.digest_date),
            "status": digest.status,
            "items_sent": 0,
            "items_skipped": 0,
            "items_failed": 0,
            "failed_items": [],
            "appendix_failures": [],
            "suppressed": True,
        }

    # Distributed lock to prevent concurrent publishes of the same digest
    lock_client = None
    lock_acquired = True
    try:
        lock_client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
        holder = f"{platform.node()}:{os.getpid()}"
        lock_acquired = bool(
            lock_client.set(
                f"news_radar:publish_lock:{digest.id}",
                holder,
                nx=True,
                ex=300,
            )
        )
    except Exception as exc:
        log.warning("Could not check Redis publish lock: %s", exc)

    if not lock_acquired:
        try:
            current_holder = lock_client.get(f"news_radar:publish_lock:{digest.id}")
            holder_str = current_holder.decode() if current_holder else "unknown"
        except Exception:
            holder_str = "unknown"
        log.warning(
            "Publish for digest %s is already running (lock held by %s). Skipping duplicate run.",
            digest.id,
            holder_str,
        )
        return {
            "digest_id": digest.id,
            "digest_date": str(digest.digest_date),
            "status": digest.status,
            "items_sent": 0,
            "items_skipped": 0,
            "items_failed": 0,
            "failed_items": [],
            "appendix_failures": [],
            "suppressed": False,
            "locked": True,
        }

    channel_id = getattr(settings, "TELEGRAM_CHANNEL_ID", "")
    if not channel_id:
        raise ValueError("TELEGRAM_CHANNEL_ID is not configured in settings")

    group_id = getattr(settings, "TELEGRAM_GROUP_ID", "")
    items = list(
        digest.items.select_related("article", "article__source")
        .prefetch_related(
            "secondary_articles",
            "secondary_articles__source",
            "article__analyses",
        )
        .order_by("position")
    )

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    sent_count = 0
    skipped_count = 0
    failed_items: list[str] = []
    appendix_failures: list[str] = []

    send_delay = getattr(settings, "TELEGRAM_SEND_DELAY", 3.0)
    try:
        for idx, item in enumerate(items):
            try:
                item.refresh_from_db(
                    fields=[
                        "channel_message_id",
                        "group_message_id",
                        "channel_delivery_state",
                        "channel_delivery_error",
                    ]
                )
            except Exception:
                pass

            if item.channel_delivery_state == DeliveryState.SENT or item.channel_message_id:
                if not republish:
                    skipped_count += 1
                    log.info(
                        "Item #%s already sent (message %s); skipping",
                        item.position,
                        item.channel_message_id,
                    )
                    continue

            if item.channel_delivery_state == DeliveryState.UNKNOWN and not republish:
                skipped_count += 1
                log.warning(
                    "Item #%s is in 'unknown' delivery state. Skipping automatic retry.",
                    item.position,
                )
                failed_items.append(f"#{item.position} (ambiguous delivery state: unknown)")
                continue

            if item.channel_delivery_state == DeliveryState.SENDING:
                log.warning(
                    "Item #%s found in 'sending' state from previous attempt. "
                    "Promoting to 'unknown' to avoid duplicate.",
                    item.position,
                )
                with transaction.atomic():
                    DigestItem.objects.filter(
                        id=item.id, channel_delivery_state=DeliveryState.SENDING
                    ).update(
                        channel_delivery_state=DeliveryState.UNKNOWN,
                        channel_delivery_error="Stale sending state promoted to unknown",
                    )
                item.refresh_from_db()
                if not republish:
                    skipped_count += 1
                    failed_items.append(f"#{item.position} (stale sending promoted to unknown)")
                    continue

            # Rate-limit: configurable delay between sends (20 msg/min budget shared with appendix)
            if idx > 0 and send_delay > 0:
                time.sleep(send_delay)

            # --- Channel post rendering ---
            try:
                post_html = ranking.render_item_post(item)
            except ValueError as exc:
                log.error("Render failed for item #%s: %s", item.position, exc)
                with transaction.atomic():
                    DigestItem.objects.filter(id=item.id).update(
                        channel_delivery_state=DeliveryState.FAILED,
                        channel_delivery_error=f"Render error: {exc}"[:512],
                    )
                failed_items.append(f"#{item.position} (render: {exc})")
                continue

            # Lock row and transition pending -> sending
            with transaction.atomic():
                locked = DigestItem.objects.select_for_update().filter(id=item.id).first()
                if not locked:
                    continue
                if locked.channel_delivery_state == DeliveryState.SENT and not republish:
                    skipped_count += 1
                    continue
                if locked.channel_delivery_state == DeliveryState.UNKNOWN and not republish:
                    skipped_count += 1
                    continue
                locked.channel_delivery_state = DeliveryState.SENDING
                locked.channel_delivery_attempted_at = timezone.now()
                locked.save(
                    update_fields=["channel_delivery_state", "channel_delivery_attempted_at"]
                )
            item.refresh_from_db()

            v2_enabled = getattr(settings, "POST_FORMAT_V2_ENABLED", False)
            sent_as_photo = False
            res_post = None

            try:
                if v2_enabled:
                    image_url = item.article.meta.get("image_url")
                    if not image_url and item.article.canonical_url:
                        try:
                            downloaded = trafilatura.fetch_url(item.article.canonical_url)
                            if downloaded:
                                fetched_img = media.extract_image_url_from_html(
                                    downloaded, base_url=item.article.canonical_url
                                )
                                if fetched_img:
                                    image_url = fetched_img
                                    meta = dict(item.article.meta or {})
                                    meta["image_url"] = fetched_img
                                    item.article.meta = meta
                                    item.article.save(update_fields=["meta"])
                        except Exception as exc:
                            log.debug(
                                "On-demand image fetch failed for item #%s: %s",
                                item.position,
                                exc,
                            )

                    valid_image_url = media.validate_image_url(image_url) if image_url else None
                    if image_url and not valid_image_url:
                        log.info(
                            "Image URL rejected by policy for item #%s (host: %s)",
                            item.position,
                            media.get_safe_image_log_host(image_url),
                        )

                    if valid_image_url:
                        try:
                            res_post = send_photo(
                                chat_id=channel_id,
                                photo_url=valid_image_url,
                                caption=post_html,
                                client=client,
                            )
                            sent_as_photo = True
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code == 400:
                                log.warning(
                                    "Telegram rejected photo for item #%s (400, host: %s). "
                                    "Falling back to text.",
                                    item.position,
                                    media.get_safe_image_log_host(valid_image_url),
                                )
                                res_post = send_message(
                                    chat_id=channel_id,
                                    text=post_html,
                                    disable_preview=True,
                                    client=client,
                                )
                                sent_as_photo = False
                            else:
                                raise
                    else:
                        res_post = send_message(
                            chat_id=channel_id,
                            text=post_html,
                            disable_preview=True,
                            client=client,
                        )
                        sent_as_photo = False
                else:
                    res_post = send_message(
                        chat_id=channel_id,
                        text=post_html,
                        client=client,
                    )
                    sent_as_photo = False

            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                log.error(
                    "Network/Timeout error during channel send for item #%s: %s. Setting UNKNOWN.",
                    item.position,
                    exc,
                )
                with transaction.atomic():
                    DigestItem.objects.filter(id=item.id).update(
                        channel_delivery_state=DeliveryState.UNKNOWN,
                        channel_delivery_error=f"Timeout/Network error: {exc}"[:512],
                    )
                send_admin_alert(
                    f"🚨 <b>Publishing Error</b>: Network timeout for item #{item.position}. "
                    "State set to UNKNOWN to prevent duplicates.",
                    client=client,
                )
                failed_items.append(f"#{item.position} (timeout/network error: {exc})")
                continue

            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code >= 500:
                    log.error(
                        "Telegram 5xx error (%s) for item #%s. Setting state UNKNOWN.",
                        status_code,
                        item.position,
                    )
                    with transaction.atomic():
                        DigestItem.objects.filter(id=item.id).update(
                            channel_delivery_state=DeliveryState.UNKNOWN,
                            channel_delivery_error=f"Telegram 5xx ({status_code}): {exc}"[:512],
                        )
                    send_admin_alert(
                        f"🚨 <b>Telegram 5xx Error</b> ({status_code}) for item #{item.position}. "
                        "State set to UNKNOWN.",
                        client=client,
                    )
                    failed_items.append(f"#{item.position} (telegram 5xx: {exc})")
                    continue
                else:
                    log.error(
                        "HTTP error (%s) during channel send for item #%s: %s",
                        status_code,
                        item.position,
                        exc,
                    )
                    with transaction.atomic():
                        DigestItem.objects.filter(id=item.id).update(
                            channel_delivery_state=DeliveryState.FAILED,
                            channel_delivery_error=f"HTTP {status_code}: {exc}"[:512],
                        )
                    failed_items.append(f"#{item.position} (HTTP {status_code}: {exc})")
                    continue

            except Exception as exc:
                log.error(
                    "Unexpected error during channel send for item #%s: %s",
                    item.position,
                    exc,
                )
                with transaction.atomic():
                    DigestItem.objects.filter(id=item.id).update(
                        channel_delivery_state=DeliveryState.UNKNOWN,
                        channel_delivery_error=f"Unexpected error: {exc}"[:512],
                    )
                failed_items.append(f"#{item.position} (unexpected error: {exc})")
                continue

            ch_msg_id = res_post.get("result", {}).get("message_id") if res_post else None

            if not ch_msg_id and not (res_post and res_post.get("suppressed")):
                log.error("Channel post failed for item #%s: %s", item.position, res_post)
                with transaction.atomic():
                    DigestItem.objects.filter(id=item.id).update(
                        channel_delivery_state=DeliveryState.FAILED,
                        channel_delivery_error=f"No message ID returned: {res_post}"[:512],
                    )
                failed_items.append(f"#{item.position} (publish failed: no message_id)")
                continue

            if ch_msg_id:
                with transaction.atomic():
                    DigestItem.objects.filter(id=item.id).update(
                        channel_message_id=ch_msg_id,
                        sent_as_photo=sent_as_photo,
                        channel_delivery_state=DeliveryState.SENT,
                        channel_delivery_error="",
                    )
                item.refresh_from_db()
                sent_count += 1
            elif res_post and res_post.get("suppressed"):
                with transaction.atomic():
                    DigestItem.objects.filter(id=item.id).update(
                        channel_delivery_state=DeliveryState.PENDING,
                        channel_delivery_error="Suppressed by kill switch",
                    )
                item.refresh_from_db()

            # --- Group appendix ---
            if group_id:
                if send_delay > 0:
                    time.sleep(min(send_delay / 2, 1.5))
                fwd_id = find_group_forward_message_id(ch_msg_id)
                if fwd_id:
                    try:
                        appendix_html = ranking.render_item_appendix(item)
                    except ValueError as exc:
                        log.error("Appendix render failed for item #%s: %s", item.position, exc)
                        appendix_failures.append(f"#{item.position} (appendix render: {exc})")
                        continue

                    res_comment = send_message(
                        chat_id=group_id,
                        text=appendix_html,
                        reply_to_message_id=fwd_id,
                        client=client,
                    )
                    grp_msg_id = res_comment.get("result", {}).get("message_id")
                    if grp_msg_id:
                        item.group_message_id = grp_msg_id
                        item.save(update_fields=["group_message_id"])
                else:
                    log.warning(
                        "Auto-forward not found for item #%s (channel_msg %s)",
                        item.position,
                        ch_msg_id,
                    )
                    appendix_failures.append(
                        f"#{item.position} (forward not found for msg {ch_msg_id})"
                    )

    finally:
        if close_client:
            client.close()
        if lock_client and lock_acquired:
            try:
                lock_client.delete(f"news_radar:publish_lock:{digest.id}")
            except Exception as exc:
                log.debug("Error releasing publish lock: %s", exc)

    # --- Status decision ---
    # Only a channel post that did not go out fails a digest. An appendix that did not land
    # leaves a degraded post, and marking the digest FAILED for it invites a re-run that
    # posts everything a second time.
    if failed_items:
        digest.status = Digest.Status.FAILED
        digest.save(update_fields=["status"])
        alert_msg = (
            f"Digest {digest.digest_date}: {sent_count}/{len(items)} items posted. "
            f"Failed: {', '.join(failed_items)}"
        )
        send_admin_alert(alert_msg)
        log.error(alert_msg)
    else:
        digest.status = Digest.Status.PUBLISHED
        digest.published_at = timezone.now()
        digest.save(update_fields=["status", "published_at"])
        log.info("Digest %s published: %s items posted", digest.digest_date, sent_count)

    if appendix_failures:
        appendix_msg = (
            f"Digest {digest.digest_date}: every post was delivered, but "
            f"{len(appendix_failures)} appendix message(s) were not: "
            f"{', '.join(appendix_failures)}. Check that the bot service is running."
        )
        send_admin_alert(appendix_msg)
        log.warning(appendix_msg)

    return {
        "digest_id": digest.id,
        "digest_date": str(digest.digest_date),
        "items_sent": sent_count,
        "items_skipped": skipped_count,
        "items_failed": len(failed_items),
        "failed_items": failed_items,
        "appendix_failures": appendix_failures,
        "status": digest.status,
        "suppressed": False,
    }
