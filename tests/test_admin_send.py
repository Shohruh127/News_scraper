"""Tests for manual send button in Django admin for DigestItem."""

import httpx
import pytest
import respx
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.digest.admin import DigestItemAdmin
from apps.digest.models import Analysis, Article, DeliveryState, Digest, DigestItem, Source
from tests.helpers import make_editorial

User = get_user_model()


@pytest.fixture
def digest_item(db):
    """Create a single renderable DigestItem."""
    src = Source.objects.create(
        name="test_source",
        connector=Source.Connector.RSS,
        url="https://example.com/rss",
    )
    art = Article.objects.create(
        source=src,
        canonical_url="https://example.com/item-1",
        content_hash="hash_item_1",
        title="Sample Item 1",
        extracted_text="Article text content " * 20,
        status=Article.Status.CLASSIFIED,
    )
    Analysis.objects.create(
        article=art,
        stage=Analysis.Stage.CLASSIFICATION,
        model_tag="gemma4:31b",
        payload={
            "primary_topic": "frontier_models",
            "maturity": "reproducible_open_source",
            "novelty": 8,
            "evidence": 8,
            "production_readiness": 8,
            "reason": "Clear open release",
        },
        latency_ms=100,
    )
    make_editorial(
        art,
        summary_uz="Muhim AI yangiligi matni.",
        lead_uz="Yangi model ishga tushirildi.",
        link_anchor_uz="tushirildi",
        body_1_uz="Asosiy tafsilotlar va raqamlar.",
    )
    d = Digest.objects.create(digest_date=timezone.now().date())
    return DigestItem.objects.create(
        digest=d,
        article=art,
        position=1,
        score=0.85,
        channel_delivery_state=DeliveryState.PENDING,
    )


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser("admin", "admin@example.com", "password")


def test_admin_has_manual_send_button_and_action(digest_item, admin_user):
    """Check that manual send action and button exist on DigestItemAdmin."""
    admin_instance = DigestItemAdmin(DigestItem, AdminSite())
    button_html = admin_instance.manual_send_action(digest_item)

    assert "Send" in button_html
    assert f"/admin/digest/digestitem/{digest_item.pk}/send/" in button_html
    assert 'class="button"' in button_html


@respx.mock
def test_send_item_view_success(digest_item, admin_user, settings):
    """Clicking Send button sends the item to Telegram and redirects to changelist."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:BOT-TOKEN"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 555}})
    )

    admin_instance = DigestItemAdmin(DigestItem, AdminSite())
    rf = RequestFactory()
    url = reverse("admin:digest_digestitem_send", args=[digest_item.pk])
    request = rf.get(url)
    request.user = admin_user
    request.session = {}
    request._messages = FallbackStorage(request)

    response = admin_instance.send_item_view(request, digest_item.pk)

    assert response.status_code == 302
    assert response.url == reverse("admin:digest_digestitem_changelist")

    digest_item.refresh_from_db()
    assert digest_item.channel_delivery_state == DeliveryState.SENT
    assert digest_item.channel_message_id == 555

    msgs = list(get_messages(request))
    assert len(msgs) == 1
    assert "successfully sent" in msgs[0].message


def test_send_item_view_kill_switch(digest_item, admin_user, settings):
    """When kill switch is active, send_item_view warns and suppresses."""
    settings.PUBLISHING_ENABLED = False

    admin_instance = DigestItemAdmin(DigestItem, AdminSite())
    rf = RequestFactory()
    url = reverse("admin:digest_digestitem_send", args=[digest_item.pk])
    request = rf.get(url)
    request.user = admin_user
    request.session = {}
    request._messages = FallbackStorage(request)

    response = admin_instance.send_item_view(request, digest_item.pk)
    assert response.status_code == 302

    digest_item.refresh_from_db()
    assert digest_item.channel_message_id is None

    msgs = list(get_messages(request))
    assert len(msgs) == 1
    assert "PUBLISHING_ENABLED is False" in msgs[0].message


@respx.mock
def test_send_selected_items_action(digest_item, admin_user, settings):
    """Test the bulk admin action to send multiple items."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:BOT-TOKEN"
    settings.TELEGRAM_CHANNEL_ID = "-100111111"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 777}})
    )

    admin_instance = DigestItemAdmin(DigestItem, AdminSite())
    rf = RequestFactory()
    request = rf.post("/admin/digest/digestitem/")
    request.user = admin_user
    request.session = {}
    request._messages = FallbackStorage(request)

    admin_instance.send_selected_items(request, DigestItem.objects.filter(pk=digest_item.pk))

    digest_item.refresh_from_db()
    assert digest_item.channel_message_id == 777
    assert digest_item.channel_delivery_state == DeliveryState.SENT

    msgs = list(get_messages(request))
    assert len(msgs) == 1
    assert "1 item(s) sent successfully" in msgs[0].message
