import asyncio
from types import SimpleNamespace

import pytest

from apps.digest import bot


def run(coro):
    return asyncio.run(coro)


def test_automatic_forward_stores_valid_handoff(monkeypatch, settings):
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = "-100222222"
    stored = []
    monkeypatch.setattr(bot, "remember_group_forward", lambda *args: stored.append(args))
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100222222),
        message_id=777,
        is_automatic_forward=True,
        forward_origin=SimpleNamespace(
            type="channel",
            chat=SimpleNamespace(id=-100111111),
            message_id=501,
        ),
    )

    run(bot.handle_automatic_forward(message))

    assert stored == [("-100111111", 501, 777)]


@pytest.mark.parametrize("destination", [-100111110, -100222222])
def test_invalid_or_ordinary_forward_is_ignored(monkeypatch, settings, destination):
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = "-100222222"
    stored = []
    monkeypatch.setattr(bot, "remember_group_forward", lambda *args: stored.append(args))
    message = SimpleNamespace(
        chat=SimpleNamespace(id=destination),
        message_id=777,
        is_automatic_forward=False,
        forward_origin=SimpleNamespace(
            type="channel",
            chat=SimpleNamespace(id=-100111111),
            message_id=501,
        ),
    )

    run(bot.handle_automatic_forward(message))

    assert stored == []


def test_create_dispatcher_registers_forward_handler():
    dp = bot.create_dispatcher()
    # message handler is registered
    assert len(dp.message.handlers) >= 1
