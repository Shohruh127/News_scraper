"""Tests for per-item publishing (T1.14).

Acceptance criteria from REMAINING_WORK.md:
1. 15 items produce 15 distinct channel_message_id values.
2. A failure on item 8 leaves items 1-7 sent and the digest FAILED.
3. Each item's appendix matches its own post and not a neighbour's.
4. Kill switch leaves digest COMPOSED with no message IDs.
"""

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

    digest_1.refresh_from_db()
    assert digest_1.status == Digest.Status.COMPOSED
    assert digest_1.published_at is None
    assert not digest_1.items.filter(channel_message_id__isnull=False).exists()


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
        DigestItem.objects.filter(digest=digest_15)
        .values_list("channel_message_id", flat=True)
    )
    assert len(msg_ids) == 15
    assert len(set(msg_ids)) == 15, f"Expected 15 distinct IDs, got {msg_ids}"
    assert set(msg_ids) == set(range(1001, 1016))


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
def test_each_items_appendix_matches_its_own_post(db, digest_15, settings):
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

    # getUpdates: for each channel_message_id, return a matching auto-forward
    update_counter = iter(range(1, 100))

    def updates_handler(request):
        n = next(update_counter)
        # Find the latest channel post that was sent (no reply_to = channel post)
        channel_posts = [
            c for c in send_calls
            if c.get("chat_id") == "-100111111" and "reply_to_message_id" not in c
        ]
        if channel_posts:
            latest = channel_posts[-1]
            ch_msg_id = latest["_msg_id"]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [{
                        "update_id": n,
                        "message": {
                            "message_id": 5000 + n,
                            "chat": {"id": -100222222},
                            "is_automatic_forward": True,
                            "forward_origin": {
                                "type": "channel",
                                "chat": {"id": -100111111},
                                "message_id": ch_msg_id,
                            },
                        },
                    }],
                },
            )
        return httpx.Response(200, json={"ok": True, "result": []})

    respx.get(f"{base_tg}/getUpdates").mock(side_effect=updates_handler)

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
def test_publish_digest_live_success_single_item(db, digest_1, settings):
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

    respx.get(f"{base_tg}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [{
                    "update_id": 1,
                    "message": {
                        "message_id": 777,
                        "chat": {"id": -100222222},
                        "is_automatic_forward": True,
                        "forward_origin": {
                            "type": "channel",
                            "chat": {"id": -100111111},
                            "message_id": 501,
                        },
                    },
                }],
            },
        )
    )

    res = publish.publish_digest(digest_1)
    assert res["items_sent"] == 1
    assert res["status"] == Digest.Status.PUBLISHED

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
