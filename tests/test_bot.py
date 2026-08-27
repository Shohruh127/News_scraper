from apps.digest import bot


def test_create_dispatcher_has_no_discussion_handlers():
    assert len(bot.create_dispatcher().message.handlers) == 0
