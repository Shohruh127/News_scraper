from datetime import date

import httpx
import pytest
import respx

from apps.digest import publish, ranking, tasks
from apps.digest.models import Analysis, Article, Digest, DigestItem, Source


@pytest.fixture
def test_digest(db):
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
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "live_product",
            "novelty": 9,
            "evidence": 8,
            "production_readiness": 9,
            "reason": "Top product",
            "summary_uz": "Yangi model nashr qilindi.",
            "technical": {
                "what_was_built": "Model",
                "local_deployable": True,
            },
        },
        latency_ms=8000,
    )
    return ranking.compose_digest(date(2026, 8, 14))


def test_publish_kill_switch_suppresses_network(db, test_digest, settings):
    settings.PUBLISHING_ENABLED = False
    settings.TELEGRAM_BOT_TOKEN = "dummy_token"
    settings.TELEGRAM_CHANNEL_ID = "-100123456"
    settings.TELEGRAM_GROUP_ID = "-100654321"

    res = publish.publish_digest(test_digest)
    assert res["channel_message_id"] == 100001
    assert res["group_message_id"] == 100001

    test_digest.refresh_from_db()
    assert test_digest.status == Digest.Status.PUBLISHED
    assert test_digest.published_at is not None


@respx.mock
def test_publish_digest_live_success(db, test_digest, settings):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = "-100222222"

    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    # 1. Post to channel
    respx.post(f"{base_tg}/sendMessage").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "result": {"message_id": 501}}),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 502}}),
        ]
    )

    # 2. getUpdates to find forwarded message in group
    respx.get(f"{base_tg}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 777,
                            "chat": {"id": -100222222},
                            "is_automatic_forward": True,
                            "forward_from_message_id": 501,
                        },
                    }
                ],
            },
        )
    )

    res = publish.publish_digest(test_digest)

    assert res["channel_message_id"] == 501
    assert res["group_message_id"] == 502

    item = DigestItem.objects.get(digest=test_digest)
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

    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})
    )

    src = Source.objects.create(
        name="failing_source",
        connector=Source.Connector.RSS,
        url="https://example.com/dead",
    )

    # Trigger failure 3 times to mark degraded and alert
    for _ in range(settings.SOURCE_DEGRADED_AFTER):
        tasks._record_failure(src, Exception("Connection timeout"))

    src.refresh_from_db()
    assert src.is_degraded is True
    assert src.consecutive_failures == 3
    assert route.call_count == 1
