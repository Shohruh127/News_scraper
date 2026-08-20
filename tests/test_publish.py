"""Tests for per-item publishing (T1.14).

Acceptance criteria from REMAINING_WORK.md:
1. 15 items produce 15 distinct channel_message_id values.
2. A failure on item 8 leaves items 1-7 sent and the digest FAILED.
3. Each item's appendix matches its own post and not a neighbour's.
4. Kill switch leaves digest COMPOSED with no message IDs.
"""

import json
from html import unescape

import httpx
import pytest
import respx
from django.utils import timezone

from apps.digest import publish, ranking
from apps.digest.models import Analysis, Article, Digest, DigestItem, Source
from tests.helpers import make_editorial


@pytest.fixture(autouse=True)
def zero_send_delay(settings):
    settings.TELEGRAM_SEND_DELAY = 0


@pytest.fixture
def digest_item_factory(db):
    """Build one renderable DigestItem with a chosen archetype and detail block."""

    def _make(archetype, detail):
        source = Source.objects.create(
            name=f"src_{archetype}",
            connector=Source.Connector.RSS,
            url="https://example.com/rss",
            priority=80,
        )
        article = Article.objects.create(
            source=source,
            canonical_url=f"https://example.com/{archetype}",
            content_hash=f"h_{archetype}",
            title="Fixture article",
            extracted_text="Body " * 60,
            status=Article.Status.CLASSIFIED,
        )
        en, uz = make_editorial(article)
        en.payload["archetype"] = archetype
        en.save(update_fields=["payload"])
        uz.payload.update(detail)
        uz.save(update_fields=["payload"])

        digest = Digest.objects.create(digest_date=timezone.localdate())
        return DigestItem.objects.create(digest=digest, article=article, position=1, score=0.9)

    return _make


@pytest.fixture
def digest_15(db):
    """Create a digest with 15 items, each with full editorial analyses."""
    src = Source.objects.create(
        name="pub_source",
        connector=Source.Connector.RSS,
        url="https://example.com/rss",
    )
    digest = Digest.objects.create(
        digest_date=timezone.localdate(),
        status=Digest.Status.COMPOSED,
    )
    for i in range(1, 16):
        art = Article.objects.create(
            source=src,
            canonical_url=f"https://example.com/art-{i}",
            content_hash=f"hash_{i:03d}",
            title=f"Article {i}",
            extracted_text=f"Article {i} text " * 30,
            status=Article.Status.CLASSIFIED,
        )
        Analysis.objects.create(
            article=art,
            stage=Analysis.Stage.CLASSIFICATION,
            model_tag="gemma4:31b",
            payload={
                "primary_topic": "frontier_models",
                "maturity": "live_product",
                "novelty": 9,
                "evidence": 8,
                "production_readiness": 9,
                "reason": "Top product",
            },
            latency_ms=8000,
        )
        make_editorial(art, summary_uz=f"Maqola {i} — muhim yangilik.")
        DigestItem.objects.create(
            digest=digest,
            article=art,
            position=i,
            score=1.0 - i * 0.01,
        )
    return digest


@pytest.fixture
def digest_1(db):
    """Create a minimal digest with 1 item for simple tests."""
    src = Source.objects.create(
        name="pub_source",
        connector=Source.Connector.RSS,
        url="https://example.com/rss",
    )
    art = Article.objects.create(
        source=src,
        canonical_url="https://example.com/art-pub",
        content_hash="hash_pub",
        title="Published Article",
        extracted_text="Article text " * 30,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 9,
            "evidence": 8,
            "production_readiness": 9,
            "reason": "Top product",
        },
        latency_ms=8000,
    )
    make_editorial(art)
    return ranking.compose_digest(timezone.localdate())


