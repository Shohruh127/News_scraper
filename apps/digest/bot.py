"""Long-polling Telegram bot for group forward tracking and heartbeat."""

import asyncio
import logging
import os

import django
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from django.conf import settings

log = logging.getLogger(__name__)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from .telegram_updates import remember_group_forward  # noqa: E402


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
    dispatcher.message.register(handle_automatic_forward)
    return dispatcher


async def _bot_heartbeat_loop() -> None:
    from . import tasks

    while True:
        try:
            await asyncio.to_thread(tasks.record_heartbeat, "bot")
        except Exception as exc:
            log.warning("Bot heartbeat error: %s", exc)
        await asyncio.sleep(30)


async def run_bot() -> None:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to run the bot")
    heartbeat_task = asyncio.create_task(_bot_heartbeat_loop())
    try:
        async with Bot(token=token) as bot:
            await create_dispatcher().start_polling(bot)
    finally:
        heartbeat_task.cancel()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
