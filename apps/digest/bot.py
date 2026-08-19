"""Long-polling Telegram bot for feedback callbacks and group forwards."""

import asyncio
import os

import django
from aiogram import Bot, Dispatcher, F
from aiogram.types import CallbackQuery, Message
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError, connections, transaction

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from .models import DigestItem, Feedback  # noqa: E402
from .publish import FEEDBACK_REACTIONS  # noqa: E402
from .telegram_updates import remember_group_forward  # noqa: E402

REACTION_VALUES = frozenset(reaction for _, reaction in FEEDBACK_REACTIONS)


def parse_feedback_data(data: str | None) -> tuple[int, str] | None:
    """Parse and validate callback data without trusting Telegram input."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "feedback" or parts[2] not in REACTION_VALUES:
        return None
    try:
        item_id = int(parts[1])
    except (TypeError, ValueError):
        return None
    return (item_id, parts[2]) if item_id > 0 else None


def _get_item(item_id: int) -> DigestItem | None:
    try:
        return DigestItem.objects.filter(pk=item_id).first()
    finally:
        connections.close_all()


def _record_feedback(item_id: int, user_id: int, reaction: str) -> bool:
    try:
        with transaction.atomic():
            if Feedback.objects.filter(digest_item_id=item_id, user_id=user_id).exists():
                return False
            try:
                Feedback.objects.create(
                    digest_item_id=item_id,
                    user_id=user_id,
                    reaction=reaction,
                )
            except IntegrityError:
                return False
            return True
    finally:
        connections.close_all()


async def _answer(callback: CallbackQuery, text: str) -> None:
    await callback.answer(text)


async def handle_feedback(callback: CallbackQuery) -> None:
    """Validate a callback against the stored channel post before writing feedback."""
    parsed = parse_feedback_data(callback.data)
    user = getattr(callback, "from_user", None)
    message = getattr(callback, "message", None)
    user_id = getattr(user, "id", None)
    message_chat = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    configured_channel = getattr(settings, "TELEGRAM_CHANNEL_ID", "")

    if not parsed or user_id is None or not configured_channel:
        await _answer(callback, "Noto'g'ri feedback so'rovi")
        return
    item_id, reaction = parsed
    if str(message_chat) != str(configured_channel):
        await _answer(callback, "Bu post uchun feedback qabul qilinmaydi")
        return

    item = await sync_to_async(_get_item)(item_id)
    if item is None or item.channel_message_id != message_id:
        await _answer(callback, "Bu post uchun feedback qabul qilinmaydi")
        return

    created = await sync_to_async(_record_feedback)(item.id, user_id, reaction)
    await _answer(callback, "Feedback saqlandi" if created else "Feedback allaqachon saqlangan")


async def handle_automatic_forward(message: Message) -> None:
    """Store a validated channel-to-group forward without a database race."""
    group_id = getattr(settings, "TELEGRAM_GROUP_ID", "")
    channel_id = getattr(settings, "TELEGRAM_CHANNEL_ID", "")
    if not group_id or not channel_id:
        return
    if str(getattr(getattr(message, "chat", None), "id", "")) != str(group_id):
        return
    if getattr(message, "is_automatic_forward", False) is not True:
        return

    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(getattr(origin, "chat", None), "id", None)
    origin_message_id = getattr(origin, "message_id", None)
    if getattr(origin, "type", None) != "channel":
        return
    if str(origin_chat) != str(channel_id) or origin_message_id is None:
        return
    forward_message_id = getattr(message, "message_id", None)
    if forward_message_id is None:
        return

    await asyncio.to_thread(
        remember_group_forward,
        channel_id,
        int(origin_message_id),
        int(forward_message_id),
    )


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.callback_query.register(handle_feedback, F.data.startswith("feedback:"))
    dispatcher.message.register(handle_automatic_forward)
    return dispatcher


async def run_bot() -> None:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to run the feedback bot")
    async with Bot(token=token) as bot:
        await create_dispatcher().start_polling(bot)


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
