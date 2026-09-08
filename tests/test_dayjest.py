"""Plain-photo post rendering and its Telegram delivery boundaries.

Every test here exercises `plain_photo_v1`, the only style any producer writes. The file
previously built most of its fixtures on a second `dayjest_v1` style that nothing ever
stamped, so the branch under test could not be reached by an article.
"""

import json
from html import unescape

import httpx
import pytest
import respx

from apps.digest import post_format, publish, ranking
from apps.digest.models import Analysis


def plain_data():
    return {
        "post_style": post_format.PLAIN_PHOTO_STYLE,
        "headline_uz": "PDFdan interaktiv dars tayyorlaymiz",
        "lead_uz": "LessonBox yangi vosita chiqardi. U PDFni dars materiallariga aylantiradi.",
        "body_1_uz": "Konspektni yuklasangiz, quyidagilarni olasiz:\n\n"
        "– Mavzu bo'yicha slaydlar;\n– Javoblarni tekshiradigan testlar;\n– Ovozli izohlar.",
        "kicker_uz": "Sinov versiyasi ochiq, to'liq xizmat esa pulli.",
        "url": "https://example.com/news",
        "article_text": "Demo: https://example.com/demo Code: https://example.com/repo",
        "topic": "ai_agents",
    }


def test_plain_caption_keeps_the_lead_the_list_and_the_access_condition():
    rendered = post_format.render_dayjest_post(plain_data())
    assert rendered.count("<a ") == 1, "exactly one link, on a word in the lead"
    assert rendered.count("\n– ") == 3
    assert "U PDFni dars materiallariga aylantiradi." in rendered
    assert "Sinov versiyasi ochiq, to'liq xizmat esa pulli." in unescape(rendered)
    # No topic hashtag line. Checked on the unescaped text and per line, because
    # html_escape turns every Uzbek apostrophe into "&#x27;" -- a bare `"#" not in`
    # assertion passes or fails on punctuation, not on the hashtag.
    assert not any(line.startswith("#") for line in unescape(rendered).splitlines())
    assert post_format.telegram_length(rendered) <= 1024


def test_plain_caption_escapes_html():
    data = plain_data()
    data["headline_uz"] = "Model <script> & test"
    rendered = post_format.render_dayjest_post(data)
    assert "<script>" not in rendered
    assert "&lt;script&gt; &amp;" in rendered


def test_plain_caption_never_accepts_instant_view_url():
    data = plain_data()
    data["url"] = "https://t.me/iv?url=https://example.com/news&rhash=123"
    with pytest.raises(ValueError, match="Instant View"):
        post_format.render_dayjest_post(data)


def test_a_caption_never_trims_a_qualification_to_fit():
    with pytest.raises(ValueError, match="exceeds 100 characters"):
        post_format.render_dayjest_post(plain_data(), max_chars=100)


def test_the_settings_can_tighten_the_caption_but_not_loosen_it():
    """900/7 is the ceiling, not a default the environment can raise.

    A photo caption is capped at 1024 by Telegram, so a `DAYJEST_MAX_CHARS` of 4000 must
    not produce a caption the API will reject.
    """
    data = plain_data()
    # One long sentence, so the character ceiling is the guard that fires rather than
    # the sentence ceiling.
    data["body_1_uz"] = "Juda uzun izoh " * 70 + "davom etadi."
    with pytest.raises(ValueError, match="exceeds 900 characters"):
        post_format.render_dayjest_post(data, max_chars=4000, max_sentences=99)

    # And the sentence ceiling clamps the same way.
    data["body_1_uz"] = "Qisqa jumla. " * 12
    with pytest.raises(ValueError, match="exceeds 7 sentences"):
        post_format.render_dayjest_post(data, max_chars=4000, max_sentences=99)


def test_an_empty_headline_is_refused_not_silently_dropped():
    """The rewrite may empty a secondary field; the headline is not one of them.

    `parts` used to omit the bold line for a falsy headline instead of refusing, so a
    simplify pass that returned "" shipped a caption with no headline at all.
    """
    data = plain_data()
    data["headline_uz"] = ""
    with pytest.raises(ValueError, match="headline_uz is empty"):
        post_format.render_dayjest_post(data)


def test_a_paragraph_that_opens_with_a_dash_is_not_a_list():
    """`\\s*` crossed the blank line and welded the paragraph onto the one above.

    The result was counted as a one-item list, failed the 2-3 band and discarded the
    article at full LLM cost -- for a body that was never a list.
    """
    data = plain_data()
    data["body_1_uz"] = "Kompaniya modelni chiqardi.\n\n- Bu birinchi ochiq versiya."
    rendered = post_format.render_dayjest_post(data)
    assert "Kompaniya modelni chiqardi.\n\nBu birinchi ochiq versiya." in unescape(rendered)


