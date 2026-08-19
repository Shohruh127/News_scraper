import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from apps.digest import bot
from apps.digest.models import Article, Digest, DigestItem, Feedback, Source


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def published_item(transactional_db):
    source = Source.objects.create(
        name="bot_source",
        connector=Source.Connector.RSS,
        url="https://bot.example/rss",
    )
    article = Article.objects.create(
        source=source,
        canonical_url="https://bot.example/item",
        content_hash="bot-item",
        title="Bot item",
        extracted_text="Body",
        status=Article.Status.CLASSIFIED,
    )
    digest = Digest.objects.create(digest_date=date(2026, 8, 19))
    return DigestItem.objects.create(
        digest=digest,
        article=article,
        position=1,
        score=0.9,
        channel_message_id=501,
    )


async def _answer(answers, text):
    answers.append(text)


@pytest.mark.parametrize("reaction", ["useful", "not_useful", "want_to_build"])
def test_valid_callback_creates_feedback(settings, published_item, reaction):
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    answers = []
    callback = SimpleNamespace(
        data=f"feedback:{published_item.id}:{reaction}",
        from_user=SimpleNamespace(id=77),
        message=SimpleNamespace(chat=SimpleNamespace(id=-100111111), message_id=501),
    )
    callback.answer = lambda text: _answer(answers, text)

    run(bot.handle_feedback(callback))

    assert list(Feedback.objects.values_list("user_id", "reaction")) == [(77, reaction)]
    assert answers == ["Feedback saqlandi"]


def test_duplicate_callback_does_not_create_second_row(settings, published_item):
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    answers = []

    def callback():
        value = SimpleNamespace(
            data=f"feedback:{published_item.id}:useful",
            from_user=SimpleNamespace(id=77),
            message=SimpleNamespace(chat=SimpleNamespace(id=-100111111), message_id=501),
        )
        value.answer = lambda text: _answer(answers, text)
        return value

    run(bot.handle_feedback(callback()))
    run(bot.handle_feedback(callback()))

    assert Feedback.objects.count() == 1
    assert answers == ["Feedback saqlandi", "Feedback allaqachon saqlangan"]


@pytest.mark.parametrize("data", [None, "feedback:nope:useful", "feedback:1:unknown"])
def test_malformed_callback_is_acknowledged_without_row(settings, published_item, data):
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    answers = []
    callback = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=77),
        message=SimpleNamespace(chat=SimpleNamespace(id=-100111111), message_id=501),
    )
    callback.answer = lambda text: _answer(answers, text)

    run(bot.handle_feedback(callback))

    assert Feedback.objects.count() == 0
    assert len(answers) == 1


def test_wrong_message_is_rejected_without_row(settings, published_item):
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    answers = []
    callback = SimpleNamespace(
        data=f"feedback:{published_item.id}:useful",
        from_user=SimpleNamespace(id=77),
        message=SimpleNamespace(chat=SimpleNamespace(id=-100111111), message_id=999),
    )
    callback.answer = lambda text: _answer(answers, text)

    run(bot.handle_feedback(callback))

    assert Feedback.objects.count() == 0
    assert len(answers) == 1


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


def test_parse_feedback_data_rejects_invalid_values():
    assert bot.parse_feedback_data("feedback:12:useful") == (12, "useful")
    assert bot.parse_feedback_data("feedback:0:useful") is None
    assert bot.parse_feedback_data("other:12:useful") is None
