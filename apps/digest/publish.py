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
import time

import httpx
from django.conf import settings
from django.utils import timezone

from . import ranking
from .models import Digest

log = logging.getLogger(__name__)


def _bot_url(method: str) -> str:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    return f"https://api.telegram.org/bot{token}/{method}"


def send_message(
    chat_id: str | int,
    text: str,
    reply_to_message_id: int | None = None,
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

    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

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


def find_group_forward_message_id(
    channel_message_id: int,
    client: httpx.Client | None = None,
    max_retries: int = 4,
    retry_delay: float = 1.5,
) -> int | None:
    """Find the auto-forwarded message in the linked discussion group via getUpdates.

    Enforces:
    - is_automatic_forward is True
    - forward_origin is channel matching TELEGRAM_CHANNEL_ID and channel_message_id
    - tracks and increments update_id offset.
    """
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        return None

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    group_id_str = str(getattr(settings, "TELEGRAM_GROUP_ID", ""))
    channel_id_str = str(getattr(settings, "TELEGRAM_CHANNEL_ID", ""))

    last_offset = None
    try:
        for _ in range(max_retries):
            params: dict = {"limit": 50, "timeout": 2}
            if last_offset is not None:
                params["offset"] = last_offset

            r = client.get(_bot_url("getUpdates"), params=params)
            if r.status_code == 200:
                data = r.json()
                updates = data.get("result", [])
                for update in reversed(updates):
                    up_id = update.get("update_id")
                    if up_id is not None:
                        last_offset = up_id + 1

                    msg = update.get("message") or update.get("channel_post")
                    if not msg:
                        continue

                    # Chat check
                    msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                    if group_id_str and msg_chat_id != group_id_str:
                        continue

                    # Automatic forward check
                    if msg.get("is_automatic_forward") is not True:
                        continue

                    # Check forward_origin (Bot API 7.0+)
                    origin = msg.get("forward_origin", {})
                    if origin:
                        origin_type = origin.get("type")
                        origin_chat_id = str(origin.get("chat", {}).get("id", ""))
                        origin_msg_id = origin.get("message_id")

                        if (
                            origin_type == "channel"
                            and (not channel_id_str or origin_chat_id == channel_id_str)
                            and origin_msg_id == channel_message_id
                        ):
                            return msg.get("message_id")

                    # Backwards compatibility check for older Bot API mocks
                    if msg.get("forward_from_message_id") == channel_message_id:
                        return msg.get("message_id")

            time.sleep(retry_delay)
        return None
    finally:
        if close_client:
            client.close()


def edit_message(
    chat_id: str | int,
    message_id: int,
    new_text: str,
    client: httpx.Client | None = None,
) -> dict:
    """Edit message text via editMessageText."""
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        log.info("[KILL SWITCH ACTIVE] editMessageText suppressed for msg %s", message_id)
        return {"suppressed": True}

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "HTML",
    }
    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    try:
        r = client.post(_bot_url("editMessageText"), json=payload)
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


def send_admin_alert(text: str, client: httpx.Client | None = None) -> None:
    """Send administrative alert to TELEGRAM_ADMIN_CHAT_ID."""
    admin_chat_id = getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "")
    if not admin_chat_id:
        log.warning("TELEGRAM_ADMIN_CHAT_ID not configured; alert dropped: %s", text)
        return

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not configured; alert dropped: %s", text)
        return

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
    except Exception as exc:
        log.error("Exception sending admin alert: %s", exc)
    finally:
        if close_client:
            client.close()


def publish_digest(
    digest: Digest,
    client: httpx.Client | None = None,
) -> dict:
    """Publish each digest item as its own channel post with its own group appendix.

    Per-item publishing (T1.14):
    - Each DigestItem gets its own sendMessage → its own channel_message_id.
    - Each post's auto-forward in the linked group is found, and a per-item appendix is
      sent as a reply → its own group_message_id.
    - Partial failure: if any item fails, the digest is marked FAILED and the admin alert
      names the failed items. Already-sent posts are NOT rolled back.
    - A small delay between sends respects Telegram's rate limit (~20 msg/min to a channel).
    """
    # Kill switch: when PUBLISHING_ENABLED is False, compose and store but send nothing.
    # Leave digest status as COMPOSED, do not set published_at, write no message IDs.
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        log.info(
            "[KILL SWITCH ACTIVE] Suppressed for digest %s. Status stays 'composed'.",
            digest.digest_date,
        )
        return {
            "suppressed": True,
            "digest_id": digest.id,
            "digest_date": str(digest.digest_date),
            "status": digest.status,
            "channel_message_id": None,
            "group_message_id": None,
            "items_count": digest.items.count(),
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
    failed_items: list[str] = []

    send_delay = getattr(settings, "TELEGRAM_SEND_DELAY", 3.0)
    try:
        for idx, item in enumerate(items):
            # Rate-limit: configurable delay between sends (20 msg/min budget shared with appendix)
            if idx > 0 and send_delay > 0:
                time.sleep(send_delay)

            # --- Channel post ---
            try:
                post_html = ranking.render_item_post(item)
            except ValueError as exc:
                log.error("Render failed for item #%s: %s", item.position, exc)
                failed_items.append(f"#{item.position} (render: {exc})")
                continue

            res_post = send_message(chat_id=channel_id, text=post_html, client=client)
            ch_msg_id = res_post.get("result", {}).get("message_id")

            if not ch_msg_id:
                log.error("sendMessage failed for item #%s: %s", item.position, res_post)
                failed_items.append(f"#{item.position} (sendMessage failed)")
                continue

            item.channel_message_id = ch_msg_id
            item.save(update_fields=["channel_message_id"])
            sent_count += 1

            # --- Group appendix ---
            if group_id:
                if send_delay > 0:
                    time.sleep(min(send_delay / 2, 1.5))
                fwd_id = find_group_forward_message_id(ch_msg_id, client=client)
                if fwd_id:
                    try:
                        appendix_html = ranking.render_item_appendix(item)
                    except ValueError as exc:
                        log.error("Appendix render failed for item #%s: %s", item.position, exc)
                        failed_items.append(f"#{item.position} (appendix render: {exc})")
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
                    failed_items.append(
                        f"#{item.position} (forward not found for msg {ch_msg_id})"
                    )

    finally:
        if close_client:
            client.close()

    # --- Status decision ---
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
        log.info(
            "Digest %s published: %s items posted",
            digest.digest_date,
            sent_count,
        )

    return {
        "digest_id": digest.id,
        "digest_date": str(digest.digest_date),
        "items_sent": sent_count,
        "items_failed": len(failed_items),
        "failed_items": failed_items,
        "status": digest.status,
    }