def test_publish_kill_switch_suppresses_network(db, digest_1, settings):
    """When PUBLISHING_ENABLED is False, publication is suppressed and status remains composed."""
    settings.PUBLISHING_ENABLED = False
    settings.TELEGRAM_BOT_TOKEN = "dummy_token"
    settings.TELEGRAM_CHANNEL_ID = "-100123456"
    settings.TELEGRAM_GROUP_ID = "-100654321"

    res = publish.publish_digest(digest_1)
    assert res.get("suppressed") is True
    assert res["items_sent"] == 0
    assert res["items_failed"] == 0
    assert res["failed_items"] == []

    digest_1.refresh_from_db()
    assert digest_1.status == Digest.Status.COMPOSED
    assert digest_1.published_at is None
    assert not digest_1.items.filter(channel_message_id__isnull=False).exists()


def test_a_publish_never_writes_the_bot_token_to_the_log(db, digest_1, settings, caplog):
    """The token is in the URL of every Telegram call. httpx logs URLs at INFO.

    It has leaked into terminal output twice. The logger config is the fix, not discipline.
    """
    import logging

    settings.PUBLISHING_ENABLED = False
    settings.TELEGRAM_BOT_TOKEN = "123456:SECRET-TOKEN-VALUE"

    with caplog.at_level(logging.DEBUG):
        publish.publish_digest(digest_1)

    assert "SECRET-TOKEN-VALUE" not in caplog.text
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING



@respx.mock
def test_15_items_produce_15_distinct_channel_message_ids(db, digest_15, settings):
    """T1.14 acceptance: 15 items produce 15 distinct channel_message_id values."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = ""  # No group to simplify this test

    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    # Each item gets its own sendMessage → unique message_id starting at 1001
    msg_counter = iter(range(1001, 1016))
    respx.post(f"{base_tg}/sendMessage").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": next(msg_counter)}},
        )
    )

    res = publish.publish_digest(digest_15)
    assert res["items_sent"] == 15
    assert res["items_failed"] == 0
    assert res["status"] == Digest.Status.PUBLISHED

    digest_15.refresh_from_db()
    assert digest_15.status == Digest.Status.PUBLISHED

    # Verify 15 distinct channel_message_id values
    msg_ids = list(
        DigestItem.objects.filter(digest=digest_15).values_list("channel_message_id", flat=True)
    )
    assert len(msg_ids) == 15
    assert len(set(msg_ids)) == 15, f"Expected 15 distinct IDs, got {msg_ids}"
    assert set(msg_ids) == set(range(1001, 1016))


@respx.mock
def test_publishing_twice_posts_each_item_once(db, digest_15, settings, monkeypatch):
    """The defect this guards: 61 of 82 live channel messages had no database record."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    counter = iter(range(600, 700))
    route = respx.post(f"{base_tg}/sendMessage").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"ok": True, "result": {"message_id": next(counter)}}
        )
    )
    DigestItem.objects.filter(digest=digest_15, position__gt=3).delete()

    first = publish.publish_digest(digest_15)
    calls_after_first = route.call_count
    second = publish.publish_digest(digest_15)

    assert first["items_sent"] == 3
    assert first["items_skipped"] == 0
    assert second["items_sent"] == 0
    assert second["items_skipped"] == 3
    assert route.call_count == calls_after_first, "the second run must send nothing"

    ids = list(
        DigestItem.objects.filter(digest=digest_15)
        .order_by("position")
        .values_list("channel_message_id", flat=True)
    )
    assert ids == [600, 601, 602], "the first run's message IDs must survive the second run"


@respx.mock
def test_a_partly_published_digest_resumes_instead_of_restarting(db, digest_15, settings):
    """An item whose send failed is the only one a second run may post."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    DigestItem.objects.filter(digest=digest_15, position__gt=3).delete()
    done = DigestItem.objects.filter(digest=digest_15, position__lt=3)
    for offset, item in enumerate(done):
        item.channel_message_id = 900 + offset
        item.save(update_fields=["channel_message_id"])

    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 950}})
    )

    res = publish.publish_digest(digest_15)

    assert res["items_sent"] == 1
    assert res["items_skipped"] == 2
    assert route.call_count == 1
    assert DigestItem.objects.get(digest=digest_15, position=3).channel_message_id == 950


@respx.mock
def test_republish_overrides_the_guard(db, digest_15, settings):
    """The one escape hatch: a post deleted by hand can be sent again, on purpose."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    DigestItem.objects.filter(digest=digest_15, position__gt=1).delete()
    item = DigestItem.objects.get(digest=digest_15, position=1)
    item.channel_message_id = 700
    item.save(update_fields=["channel_message_id"])

    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 800}})
    )

    res = publish.publish_digest(digest_15, republish=True)

    assert res["items_sent"] == 1
    assert res["items_skipped"] == 0
    assert route.call_count == 1
    item.refresh_from_db()
    assert item.channel_message_id == 800


