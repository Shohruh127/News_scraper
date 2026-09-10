"""Telegram publishing module using direct Bot API via httpx.

Rules (T1.7, T1.9):
1. No aiogram in M1 — Celery tasks are synchronous, sendMessage is plain HTTP POST.
2. Kill switch: when settings.PUBLISHING_ENABLED is False, compose and store but send nothing.
   Leaves digest status as COMPOSED, writes no message IDs, sets no published_at.
3. Degraded-source alerts are delivered to TELEGRAM_ADMIN_CHAT_ID.
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

from . import media, post_format, ranking
from .models import Analysis, DeliveryState, Digest, DigestItem

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

    # The channel owner requires photos and word links, never preview/Instant View
    # cards. `disable_preview` is kept because callers pass it, but it can only ever
    # disable: neither an argument nor TELEGRAM_LINK_PREVIEW can turn a preview back on.
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

    if post_format.telegram_length(caption) > 1024:
        raise ValueError("Photo caption exceeds 1024 characters")

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


def keep_publishable(candidates):
    """Drop a story that has no photo before the editorial stage pays for it.

    Added 2026-09-10. A post is a photo post and an item without one fails at publish, so
    the photo is resolved first and the page fetch is spent instead of the editorial call:
    on 2026-09-09 DeepMind's AlphaGenome post was drafted and re-expressed on Gemini, then
    failed at 18:10 for want of a photo, while Google's copy of the same story sat one slot
    below with a picture. A story keeps its slot if any copy has a photo - the copy that
    does becomes the primary and the rest stay attached. `resolve_photo_url` writes what it
    finds back to `article.meta`, so publish does not fetch the page again.

    Input and output are `select_digest_candidates` tuples:
    (primary, analysis, score, [secondary, ...]).
    """
    kept = []
    for article, analysis, score, secondary in candidates:
        members = [article, *secondary]
        with_photo = next((m for m in members if _photo_or_none(m)), None)
        if with_photo is None:
            log.warning(
                "Dropping candidate %s before the editorial: no usable photo (%s)",
                article.id,
                article.title[:60],
            )
            continue
        if with_photo.id != article.id:
            log.info(
                "Candidate %s carries the photo for the story led by %s; it becomes the primary",
                with_photo.id,
                article.id,
            )
            analysis = (
                Analysis.objects.filter(article=with_photo, stage=Analysis.Stage.CLASSIFICATION)
                .order_by("-created_at")
                .first()
                or analysis
            )
            secondary = [m for m in members if m.id != with_photo.id]
            article = with_photo
        kept.append((article, analysis, score, secondary))
    return kept


def _photo_or_none(article) -> str | None:
    try:
        return resolve_photo_url(article)
    except Exception as exc:  # noqa: BLE001 - one page's failure must not cost the edition
        log.warning("Photo lookup raised for article %s: %s", article.id, type(exc).__name__)
        return None


def resolve_photo_url(article) -> str | None:
    """Use the article's photo, never its link-preview card as a substitute.

    Writes a freshly fetched image back to `article.meta`, so a republish, an
    `edit_message` or a later retry reuses it instead of downloading the page again.

    Says which of the three outcomes happened. Returning a bare None for all of them
    left the operator holding "Photo required" with no way to tell a page that has no
    og:image from one whose image the media policy rejected.
    """
    raw = (article.meta or {}).get("image_url")
    image = media.validate_image_url(raw)
    if image:
        return image
    if raw:
        log.warning(
            "Stored image URL rejected by policy for article %s (host: %s)",
            article.id,
            media.get_safe_image_log_host(raw),
        )
    if not article.canonical_url:
        return None
    try:
        downloaded = trafilatura.fetch_url(article.canonical_url)
    except Exception as exc:
        log.warning(
            "Article photo lookup failed for article %s: %s", article.id, type(exc).__name__
        )
        return None
    if not downloaded:
        log.info("Article photo lookup fetched nothing for article %s", article.id)
        return None
    found = media.extract_image_url_from_html(downloaded, base_url=article.canonical_url)
    if not found:
        log.info("Article %s has no usable og:image", article.id)
        return None
    meta = dict(article.meta or {})
    meta["image_url"] = found
    article.meta = meta
    article.save(update_fields=["meta"])
    return found


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
        if post_format.telegram_length(new_text) > 1024:
            raise ValueError("Photo caption exceeds 1024 characters")
        payload["caption"] = new_text
    else:
        payload["text"] = new_text
        payload["link_preview_options"] = {"is_disabled": True}

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
    finally:
        if close_client:
            client.close()


def publish_digest_item(
    item: DigestItem,
    client: httpx.Client | None = None,
    *,
    republish: bool = False,
) -> dict:
    """Publish a single DigestItem as a channel post.

    Respects PUBLISHING_ENABLED kill switch.
    Handles per-item locking, format rendering, image/photo extraction and fallback,
    Telegram rate limits/timeouts, error handling, and state updating.
    """
    if not getattr(settings, "PUBLISHING_ENABLED", False):
        log.info(
            "[KILL SWITCH ACTIVE] Suppressed for item #%s.",
            item.position,
        )
        return {
            "success": False,
            "status": "suppressed",
            "item_id": item.id,
            "position": item.position,
            "channel_message_id": None,
            "sent_as_photo": False,
            "error": "Suppressed by kill switch (PUBLISHING_ENABLED is False)",
            "suppressed": True,
        }

    channel_id = getattr(settings, "TELEGRAM_CHANNEL_ID", "")
    if not channel_id:
        raise ValueError("TELEGRAM_CHANNEL_ID is not configured in settings")

    try:
        item.refresh_from_db(
            fields=[
                "channel_message_id",
                "channel_delivery_state",
                "channel_delivery_error",
            ]
        )
    except Exception:
        pass

    if item.channel_delivery_state == DeliveryState.SENT or item.channel_message_id:
        if not republish:
            log.info(
                "Item #%s already sent (message %s); skipping",
                item.position,
                item.channel_message_id,
            )
            return {
                "success": True,
                "status": "skipped",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": item.channel_message_id,
                "sent_as_photo": item.sent_as_photo,
                "error": None,
                "suppressed": False,
            }

    if item.channel_delivery_state == DeliveryState.UNKNOWN and not republish:
        log.warning(
            "Item #%s is in 'unknown' delivery state. Skipping automatic retry.",
            item.position,
        )
        return {
            "success": False,
            "status": "skipped",
            "item_id": item.id,
            "position": item.position,
            "channel_message_id": None,
            "sent_as_photo": False,
            "error": "ambiguous delivery state: unknown",
            "suppressed": False,
        }

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
            return {
                "success": False,
                "status": "skipped",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": "stale sending promoted to unknown",
                "suppressed": False,
            }

    # --- Channel post rendering ---
    editorial = (
        item.article.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ)
        .order_by("-created_at")
        .first()
    )
    photo_only = bool(
        editorial and editorial.payload.get("post_style") == post_format.PLAIN_PHOTO_STYLE
    )
    try:
        post_html = ranking.render_item_post(item)
        if photo_only and post_format.telegram_length(post_html) > 1024:
            raise ValueError("Photo caption exceeds 1024 characters")
    except ValueError as exc:
        log.error("Render failed for item #%s: %s", item.position, exc)
        with transaction.atomic():
            DigestItem.objects.filter(id=item.id).update(
                channel_delivery_state=DeliveryState.FAILED,
                channel_delivery_error=f"Render error: {exc}"[:512],
            )
        item.refresh_from_db()
        return {
            "success": False,
            "status": "failed",
            "item_id": item.id,
            "position": item.position,
            "channel_message_id": None,
            "sent_as_photo": False,
            "error": f"render: {exc}",
            "suppressed": False,
        }

    photo_url = None
    if photo_only:
        photo_url = resolve_photo_url(item.article)
        if not photo_url:
            # FAILED, not PENDING. The owner's rule is photo-or-nothing, so this item
            # cannot go out — but `publish_next_item` selects the lowest position still
            # PENDING or SENDING, and `resolve_photo_url` is deterministic for a given
            # article, so leaving it pending re-picks the same item at every two-hour
            # tick: items #2-#6 never publish, `publish_roundup` never fires because
            # `has_remaining` stays true, and every later edition queues behind it,
            # since digests are selected FIFO by `composed_at`. Measured 2026-09-07:
            # five consecutive ticks all returned this item, and nothing was sent.
            # FAILED is the state the drip is documented to step past, and it reaches
            # the operator through the admin alert; the item can be republished once a
            # photo exists. Silence in the channel is the worse failure.
            log.error("No photo for item #%s; the item cannot be sent.", item.position)
            with transaction.atomic():
                DigestItem.objects.filter(id=item.id).update(
                    channel_delivery_state=DeliveryState.FAILED,
                    channel_delivery_error="Photo required: no usable article image"[:512],
                )
            item.refresh_from_db()
            return {
                "success": False,
                "status": "failed",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": "Photo required: no usable article image",
                "suppressed": False,
            }

    # Lock row and transition pending -> sending
    with transaction.atomic():
        locked = DigestItem.objects.select_for_update().filter(id=item.id).first()
        if not locked:
            return {
                "success": False,
                "status": "failed",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": "Item not found during lock",
                "suppressed": False,
            }
        if locked.channel_delivery_state == DeliveryState.SENT and not republish:
            return {
                "success": True,
                "status": "skipped",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": locked.channel_message_id,
                "sent_as_photo": locked.sent_as_photo,
                "error": None,
                "suppressed": False,
            }
        if locked.channel_delivery_state == DeliveryState.UNKNOWN and not republish:
            return {
                "success": False,
                "status": "skipped",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": "ambiguous delivery state: unknown",
                "suppressed": False,
            }
        locked.channel_delivery_state = DeliveryState.SENDING
        locked.channel_delivery_attempted_at = timezone.now()
        locked.save(update_fields=["channel_delivery_state", "channel_delivery_attempted_at"])
    item.refresh_from_db()

    v2_enabled = getattr(settings, "POST_FORMAT_V2_ENABLED", False)
    sent_as_photo = False
    res_post = None

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    try:
        try:
            if photo_only:
                res_post = send_photo(
                    chat_id=channel_id, photo_url=photo_url, caption=post_html, client=client
                )
                sent_as_photo = True
            elif post_format.telegram_length(post_html) > 1024:
                # A legacy post too long for a caption goes out whole as one text
                # message. It used to pass disable_preview=False expecting an unfurled
                # card, which send_message ignores -- so the argument is dropped rather
                # than left implying a preview this path never gets.
                res_post = send_message(chat_id=channel_id, text=post_html, client=client)
            elif v2_enabled:
                # One article-photo policy, one implementation. This branch used to
                # inline the same meta-then-fetch-then-cache sequence resolve_photo_url
                # now owns; two copies meant the plain-photo path silently lost the
                # meta write-back and the policy-rejection log.
                image_url = resolve_photo_url(item.article)

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
            item.refresh_from_db()
            return {
                "success": False,
                "status": "failed",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": f"timeout/network error: {exc}",
                "suppressed": False,
            }

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
                item.refresh_from_db()
                return {
                    "success": False,
                    "status": "failed",
                    "item_id": item.id,
                    "position": item.position,
                    "channel_message_id": None,
                    "sent_as_photo": False,
                    "error": f"telegram 5xx: {exc}",
                    "suppressed": False,
                }
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
                item.refresh_from_db()
                return {
                    "success": False,
                    "status": "failed",
                    "item_id": item.id,
                    "position": item.position,
                    "channel_message_id": None,
                    "sent_as_photo": False,
                    "error": f"HTTP {status_code}: {exc}",
                    "suppressed": False,
                }

        except ValueError as exc:
            # A ValueError here comes from our own pre-send contract checks -- the 1024
            # caption cap in `send_photo`, for one -- and means the request was never
            # issued. The broad handler below sets UNKNOWN, which reads as "we may have
            # sent it": `publish_digest_item` refuses to retry UNKNOWN automatically and
            # the drip steps past it, so a send that provably never happened froze the
            # item to manual review forever. FAILED is the honest state.
            log.error("Refused to send item #%s: %s", item.position, exc)
            with transaction.atomic():
                DigestItem.objects.filter(id=item.id).update(
                    channel_delivery_state=DeliveryState.FAILED,
                    channel_delivery_error=f"Refused before sending: {exc}"[:512],
                )
            item.refresh_from_db()
            return {
                "success": False,
                "status": "failed",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": f"refused: {exc}",
                "suppressed": False,
            }

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
            item.refresh_from_db()
            return {
                "success": False,
                "status": "failed",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": f"unexpected error: {exc}",
                "suppressed": False,
            }

        ch_msg_id = res_post.get("result", {}).get("message_id") if res_post else None

        if not ch_msg_id and not (res_post and res_post.get("suppressed")):
            log.error("Channel post failed for item #%s: %s", item.position, res_post)
            with transaction.atomic():
                DigestItem.objects.filter(id=item.id).update(
                    channel_delivery_state=DeliveryState.FAILED,
                    channel_delivery_error=f"No message ID returned: {res_post}"[:512],
                )
            item.refresh_from_db()
            return {
                "success": False,
                "status": "failed",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": "publish failed: no message_id",
                "suppressed": False,
            }

        if ch_msg_id:
            with transaction.atomic():
                DigestItem.objects.filter(id=item.id).update(
                    channel_message_id=ch_msg_id,
                    sent_as_photo=sent_as_photo,
                    channel_delivery_state=DeliveryState.SENT,
                    channel_delivery_error="",
                )
            item.refresh_from_db()
        elif res_post and res_post.get("suppressed"):
            with transaction.atomic():
                DigestItem.objects.filter(id=item.id).update(
                    channel_delivery_state=DeliveryState.PENDING,
                    channel_delivery_error="Suppressed by kill switch",
                )
            item.refresh_from_db()
            return {
                "success": False,
                "status": "suppressed",
                "item_id": item.id,
                "position": item.position,
                "channel_message_id": None,
                "sent_as_photo": False,
                "error": "Suppressed by kill switch",
                "suppressed": True,
            }

        return {
            "success": True,
            "status": "sent",
            "item_id": item.id,
            "position": item.position,
            "channel_message_id": ch_msg_id,
            "sent_as_photo": sent_as_photo,
            "error": None,
            "suppressed": False,
        }
    finally:
        if close_client:
            client.close()


#: A delivery state that will not change on its own. UNKNOWN is terminal on purpose:
#: publish_digest_item refuses to retry it automatically, so treating it as unfinished would
#: stall a block forever behind an item that can only be resolved by hand.
TERMINAL_DELIVERY_STATES = (DeliveryState.SENT, DeliveryState.FAILED, DeliveryState.UNKNOWN)


def refresh_digest_status(digest: Digest) -> str:
    """Set the digest's status from its items, and return it.

    Items are now spread over twelve hours, so there is no single moment at which a digest
    becomes published. The per-item truth already lives in DigestItem.channel_delivery_state;
    the digest reads it rather than keeping a second copy.

    A digest with no items always stays COMPOSED. (digest_date, edition) is unique, so any
    other status refuses every later attempt at the slot — including the pipeline's own, once
    the LLM stage finishes. That is what cost 2026-08-21 its post.
    """
    states = list(digest.items.values_list("channel_delivery_state", flat=True))
    if not states:
        return digest.status
    if any(state not in TERMINAL_DELIVERY_STATES for state in states):
        return digest.status

    if DeliveryState.SENT in states:
        digest.status = Digest.Status.PUBLISHED
        fields = ["status"]
        if digest.published_at is None:
            digest.published_at = timezone.now()
            fields.append("published_at")
        digest.save(update_fields=fields)
    else:
        digest.status = Digest.Status.FAILED
        digest.save(update_fields=["status"])
    return digest.status


def publish_digest(
    digest: Digest,
    client: httpx.Client | None = None,
    *,
    republish: bool = False,
) -> dict:
    """Publish each digest item as its own channel post.

    Per-item publishing (T1.14):
    - Each DigestItem gets its own sendMessage → its own channel_message_id.
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
            "suppressed": False,
            "locked": True,
        }

    def release_publish_lock():
        """Give the lock back. Every exit path after acquisition has to call this."""
        if lock_client and lock_acquired:
            try:
                lock_client.delete(f"news_radar:publish_lock:{digest.id}")
            except Exception as exc:
                log.debug("Error releasing publish lock: %s", exc)

    # Everything from here to the send loop's own `finally` runs while the lock is held,
    # so a raise inside this window - an unset channel id, a database error, a client
    # that cannot be built - has to hand the lock back before it leaves. Otherwise the
    # key survives for its full 300s TTL and the next publish of this digest is refused.
    try:
        channel_id = getattr(settings, "TELEGRAM_CHANNEL_ID", "")
        if not channel_id:
            raise ValueError("TELEGRAM_CHANNEL_ID is not configured in settings")

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
    except Exception:
        release_publish_lock()
        raise

    sent_count = 0
    skipped_count = 0
    failed_items: list[str] = []

    send_delay = getattr(settings, "TELEGRAM_SEND_DELAY", 3.0)
    try:
        for idx, item in enumerate(items):
            # Rate-limit: configurable delay between channel sends.
            if idx > 0 and send_delay > 0:
                time.sleep(send_delay)

            res_item = publish_digest_item(item, client=client, republish=republish)
            if res_item["status"] == "sent":
                sent_count += 1
            elif res_item["status"] == "skipped":
                skipped_count += 1
                if res_item.get("error"):
                    failed_items.append(f"#{item.position} ({res_item['error']})")
            elif res_item["status"] == "failed":
                failed_items.append(f"#{item.position} ({res_item.get('error', 'failed')})")
            elif res_item["status"] == "suppressed":
                pass

    finally:
        if close_client:
            client.close()
        release_publish_lock()

    # --- Status decision ---
    # The status now follows the items (refresh_digest_status), so the manual bulk path and
    # the drip path cannot disagree about what a partially delivered block means.
    refresh_digest_status(digest)
    if failed_items:
        alert_msg = (
            f"Digest {digest.digest_date}: {sent_count}/{len(items)} items posted. "
            f"Failed: {', '.join(failed_items)}"
        )
        send_admin_alert(alert_msg)
        log.error(alert_msg)
    elif not items:
        log.warning(
            "Digest %s (%s) has no items; leaving it unpublished so the slot stays open.",
            digest.digest_date,
            digest.edition,
        )
    else:
        log.info("Digest %s published: %s items posted", digest.digest_date, sent_count)

    return {
        "digest_id": digest.id,
        "digest_date": str(digest.digest_date),
        "items_sent": sent_count,
        "items_skipped": skipped_count,
        "items_failed": len(failed_items),
        "failed_items": failed_items,
        "status": digest.status,
        "suppressed": False,
    }