def test_an_em_dash_list_is_counted_like_any_other():
    """The marker class omitted U+2014, so an em-dash list bypassed the count check."""
    data = plain_data()
    data["body_1_uz"] = "Nima:\n\n— birinchi\n— ikkinchi\n— uchinchi\n— tortinchi"
    with pytest.raises(ValueError, match="2-3 items"):
        post_format.render_dayjest_post(data)


def test_a_genuine_list_keeps_the_blank_line_above_it():
    data = plain_data()
    rendered = unescape(post_format.render_dayjest_post(data))
    assert "olasiz:\n\n– Mavzu" in rendered


def test_telegram_budget_counts_emoji_conservatively():
    assert post_format.telegram_length("<b>😀</b>&amp;") == 3


def test_new_style_checks_headline_numbers_without_checking_style_version():
    from apps.digest.translation_gates import check_numbers_against_source

    data = {"post_style": post_format.PLAIN_PHOTO_STYLE, "headline_uz": "Narx 99 dollarga tushdi"}
    violations = check_numbers_against_source("The price is 100 dollars.", data)
    assert len(violations) == 1
    assert "99" in violations[0] and "headline_uz" in violations[0]


@pytest.mark.django_db
def test_a_stored_plain_post_routes_around_the_legacy_caps(digest_with_item, settings):
    """The legacy 500-char/3-sentence contract must not truncate a new post."""
    settings.POST_MAX_CHARS = 100
    settings.POST_MAX_SENTENCES = 1
    item = digest_with_item.items.first()
    data = plain_data()
    item.article.extracted_text = data["article_text"]
    item.article.save(update_fields=["extracted_text"])
    Analysis.objects.create(
        article=item.article,
        stage=Analysis.Stage.EDITORIAL_UZ,
        model_tag="test",
        payload=data,
        latency_ms=1,
    )
    rendered = ranking.render_item_post(item)
    assert "Ovozli izohlar." in unescape(rendered)
    assert rendered.count("\n– ") == 3


@pytest.mark.django_db
@respx.mock
def test_legacy_text_cannot_enable_preview_from_environment(
    digest_with_item, settings, monkeypatch
):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_SEND_DELAY = 0
    settings.TELEGRAM_LINK_PREVIEW = True
    item = digest_with_item.items.first()
    item.article.meta = {"image_url": "https://example.com/image.jpg"}
    item.article.save(update_fields=["meta"])
    text = "<b>Sinov</b>\n\n" + "To'liq matn saqlanadi. " * 55
    monkeypatch.setattr(ranking, "render_item_post", lambda _: text)
    monkeypatch.setattr(
        publish.trafilatura,
        "fetch_url",
        lambda *args: pytest.fail("Long posts must not fetch a photo"),
    )
    route = respx.post("https://api.telegram.org/bottest_token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 987}})
    )
    result = publish.publish_digest(item.digest)
    assert result["items_sent"] == 1
    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    assert payload["text"] == text
    assert payload["link_preview_options"]["is_disabled"] is True
    item.refresh_from_db()
    assert item.sent_as_photo is False


@pytest.mark.django_db
@respx.mock
@pytest.mark.parametrize("photo_status", [200, 400])
def test_plain_post_sends_photo_without_text_fallback(digest_with_item, settings, photo_status):
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_ADMIN_CHAT_ID = ""
    settings.TELEGRAM_SEND_DELAY = 0
    settings.POST_FORMAT_V2_ENABLED = False
    item = digest_with_item.items.first()
    item.article.meta = {"image_url": "https://example.com/image.jpg"}
    item.article.save(update_fields=["meta"])
    Analysis.objects.create(
        article=item.article,
        stage=Analysis.Stage.EDITORIAL_UZ,
        model_tag="test",
        payload=plain_data(),
        latency_ms=1,
    )
    photo = respx.post("https://api.telegram.org/bottest_token/sendPhoto").mock(
        return_value=httpx.Response(
            photo_status, json={"ok": photo_status == 200, "result": {"message_id": 999}}
        )
    )
    text = respx.post("https://api.telegram.org/bottest_token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 998}})
    )
    result = publish.publish_digest(item.digest)
    assert photo.call_count == 1
    assert not text.called
    assert result["items_sent"] == (1 if photo_status == 200 else 0)
    payload = json.loads(photo.calls[0].request.content)
    assert payload["caption"].count("<a ") == 1
    assert "link_preview_options" not in payload


def test_send_photo_refuses_an_over_length_caption():
    """The 1024 guard has to exist below the renderer too: admin and management commands
    reach `send_photo` without going through `render_dayjest_post`."""
    with pytest.raises(ValueError, match="1024"):
        publish.send_photo(
            chat_id="-100channel", photo_url="https://example.com/i.jpg", caption="x" * 1100
        )