@respx.mock
def test_a_missing_appendix_alerts_but_leaves_the_digest_published(
    db, digest_15, settings, monkeypatch
):
    """Every post landed. A missing auto-forward is a degraded post, not a failed digest.

    Marking it FAILED is what invited the re-runs that put 61 untracked messages in the channel.
    """
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_GROUP_ID = "-100222222"
    settings.TELEGRAM_ADMIN_CHAT_ID = "999888777"
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    counter = iter(range(500, 600))
    respx.post(f"{base_tg}/sendMessage").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"ok": True, "result": {"message_id": next(counter)}}
        )
    )
    monkeypatch.setattr(publish, "find_group_forward_message_id", lambda _msg_id: None)
    DigestItem.objects.filter(digest=digest_15, position__gt=2).delete()

    res = publish.publish_digest(digest_15)

    assert res["items_sent"] == 2
    assert res["items_failed"] == 0
    assert len(res["appendix_failures"]) == 2
    assert res["status"] == Digest.Status.PUBLISHED

    digest_15.refresh_from_db()
    assert digest_15.status == Digest.Status.PUBLISHED
    assert digest_15.published_at is not None


@respx.mock
def test_publish_digest_acquires_lock_and_rejects_concurrent_run(
    db, digest_15, settings, monkeypatch
):
    """When a publish lock is held for the digest, concurrent publish_digest skips immediately."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"
    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})
    )

    class FakeRedisLock:
        def set(self, key, val, nx=False, ex=None):
            return False  # lock acquisition fails (already held)

        def get(self, key):
            return b"other-worker:1234"

        def delete(self, key):
            pass

        def close(self):
            pass

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", lambda url: FakeRedisLock())

    res = publish.publish_digest(digest_15)
    assert res.get("locked") is True
    assert res["items_sent"] == 0
    assert route.call_count == 0


@respx.mock
def test_publish_digest_refreshes_item_from_db_before_send(db, digest_15, settings):
    """If another process sets channel_message_id mid-run, refresh_from_db skips it."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    DigestItem.objects.filter(digest=digest_15, position__gt=2).delete()
    item1 = DigestItem.objects.get(digest=digest_15, position=1)
    item2 = DigestItem.objects.get(digest=digest_15, position=2)

    def send_handler(request):
        # Simulate concurrent worker publishing item 2 while item 1 is being sent
        item2.channel_message_id = 8888
        item2.save(update_fields=["channel_message_id"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7777}})

    respx.post(f"{base_tg}/sendMessage").mock(side_effect=send_handler)

    res = publish.publish_digest(digest_15)

    assert res["items_sent"] == 1
    assert res["items_skipped"] == 1
    item1.refresh_from_db()
    item2.refresh_from_db()
    assert item1.channel_message_id == 7777
    assert item2.channel_message_id == 8888


