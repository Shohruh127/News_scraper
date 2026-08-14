"""Telegram publishing module using direct Bot API via httpx.

Rules:
1. No aiogram in M1 — Celery tasks are synchronous, sendMessage is plain HTTP POST.
2. Kill switch: when settings.PUBLISHING_ENABLED is False, compose and store but send nothing.
3. Technical appendix is posted as a reply to the auto-forwarded message in the linked group.
4. Degraded-source alerts are delivered to TELEGRAM_ADMIN_CHAT_ID.
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
            "[KILL SWITCH ACTIVE] Telegram sendMessage suppressed for chat %s. "
            "Text preview: %s",
            chat_id,
            text[:100],
        )
        return {"ok": True, "result": {"message_id": 100001}}

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
    """Find the auto-forwarded message in the linked discussion group via getUpdates."""
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        return 100002

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    group_id_str = str(getattr(settings, "TELEGRAM_GROUP_ID", ""))

    try:
        for _ in range(max_retries):
            r = client.get(_bot_url("getUpdates"), params={"limit": 50, "timeout": 2})
            if r.status_code == 200:
                data = r.json()
                for update in reversed(data.get("result", [])):
                    msg = update.get("message") or update.get("channel_post")
                    if not msg:
                        continue
                    chat_id_match = (
                        not group_id_str or str(msg.get("chat", {}).get("id")) == group_id_str
                    )
                    is_forward = (
                        msg.get("is_automatic_forward") is True
                        or msg.get("forward_from_message_id") == channel_message_id
                    )
                    if is_forward and chat_id_match:
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
        return {"ok": True, "result": True}

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
        return {"ok": True, "result": True}

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

    # Admin alerts always send if bot token is present
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
    """Publish digest post to channel and technical appendix to linked discussion group."""
    channel_id = getattr(settings, "TELEGRAM_CHANNEL_ID", "")
    if not channel_id:
        raise ValueError("TELEGRAM_CHANNEL_ID is not configured in settings")

    # Step 1: Render and send main channel post
    channel_post_html = ranking.render_channel_post(digest)
    res_post = send_message(chat_id=channel_id, text=channel_post_html, client=client)
    channel_msg_id = res_post.get("result", {}).get("message_id")

    # Step 2: Store channel_message_id on all items
    if channel_msg_id:
        digest.items.update(channel_message_id=channel_msg_id)

    # Step 3: Find auto-forwarded message in linked group and reply with technical appendix
    group_msg_id = None
    group_id = getattr(settings, "TELEGRAM_GROUP_ID", "")
    if group_id and channel_msg_id:
        fwd_id = find_group_forward_message_id(channel_msg_id, client=client)
        if fwd_id:
            group_comment_html = ranking.render_group_comment(digest)
            res_comment = send_message(
                chat_id=group_id,
                text=group_comment_html,
                reply_to_message_id=fwd_id,
                client=client,
            )
            group_msg_id = res_comment.get("result", {}).get("message_id")
            if group_msg_id:
                digest.items.update(group_message_id=group_msg_id)
        else:
            log.warning(
                "Could not locate auto-forwarded message for channel msg %s",
                channel_msg_id,
            )

    # Step 4: Mark digest as published
    digest.status = Digest.Status.PUBLISHED
    digest.published_at = timezone.now()
    digest.save(update_fields=["status", "published_at"])

    log.info(
        "Digest %s published: channel_msg_id=%s, group_msg_id=%s",
        digest.digest_date,
        channel_msg_id,
        group_msg_id,
    )
    return {
        "digest_id": digest.id,
        "digest_date": str(digest.digest_date),
        "channel_message_id": channel_msg_id,
        "group_message_id": group_msg_id,
        "items_published": digest.items.count(),
    }