@pytest.mark.django_db
@respx.mock
def test_plain_post_without_photo_fails_the_item_and_sends_nothing(
    digest_with_item, settings, monkeypatch
):
    """No photo means no post -- but the item must reach a terminal state.

    The owner's rule is photo-or-nothing, so nothing may be sent. Leaving the item
    PENDING was the bug: see the companion test below.
    """
    from apps.digest.models import DeliveryState

    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"
    settings.TELEGRAM_ADMIN_CHAT_ID = ""
    item = digest_with_item.items.first()
    Analysis.objects.create(
        article=item.article,
        stage=Analysis.Stage.EDITORIAL_UZ,
        model_tag="test",
        payload=plain_data(),
        latency_ms=1,
    )
    monkeypatch.setattr(publish, "resolve_photo_url", lambda *args: None)
    result = publish.publish_digest(item.digest)
    assert result["items_sent"] == 0
    assert not respx.calls
    item.refresh_from_db()
    assert item.channel_delivery_state == DeliveryState.FAILED
    assert "Photo required" in item.channel_delivery_error


@pytest.mark.django_db
def test_a_photoless_item_does_not_block_the_rest_of_the_block(
    digest_with_two_items, settings, monkeypatch
):
    """The drip must step past an item that can never be sent.

    Measured 2026-09-07 against the first version of this feature: five consecutive
    `publish_next_item` ticks all selected item #1 and returned "skipped", leaving it
    PENDING, so item #2 never published and `publish_roundup` never fired. Because
    digests are selected FIFO by `composed_at`, every later edition queued behind it
    too. `publish_next_item` picks the lowest position still PENDING or SENDING, so a
    non-terminal state on an item that cannot succeed is an unbounded stall.
    """
    from apps.digest import tasks
    from apps.digest.models import DeliveryState

    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "test_token"
    settings.TELEGRAM_CHANNEL_ID = "-100channel"

    item1 = digest_with_two_items.items.get(position=1)
    Analysis.objects.create(
        article=item1.article,
        stage=Analysis.Stage.EDITORIAL_UZ,
        model_tag="test",
        payload=plain_data(),
        latency_ms=1,
    )
    monkeypatch.setattr(publish, "resolve_photo_url", lambda *args: None)
    monkeypatch.setattr(publish, "send_photo", lambda **kw: {"ok": True, "result": {}})
    monkeypatch.setattr(
        publish, "send_message", lambda **kw: {"ok": True, "result": {"message_id": 7}}
    )

    positions = [tasks.publish_next_item().get("position") for _ in range(3)]

    assert positions[0] == 1, "the first tick attempts item #1"
    assert 2 in positions, f"item #2 never got a turn: {positions}"
    item1.refresh_from_db()
    assert item1.channel_delivery_state == DeliveryState.FAILED


def test_a_bulleted_list_costs_one_unit_of_the_sentence_budget():
    """The item count already bounds a list; charging each bullet bounded it twice.

    Measured 2026-09-08: a 647-character watermarking post -- well inside the 900-character
    limit -- was discarded for "10 sentences" because a three-item list, its intro line,
    a two-sentence lead and a kicker added up past seven. The budget exists to stop
    rambling prose, and a list is not prose.
    """
    prose_only = "<b>Sarlavha</b>\n\nBirinchi gap. Ikkinchi gap.\n\nUchinchi gap."
    assert post_format.count_sentences(prose_only) == 3

    with_list = (
        "<b>Sarlavha</b>\n\nBirinchi gap. Ikkinchi gap.\n\nQuyidagilar bor:\n"
        "– birinchi band;\n– ikkinchi band;\n– uchinchi band.\n\nYakuniy gap."
    )
    # lead (2) + intro (1) + the whole list (1) + closing (1)
    assert post_format.count_sentences(with_list) == 5


def test_two_separate_lists_each_count_once():
    """The run is the unit, so a second list after prose is charged again."""
    html = "Kirish.\n– a;\n– b.\n\nOraliq gap.\n– c;\n– d."
    assert post_format.count_sentences(html) == 4


def test_a_three_item_list_post_of_normal_length_renders():
    """The end-to-end case the previous behaviour discarded."""
    data = plain_data()
    data["body_1_uz"] = (
        "Lekin bu usulning o'z chegarasi bor:\n"
        "– qisqa matnlarda belgi qolmaydi;\n"
        "– faqat xato tuzatilgan bo'lsa, payqash qiyin;\n"
        "– matnni kim yozdirganini aniqlab bo'lmaydi."
    )
    rendered = post_format.render_dayjest_post(data)
    assert rendered.count("\n– ") == 3
    assert post_format.telegram_length(rendered) <= 900
