"""Long-polling Telegram bot used for the worker heartbeat."""

import asyncio
import logging
import os

import django
from aiogram import Bot, Dispatcher
from django.conf import settings

log = logging.getLogger(__name__)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()


def create_dispatcher() -> Dispatcher:
    """Create the heartbeat bot dispatcher without group-message handlers."""
    return Dispatcher()


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
