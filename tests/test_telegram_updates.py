from apps.digest import telegram_updates


class FakeRedis:
    def __init__(self, value=None):
        self.value = value
        self.calls = []

    def set(self, key, value, ex):
        self.calls.append((key, value, ex))
        self.value = str(value).encode()

    def get(self, key):
        self.calls.append(("get", key))
        return self.value


def test_remember_group_forward_uses_namespaced_ttl():
    client = FakeRedis()

    telegram_updates.remember_group_forward(
        "-1001",
        123,
        456,
        client=client,
        ttl=17,
    )

    assert client.calls == [("news_radar:telegram_forward:-1001:123", "456", 17)]


def test_wait_for_group_forward_reads_prepopulated_mapping():
    client = FakeRedis(b"456")

    assert (
        telegram_updates.wait_for_group_forward(
            "-1001",
            123,
            client=client,
            max_retries=1,
        )
        == 456
    )


def test_wait_for_group_forward_retries_then_returns_none(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(telegram_updates.time, "sleep", lambda _delay: None)

    assert (
        telegram_updates.wait_for_group_forward(
            "-1001",
            123,
            client=client,
            max_retries=3,
            retry_delay=0,
        )
        is None
    )
    assert [call[0] for call in client.calls] == ["get", "get", "get"]


def test_wait_for_group_forward_rejects_malformed_mapping():
    client = FakeRedis(b"not-an-id")

    assert (
        telegram_updates.wait_for_group_forward(
            "-1001",
            123,
            client=client,
            max_retries=1,
        )
        is None
    )