@respx.mock
def test_failure_on_item_8_leaves_1_through_7_sent_digest_failed(db, digest_15, settings):
    """T1.14 acceptance: failure on item 8 leaves 1-7 sent, digest FAILED."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = ""
    settings.TELEGRAM_ADMIN_CHAT_ID = "999888777"

    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    call_count = 0

    def send_side_effect(request):
        nonlocal call_count
        call_count += 1
        # Items 1-7 succeed (calls 1-7), item 8 fails (call 8)
        if call_count == 8:
            return httpx.Response(200, json={"ok": False, "description": "rate limit"})
        # Items 9-15 succeed (calls 9-15), plus admin alert (call 16)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 1000 + call_count}},
        )

    respx.post(f"{base_tg}/sendMessage").mock(side_effect=send_side_effect)

    res = publish.publish_digest(digest_15)

    # Item 8 failed, so digest should be FAILED
    assert res["status"] == Digest.Status.FAILED
    # 14 sent (items 1-7 + 9-15), 1 failed (item 8)
    assert res["items_sent"] == 14
    assert res["items_failed"] == 1

    digest_15.refresh_from_db()
    assert digest_15.status == Digest.Status.FAILED

    # Items 1-7 should have channel_message_id
    first_7 = DigestItem.objects.filter(digest=digest_15, position__lte=7)
    for item in first_7:
        assert item.channel_message_id is not None

    # Item 8 should NOT have channel_message_id
    item_8 = DigestItem.objects.get(digest=digest_15, position=8)
    assert item_8.channel_message_id is None


@respx.mock
def test_each_items_appendix_matches_its_own_post(db, digest_15, settings, monkeypatch):
    """T1.14 acceptance: each item's appendix replies to its own auto-forwarded post."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = "-100222222"

    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    # Track sendMessage calls to verify appendix reply_to targets
    send_calls = []
    msg_counter = iter(range(2001, 2100))

    def send_handler(request):
        import json

        data = json.loads(request.content.decode())
        mid = next(msg_counter)
        send_calls.append({**data, "_msg_id": mid})
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": mid}},
        )

    respx.post(f"{base_tg}/sendMessage").mock(side_effect=send_handler)

    monkeypatch.setattr(
        publish,
        "find_group_forward_message_id",
        lambda channel_message_id: channel_message_id + 3000,
    )
    # Use only 3 items for this test to keep it manageable
    DigestItem.objects.filter(digest=digest_15, position__gt=3).delete()

    res = publish.publish_digest(digest_15)
    assert res["items_sent"] == 3

    # Verify each appendix reply_to_message_id refers to a unique forward
    reply_calls = [c for c in send_calls if c.get("reply_to_message_id")]
    assert len(reply_calls) == 3, f"Expected 3 appendix replies, got {len(reply_calls)}"

    reply_to_ids = [c["reply_to_message_id"] for c in reply_calls]
    assert len(set(reply_to_ids)) == 3, (
        f"Each appendix must reply to its own forward: {reply_to_ids}"
    )