def publish_roundup(digest: Digest, client: httpx.Client | None = None) -> dict:
    """Send the closing summary post for a completed drip block to the channel.

    Idempotent: if digest.roundup_message_id is already set, skips.
    """
    if digest.roundup_message_id:
        log.info(
            "Roundup for digest %s already published (message %s)",
            digest.id,
            digest.roundup_message_id,
        )
        return {
            "status": "skipped",
            "message_id": digest.roundup_message_id,
            "digest_id": digest.id,
        }

    if not getattr(settings, "PUBLISHING_ENABLED", False):
        log.info("[KILL SWITCH ACTIVE] Suppressed roundup for digest %s", digest.id)
        return {"status": "suppressed", "digest_id": digest.id}

    from . import ranking

    text = ranking.render_roundup_post(digest)

    channel_id = getattr(settings, "TELEGRAM_CHANNEL_ID", "")
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not channel_id or not bot_token:
        log.warning("Telegram channel or token not configured for roundup")
        return {"status": "failed", "error": "not_configured"}

    close_client = False
    if client is None:
        client = httpx.Client(timeout=30)
        close_client = True

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": channel_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        resp = client.post(url, json=payload)
        data = resp.json()
        if data.get("ok"):
            msg_id = data["result"]["message_id"]
            digest.roundup_message_id = msg_id
            digest.save(update_fields=["roundup_message_id"])
            log.info("Roundup for digest %s published as message %s", digest.id, msg_id)
            return {"status": "sent", "message_id": msg_id, "digest_id": digest.id}
        else:
            log.error("Failed to publish roundup for digest %s: %s", digest.id, data)
            return {
                "status": "failed",
                "error": data.get("description", "unknown"),
                "digest_id": digest.id,
            }
    except Exception as exc:
        log.error("Error publishing roundup for digest %s: %s", digest.id, exc)
        return {"status": "failed", "error": str(exc), "digest_id": digest.id}
    finally:
        if close_client:
            client.close()
