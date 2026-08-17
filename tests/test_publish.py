import httpx
import pytest
import respx
from django.utils import timezone

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
    Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.EDITORIAL,
        model_tag="gemma4:31b",
        payload={
            "summary_uz": "Yangi model nashr qilindi.",
            "why_it_matters_uz": "Muhim yangilik.",
            "leadership_uz": "Boshqaruv uchun tavsiya.",
            "technical": {
                "what_was_built": "Model",
                "local_deployable": True,
            },
            "uzbekistan_application_uz": "O'zbekistonda qo'llash mumkin.",
            "evidence_level": "vendor_claim_only",
        },
        latency_ms=8000,
    )
    return ranking.compose_digest(timezone.localdate())


def test_publish_kill_switch_suppresses_network(db, test_digest, settings):
    """When PUBLISHING_ENABLED is False, publication is suppressed and status remains composed."""
    settings.PUBLISHING_ENABLED = False
    settings.TELEGRAM_BOT_TOKEN = "dummy_token"
    settings.TELEGRAM_CHANNEL_ID = "-100123456"
    settings.TELEGRAM_GROUP_ID = "-100654321"

    res = publish.publish_digest(test_digest)
    assert res.get("suppressed") is True

    test_digest.refresh_from_db()
    assert test_digest.status == Digest.Status.COMPOSED
    assert test_digest.published_at is None
    assert not test_digest.items.filter(channel_message_id__isnull=False).exists()


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
                            "forward_origin": {
                                "type": "channel",
                                "chat": {"id": -100111111},
                                "message_id": 501,
                            },
                        },
                    }
                ],
            },
        )
    )

    res = publish.publish_digest(test_digest)

    assert res["channel_message_id"] == 501
    assert res["group_message_id"] == 502
    assert res["status"] == Digest.Status.PUBLISHED

    item = DigestItem.objects.get(digest=test_digest)
    assert item.channel_message_id == 501
    assert item.group_message_id == 502


@respx.mock
def test_missing_group_forward_marks_failed(db, test_digest, settings):
    """If group is set but forward message not found, mark digest as FAILED and alert."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = "-100222222"
    settings.TELEGRAM_ADMIN_CHAT_ID = "999888777"

    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    # Channel post succeeds
    respx.post(f"{base_tg}/sendMessage").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "result": {"message_id": 501}}),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 999}}),  # Admin alert
        ]
    )

    # getUpdates returns empty results (forward not found)
    respx.get(f"{base_tg}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )

    res = publish.publish_digest(test_digest)
    assert res.get("status") == Digest.Status.FAILED

    test_digest.refresh_from_db()
    assert test_digest.status == Digest.Status.FAILED


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