@respx.mock
def test_publish_digest_live_success_single_item(db, digest_1, settings, monkeypatch):
    """Backwards compatibility: single-item digest publishes correctly."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = "-100222222"

    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    respx.post(f"{base_tg}/sendMessage").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "result": {"message_id": 501}}),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 502}}),
        ]
    )

    monkeypatch.setattr(publish, "find_group_forward_message_id", lambda _channel_message_id: 777)
    res = publish.publish_digest(digest_1)
    assert res["items_sent"] == 1
    assert res["status"] == Digest.Status.PUBLISHED
    assert res["suppressed"] is False

    item = DigestItem.objects.get(digest=digest_1)
    assert item.channel_message_id == 501
    assert item.group_message_id == 502


@respx.mock
def test_edit_and_delete_message(settings):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    respx.post(f"{base_tg}/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 501}})
    )
    respx.post(f"{base_tg}/deleteMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )

    res_edit = publish.edit_message(chat_id="-100111111", message_id=501, new_text="Edited")
    assert res_edit["ok"] is True

    res_del = publish.delete_message(chat_id="-100111111", message_id=501)
    assert res_del["ok"] is True


@respx.mock
def test_send_admin_alert_and_source_failure_trigger(db, settings):
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_ADMIN_CHAT_ID = "999888777"
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    from apps.digest import tasks

    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})
    )

    src = Source.objects.create(
        name="failing_source",
        connector=Source.Connector.RSS,
        url="https://example.com/dead",
    )

    for _ in range(settings.SOURCE_DEGRADED_AFTER):
        tasks._record_failure(src, Exception("Connection timeout"))

    src.refresh_from_db()
    assert src.is_degraded is True
    assert src.consecutive_failures == 3
    assert route.call_count == 1


@respx.mock
def test_a_failed_alert_is_not_recorded_as_sent(db, settings):
    """A rejected alert leaves last_alerted_on unset so the next failure retries."""
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_ADMIN_CHAT_ID = "999888777"
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    from apps.digest import tasks

    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(
            403, json={"ok": False, "description": "Forbidden: bot can't initiate conversation"}
        )
    )

    src = Source.objects.create(
        name="rejected_alert_source",
        connector=Source.Connector.RSS,
        url="https://example.com/dead",
    )

    for _ in range(settings.SOURCE_DEGRADED_AFTER):
        tasks._record_failure(src, Exception("Connection timeout"))

    src.refresh_from_db()
    assert route.call_count == 1
    assert src.is_degraded is True
    assert src.enabled is True
    assert src.last_alerted_on is None


@pytest.mark.django_db
def test_unknown_archetype_falls_back_without_raising(digest_item_factory, settings):
    """An archetype we do not recognise must simplify the layout, never lose the post."""
    settings.POST_FORMAT_V2_ENABLED = False
    item = digest_item_factory(archetype="teleportation", detail={})

    html = ranking.render_item_post(item)

    assert "Yangi model chiqdi" in html


@pytest.mark.django_db
def test_missing_required_detail_falls_back(digest_item_factory, settings):
    """A release with no `what_changed_uz` renders as a plain post rather than an empty one."""
    settings.POST_FORMAT_V2_ENABLED = False
    item = digest_item_factory(archetype="release", detail={})

    html = ranking.render_item_post(item)

    assert "Yangi model chiqdi" in html
    assert "🚀" not in html


@pytest.mark.django_db
def test_archetype_selects_its_template(digest_item_factory, settings):
    """A release with its required field renders the release template."""
    settings.POST_FORMAT_V2_ENABLED = False
    item = digest_item_factory(
        archetype="release",
        detail={"what_changed_uz": "repeat_penalty endi 1.0 ga teng"},
    )

    html = ranking.render_item_post(item)

    assert "🚀" in html
    assert "repeat_penalty endi 1.0 ga teng" in html


ARCHETYPE_CASES = [
    (
        "release",
        "🚀",
        {"what_changed_uz": "repeat_penalty endi 1.0 ga teng"},
        {
            "benchmarks_uz": "Prefill 7–8% tezroq",
            "availability_uz": "GitHub relizlaridan yuklab olinadi",
        },
    ),
    (
        "agent_protocol",
        "🔌",
        {"connects_uz": "IDE ni ma'lumotlar bazasiga ulaydi"},
        {"deployment_uz": "Self-hosted va Ollama bilan ishlaydi"},
    ),
    (
        "risk_hardening",
        "🛡",
        {"risk_uz": "Suv belgisini o'chirish oson", "mitigation_uz": "Kriptografik imzo qo'shildi"},
        {"residual_uz": "Qisqa matnlarda hamon ishonchsiz"},
    ),
    (
        "policy",
        "⚖️",
        {
            "who_issued_uz": "Yevropa Ittifoqi",
            "who_must_comply_uz": "Generativ model provayderlari",
        },
        {"deadline_uz": "2027-yil 1-avgust"},
    ),
    (
        "research",
        "🔬",
        {"claim_uz": "Ixchamlash uzun sessiyalarni saqlaydi"},
        {
            "evidence_strength_uz": "Bitta laboratoriya, mustaqil takror yo'q",
            "reproducible_uz": "Kod ochiq emas",
        },
    ),
    (
        "company_product",
        "🏢",
        {"what_they_do_uz": "Konteyner obrazlarini avtomatik tozalaydi"},
        {"availability_uz": "Enterprise mijozlar uchun ochiq"},
    ),
]


@pytest.mark.django_db
@pytest.mark.parametrize("archetype, emoji, required, optional", ARCHETYPE_CASES)
def test_archetype_renders_with_every_field(
    digest_item_factory, archetype, emoji, required, optional, settings
):
    settings.POST_FORMAT_V2_ENABLED = False
    item = digest_item_factory(archetype=archetype, detail={**required, **optional})

    html = ranking.render_item_post(item)
    unescaped = unescape(html)

    assert emoji in html
    for value in {**required, **optional}.values():
        assert value in unescaped


@pytest.mark.django_db
@pytest.mark.parametrize("archetype, emoji, required, optional", ARCHETYPE_CASES)
def test_archetype_renders_with_no_optional_fields(
    digest_item_factory, archetype, emoji, required, optional, settings
):
    """The path most posts actually take.

    `benchmarks` is populated 40% of the time, so two release posts in three walk this branch.
    The full case is the one easy to imagine and the rarer one in production.
    """
    settings.POST_FORMAT_V2_ENABLED = False
    item = digest_item_factory(archetype=archetype, detail=required)

    html = ranking.render_item_post(item)
    unescaped = unescape(html)

    assert emoji in html
    for value in required.values():
        assert value in unescaped
    for value in optional.values():
        assert value not in unescaped


@pytest.mark.django_db
@pytest.mark.parametrize("archetype, emoji, required, optional", ARCHETYPE_CASES)
def test_visible_part_stays_short(
    digest_item_factory, archetype, emoji, required, optional, settings
):
    """Everything new lives inside the collapsed block, so the visible length must not grow."""
    settings.POST_FORMAT_V2_ENABLED = False
    item = digest_item_factory(archetype=archetype, detail={**required, **optional})

    visible = ranking.render_item_post(item).split("<blockquote expandable>")[0]

    assert len(visible) < 600


@pytest.mark.django_db
def test_appendix_prefers_uzbek_and_falls_back_to_english(digest_item_factory):
    """A stored digest from before this change still renders, in English.

    `_item_data` prefers the `_uz` value. Old payloads have none, so they fall back rather
    than rendering blank labels.
    """
    item = digest_item_factory(archetype="release", detail={})

    en = item.article.analyses.get(stage=Analysis.Stage.EDITORIAL_EN)
    en.payload["technical"] = {
        "what_was_built": "An English sentence",
        "limitations": "An English limitation",
        "local_deployable": True,
    }
    en.save(update_fields=["payload"])

    html = ranking.render_item_appendix(item)
    assert "An English sentence" in html

    uz = item.article.analyses.get(stage=Analysis.Stage.EDITORIAL_UZ)
    uz.payload["what_was_built_uz"] = "O'zbekcha jumla"
    uz.save(update_fields=["payload"])

    html = ranking.render_item_appendix(item)
    assert "O'zbekcha jumla" in unescape(html)
    assert "An English sentence" not in html
    assert "An English limitation" in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("evidence_level", "label"),
    [
        ("vendor_claim_only", "Asosiy manba da'vosi"),
        ("multiple_evidence", "Bir nechta manbada mos benchmark raqami"),
    ],
)
def test_appendix_renders_accurate_evidence_label(digest_item_factory, evidence_level, label):
    item = digest_item_factory(archetype="release", detail={})
    en = item.article.analyses.get(stage=Analysis.Stage.EDITORIAL_EN)
    en.payload["evidence_level"] = evidence_level
    en.save(update_fields=["payload"])

    html = ranking.render_item_appendix(item)

    assert label in unescape(html)


@pytest.mark.django_db
def test_appendix_unknown_evidence_level_falls_back_to_vendor_label(digest_item_factory):
    item = digest_item_factory(archetype="release", detail={})
    en = item.article.analyses.get(stage=Analysis.Stage.EDITORIAL_EN)
    en.payload.pop("evidence_level")
    en.save(update_fields=["payload"])

    html = ranking.render_item_appendix(item)

    assert "Asosiy manba da'vosi" in unescape(html)
    assert "Bir nechta manbada mos benchmark raqami" not in unescape(html)


@pytest.mark.django_db
@respx.mock
def test_send_photo_payload_structure(settings):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"

    route = respx.post("https://api.telegram.org/bottest_token/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})
    )

    photo_url = "https://cdn.example.com/image.jpg"

    res = publish.send_photo(
        chat_id="-100123",
        photo_url=photo_url,
        caption="Caption text",
    )

    assert res["result"]["message_id"] == 999
    assert route.called
    req_payload = json.loads(route.calls[0].request.content)
    assert req_payload["photo"] == "https://cdn.example.com/image.jpg"
    assert req_payload["caption"] == "Caption text"
    assert req_payload["parse_mode"] == "HTML"


@pytest.mark.django_db
@respx.mock
def test_edit_message_routes_to_caption_or_text(settings):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"

    caption_route = respx.post("https://api.telegram.org/bottest_token/editMessageCaption").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 111}})
    )
    text_route = respx.post("https://api.telegram.org/bottest_token/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 222}})
    )

    # Edit photo message
    res1 = publish.edit_message("-100123", 111, "New caption", sent_as_photo=True)
    assert res1["result"]["message_id"] == 111
    assert caption_route.called

    # Edit text message
    res2 = publish.edit_message("-100123", 222, "New text", sent_as_photo=False)
    assert res2["result"]["message_id"] == 222
    assert text_route.called


@pytest.mark.django_db
@respx.mock
def test_publish_digest_v2_with_valid_image_sends_photo(digest_item_factory, settings):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_GROUP_ID = ""
    settings.TELEGRAM_SEND_DELAY = 0
    settings.POST_FORMAT_V2_ENABLED = True

    item = digest_item_factory(archetype="release", detail={})
    item.article.meta["image_url"] = "https://example.com/img.jpg"
    item.article.save()

    photo_route = respx.post("https://api.telegram.org/bottest_token/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})
    )

    res = publish.publish_digest(item.digest)
    assert res["items_sent"] == 1
    assert photo_route.called

    req_payload = json.loads(photo_route.calls[0].request.content)
    assert req_payload["photo"] == "https://example.com/img.jpg"
    assert "caption" in req_payload
    assert req_payload["parse_mode"] == "HTML"

    item.refresh_from_db()
    assert item.channel_message_id == 777
    assert item.sent_as_photo is True


@pytest.mark.django_db
@respx.mock
def test_publish_digest_v2_photo_400_falls_back_to_text(digest_item_factory, settings):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_GROUP_ID = ""
    settings.TELEGRAM_SEND_DELAY = 0
    settings.POST_FORMAT_V2_ENABLED = True

    item = digest_item_factory(archetype="release", detail={})
    item.article.meta["image_url"] = "https://example.com/unreachable.jpg"
    item.article.save()

    # Telegram rejects photo URL with 400 Bad Request
    respx.post("https://api.telegram.org/bottest_token/sendPhoto").mock(
        return_value=httpx.Response(
            400,
            json={"ok": False, "description": "Bad Request: wrong file identifier/HTTP URL"},
        )
    )
    msg_route = respx.post("https://api.telegram.org/bottest_token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 888}})
    )

    res = publish.publish_digest(item.digest)
    assert res["items_sent"] == 1
    assert msg_route.called

    item.refresh_from_db()
    assert item.channel_message_id == 888
    assert item.sent_as_photo is False


@pytest.mark.django_db
@respx.mock
def test_publish_digest_v2_without_image_sends_text_with_disabled_preview(
    digest_item_factory, settings
):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_GROUP_ID = ""
    settings.TELEGRAM_SEND_DELAY = 0
    settings.POST_FORMAT_V2_ENABLED = True

    item = digest_item_factory(archetype="release", detail={})
    item.article.meta.pop("image_url", None)
    item.article.save()

    msg_route = respx.post("https://api.telegram.org/bottest_token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 888}})
    )

    res = publish.publish_digest(item.digest)
    assert res["items_sent"] == 1
    assert msg_route.called

    req_payload = json.loads(msg_route.calls[0].request.content)
    assert req_payload["link_preview_options"]["is_disabled"] is True

    item.refresh_from_db()
    assert item.channel_message_id == 888
    assert item.sent_as_photo is False


@pytest.mark.django_db
@respx.mock
def test_publish_digest_500_sets_unknown_and_alerts_admin(digest_item_factory, settings):
    from apps.digest.models import DeliveryState

    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_ADMIN_CHAT_ID = "12345"
    settings.TELEGRAM_GROUP_ID = ""
    settings.TELEGRAM_SEND_DELAY = 0

    item = digest_item_factory(archetype="release", detail={})

    # Channel send returns 500 Internal Server Error
    channel_route = respx.post(
        "https://api.telegram.org/bottest_token/sendMessage",
        json__chat_id="-100channel",
    ).mock(
        return_value=httpx.Response(500, json={"ok": False, "description": "Internal Server Error"})
    )
    # Admin alert succeeds
    admin_alert_route = respx.post(
        "https://api.telegram.org/bottest_token/sendMessage",
        json__chat_id="12345",
    ).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 111}}))

    res = publish.publish_digest(item.digest)
    assert res["items_sent"] == 0
    assert len(res["failed_items"]) == 1
    assert channel_route.called
    assert admin_alert_route.called

    item.refresh_from_db()
    assert item.channel_delivery_state == DeliveryState.UNKNOWN
    assert "500" in item.channel_delivery_error


@pytest.mark.django_db
@respx.mock
def test_publish_digest_timeout_sets_unknown_and_alerts_admin(digest_item_factory, settings):
    from apps.digest.models import DeliveryState

    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_ADMIN_CHAT_ID = "12345"
    settings.TELEGRAM_GROUP_ID = ""
    settings.TELEGRAM_SEND_DELAY = 0

    item = digest_item_factory(archetype="release", detail={})

    channel_route = respx.post(
        "https://api.telegram.org/bottest_token/sendMessage",
        json__chat_id="-100channel",
    ).mock(side_effect=httpx.ConnectTimeout("Connection timed out"))
    admin_alert_route = respx.post(
        "https://api.telegram.org/bottest_token/sendMessage",
        json__chat_id="12345",
    ).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 112}}))

    res = publish.publish_digest(item.digest)
    assert res["items_sent"] == 0
    assert len(res["failed_items"]) == 1
    assert channel_route.called
    assert admin_alert_route.called

    item.refresh_from_db()
    assert item.channel_delivery_state == DeliveryState.UNKNOWN
    assert "Timeout" in item.channel_delivery_error


@pytest.mark.django_db
@respx.mock
def test_publish_digest_unknown_state_skipped_on_rerun(digest_item_factory, settings):
    from apps.digest.models import DeliveryState

    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_ADMIN_CHAT_ID = "12345"
    settings.TELEGRAM_GROUP_ID = ""
    settings.TELEGRAM_SEND_DELAY = 0

    item = digest_item_factory(archetype="release", detail={})
    item.channel_delivery_state = DeliveryState.UNKNOWN
    item.save()

    channel_send_route = respx.post(
        "https://api.telegram.org/bottest_token/sendMessage",
        json__chat_id="-100channel",
    ).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 999}}))
    respx.post(
        "https://api.telegram.org/bottest_token/sendMessage",
        json__chat_id="12345",
    ).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 113}}))

    res = publish.publish_digest(item.digest)
    assert res["items_sent"] == 0
    assert res["items_skipped"] == 1
    # Channel post must NOT be called for unknown item
    assert not channel_send_route.called


@pytest.mark.django_db
@respx.mock
def test_publish_digest_stale_sending_promoted_to_unknown(digest_item_factory, settings):
    from apps.digest.models import DeliveryState

    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_ADMIN_CHAT_ID = "12345"
    settings.TELEGRAM_GROUP_ID = ""
    settings.TELEGRAM_SEND_DELAY = 0

    item = digest_item_factory(archetype="release", detail={})
    item.channel_delivery_state = DeliveryState.SENDING
    item.save()

    channel_send_route = respx.post(
        "https://api.telegram.org/bottest_token/sendMessage",
        json__chat_id="-100channel",
    ).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 999}}))
    respx.post(
        "https://api.telegram.org/bottest_token/sendMessage",
        json__chat_id="12345",
    ).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 114}}))

    res = publish.publish_digest(item.digest)
    assert res["items_sent"] == 0
    assert res["items_skipped"] == 1
    assert not channel_send_route.called

    item.refresh_from_db()
    assert item.channel_delivery_state == DeliveryState.UNKNOWN
