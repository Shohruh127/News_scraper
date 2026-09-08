"""The two per-article admin buttons: write a fresh post, send it to the eval channel.

Neither creates a Digest or a DigestItem. A Digest claims a (digest_date, edition) slot,
and the uniqueness constraint on that pair is why an empty digest once cost a whole day
of posts -- an operator previewing a post must not be able to trigger that.
"""

import json

import httpx
import pytest
import respx
from django.urls import reverse

from apps.digest import llm
from apps.digest.models import Analysis, Article, Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def article(db):
    source = Source.objects.create(
        name="btn_src", connector=Source.Connector.RSS, url="https://b.test/rss", priority=50
    )
    return Article.objects.create(
        source=source,
        canonical_url="https://b.test/a",
        content_hash="btn_h1",
        title="A tool was released",
        extracted_text="The tool does one thing. It costs 5 dollars. " * 30,
        status=Article.Status.CLASSIFIED,
        meta={"image_url": "https://b.test/img.jpg"},
    )


def _row(article, lead="Kompaniya vositani chiqardi."):
    return Analysis.objects.create(
        article=article,
        stage=Analysis.Stage.EDITORIAL_UZ,
        model_tag="test",
        payload={
            "post_style": "plain_photo_v1",
            "lead_uz": lead,
            "body_1_uz": "U bitta ishni qiladi.",
            "kicker_uz": "Narxi 5 dollar.",
            "technical": {},
            "evidence_level": "vendor_claim_only",
        },
        latency_ms=1,
    )


def test_force_bypasses_the_reuse_memo(article, monkeypatch):
    """Without force an existing post is reused and the model is not called; the button
    passes force=True precisely so the operator sees what the current prompt makes of it."""
    _row(article)
    calls = []

    def fake(art, client=None):
        calls.append(art.id)
        return llm.ChatResult(
            {
                "post_style": "plain_photo_v1",
                "lead_uz": "Yangi post.",
                "body_1_uz": "",
                "kicker_uz": "",
                "technical": {},
                "evidence_level": "vendor_claim_only",
            },
            5,
            "smart",
            1,
            1,
        )

    monkeypatch.setattr(llm, "editorial_uz_for_article", fake)

    reused = llm.analyse_for_digest_logic([article.id])
    assert calls == [] and reused[0].payload["lead_uz"] == "Kompaniya vositani chiqardi."

    fresh = llm.analyse_for_digest_logic([article.id], force=True)
    assert calls == [article.id]
    assert fresh[0].payload["lead_uz"] == "Yangi post."
    assert article.analyses.filter(stage=Analysis.Stage.EDITORIAL_UZ).count() == 2


def test_the_changelist_shows_both_buttons(article, admin_client):
    response = admin_client.get(reverse("admin:digest_article_changelist"))
    body = response.content.decode()
    assert reverse("admin:digest_article_write_post", args=[article.pk]) in body
    assert reverse("admin:digest_article_send_preview", args=[article.pk]) in body


def test_write_post_calls_the_stage_with_force_and_reports_the_lead(
    article, admin_client, monkeypatch
):
    seen = {}

    def fake(ids, client=None, force=False):
        seen["ids"], seen["force"] = ids, force
        return [_row(article, lead="Yangi yozilgan lead.")]

    monkeypatch.setattr(llm, "analyse_for_digest_logic", fake)

    response = admin_client.get(
        reverse("admin:digest_article_write_post", args=[article.pk]), follow=True
    )
    assert seen == {"ids": [article.pk], "force": True}
    assert "Yangi yozilgan lead." in response.content.decode()


def test_write_post_reports_a_discarded_post_instead_of_pretending(
    article, admin_client, monkeypatch
):
    monkeypatch.setattr(llm, "analyse_for_digest_logic", lambda ids, client=None, force=False: [])
    response = admin_client.get(
        reverse("admin:digest_article_write_post", args=[article.pk]), follow=True
    )
    assert "discarded_violations" in response.content.decode()


def test_send_preview_refuses_without_an_eval_channel(article, admin_client, settings):
    _row(article)
    settings.TELEGRAM_EVAL_CHANNEL_ID = ""
    settings.TELEGRAM_CHANNEL_ID = "-100live"
    response = admin_client.get(
        reverse("admin:digest_article_send_preview", args=[article.pk]), follow=True
    )
    assert "TELEGRAM_EVAL_CHANNEL_ID" in response.content.decode()


@respx.mock
def test_send_preview_posts_a_photo_to_the_eval_channel_only(article, admin_client, settings):
    """The eval channel by name. Even with a live channel configured, nothing goes there."""
    _row(article)
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "t"
    settings.TELEGRAM_EVAL_CHANNEL_ID = "-100eval"
    settings.TELEGRAM_CHANNEL_ID = "-100live"

    photo = respx.post("https://api.telegram.org/bott/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    )
    text = respx.post("https://api.telegram.org/bott/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 43}})
    )

    response = admin_client.get(
        reverse("admin:digest_article_send_preview", args=[article.pk]), follow=True
    )

    assert photo.call_count == 1 and not text.called
    payload = json.loads(photo.calls[0].request.content)
    assert payload["chat_id"] == "-100eval"
    assert "<a " in payload["caption"], "the rendered caption, link on a word in the lead"
    assert "xabar 42" in response.content.decode()


def test_send_preview_needs_a_post_first(article, admin_client, settings):
    from html import unescape

    settings.TELEGRAM_EVAL_CHANNEL_ID = "-100eval"
    response = admin_client.get(
        reverse("admin:digest_article_send_preview", args=[article.pk]), follow=True
    )
    # unescape: the admin renders the apostrophe in "yo'q" as &#x27;.
    assert "hali post yo'q" in unescape(response.content.decode())
