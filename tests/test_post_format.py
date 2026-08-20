"""Tests for pure post-format v2 layer (apps/digest/post_format.py)."""

import pytest

from apps.digest import post_format
from apps.digest.models import Topic


def test_topic_tags_covers_all_topics_except_irrelevant():
    """TOPIC_TAGS invariant: every Topic except IRRELEVANT must have a unique, lowercase hashtag."""
    assert set(post_format.TOPIC_TAGS.keys()) == set(Topic) - {Topic.IRRELEVANT}
    for tag in post_format.TOPIC_TAGS.values():
        assert tag.startswith("#")
        assert tag == tag.lower()
        assert " " not in tag


def test_get_topic_tag_valid_and_invalid():
    """get_topic_tag returns hashtag for valid topics and raises on invalid/irrelevant."""
    assert post_format.get_topic_tag(Topic.ROBOTICS) == "#robototexnika"
    assert post_format.get_topic_tag("robotics") == "#robototexnika"
    assert post_format.get_topic_tag(Topic.AI_AGENTS) == "#agentlar"

    with pytest.raises(ValueError, match="irrelevant or untagged"):
        post_format.get_topic_tag(Topic.IRRELEVANT)

    with pytest.raises(ValueError, match="Unknown topic"):
        post_format.get_topic_tag("non_existent_topic")


def test_is_valid_action_verb():
    """is_valid_action_verb accepts real action verbs and rejects multiwords, sources, and nouns."""
    assert post_format.is_valid_action_verb("chiqardi") is True
    assert post_format.is_valid_action_verb("boshladi") is True
    assert post_format.is_valid_action_verb("etdi") is True
    assert post_format.is_valid_action_verb("qo'ymoqda") is True
    assert post_format.is_valid_action_verb("tushirdi.") is True  # punctuation stripped

    # Rejects multiwords
    assert post_format.is_valid_action_verb("yo'lga qo'ymoqda") is False
    assert post_format.is_valid_action_verb("taqdim etdi") is False

    # Rejects sources and domains
    assert post_format.is_valid_action_verb("github.com") is False
    assert post_format.is_valid_action_verb("Nextgov") is False
    assert post_format.is_valid_action_verb("Wiz") is False
    assert post_format.is_valid_action_verb("U.S") is False

    # Rejects common nouns
    assert post_format.is_valid_action_verb("model") is False
    assert post_format.is_valid_action_verb("agent") is False
    assert post_format.is_valid_action_verb("tizim") is False


def test_resolve_anchor_exact_match():
    """When requested_anchor is a valid single-word verb in sentence one, use it."""
    lead = "EHang kompaniyasi uchar taksi xizmatini yo'lga qo'ymoqda. Parvoz 20 daqiqa."
    anchor = post_format.resolve_anchor(lead, "qo'ymoqda")
    assert anchor == "qo'ymoqda"


def test_resolve_anchor_rejects_multiword_and_selects_verb():
    """When requested_anchor is multiword, resolve_anchor falls back to first sentence verb."""
    lead = "EHang kompaniyasi uchar taksi xizmatini yo'lga qo'ymoqda. Parvoz 20 daqiqa."
    anchor = post_format.resolve_anchor(lead, "yo'lga qo'ymoqda")
    assert anchor == "qo'ymoqda"


def test_resolve_anchor_rejects_source_name_and_selects_verb():
    """When requested_anchor is a source/domain name, resolve_anchor ignores it and finds verb."""
    lead = "Nextgov ma'lumotlariga ko'ra agentlik yangi grant berdi. Bu uch yillik loyiha."
    anchor = post_format.resolve_anchor(lead, "Nextgov")
    assert anchor == "berdi"


def test_resolve_anchor_returns_empty_when_no_verb_in_first_sentence():
    """When the first sentence has no action verb, resolve_anchor returns empty string."""
    lead = "Yangi model va yangi dasturiy ta'minot haqida hisobot. U yaxshi ishlaydi."
    anchor = post_format.resolve_anchor(lead, "hisobot")
    assert anchor == ""


def test_linkify_lead_boundary_etdi_vs_ketdi():
    """etdi vs ketdi: 'etdi' must NOT link inside 'ketdi'."""
    lead = "Qwen dasturchilar jamoasi ketdi va yangi model taqdim etdi. Muhim natijalar."
    url = "https://example.com/qwen"
    anchor = "etdi"

    linked = post_format.linkify_lead(lead, url, anchor)
    assert "ketdi" in linked
    assert '<a href="https://example.com/qwen">etdi</a>' in linked
    assert '<a href="https://example.com/qwen">ketdi</a>' not in linked
    assert 'k<a href="https://example.com/qwen">etdi</a>' not in linked


def test_linkify_lead_punctuation_adjacent():
    """Punctuation adjacent to anchor must remain outside the <a> tag."""
    lead = "Zed AI yangi muhitni ishga tushirdi. Keyingi tafsilotlar."
    url = "https://example.com/zed"
    anchor = "tushirdi"

    linked = post_format.linkify_lead(lead, url, anchor)
    assert 'ishga <a href="https://example.com/zed">tushirdi</a>.' in linked


def test_linkify_lead_repeated_same_token():
    """Repeated same token links only the intended first occurrence in sentence one."""
    lead = "Model tez ishlaydi va juda samarali ishlaydi. Keyingi fakt."
    url = "https://example.com/model"
    anchor = "ishlaydi"

    linked = post_format.linkify_lead(lead, url, anchor)
    assert linked.count("<a ") == 1
    assert linked.count("</a>") == 1
    # First occurrence linked, second occurrence unlinked
    expected = 'tez <a href="https://example.com/model">ishlaydi</a> va juda samarali ishlaydi.'
    assert expected in linked


def test_linkify_lead_rejects_unsafe_schemes():
    """linkify_lead rejects javascript:, data:, and empty schemes."""
    lead = "Test lead sentence boshladi."
    with pytest.raises(ValueError, match="Invalid URL scheme"):
        post_format.linkify_lead(lead, "javascript:alert(1)", "boshladi")

    with pytest.raises(ValueError, match="Invalid URL scheme"):
        post_format.linkify_lead(lead, "ftp://example.com/file", "boshladi")


def test_strip_markdown_formatting():
    """strip_markdown_formatting removes **, __, and ` markdown formatting."""
    assert post_format.strip_markdown_formatting("**bold text**") == "bold text"
    assert post_format.strip_markdown_formatting("__italic text__") == "italic text"
    assert post_format.strip_markdown_formatting("`code`") == "code"
    assert (
        post_format.strip_markdown_formatting("Normal **important** news.")
        == "Normal important news."
    )


def test_visible_length_strips_tags_and_unescapes_entities():
    """visible_length computes character count seen by readers."""
    html = 'Hello <a href="https://example.com">world</a> &amp; friends'
    assert post_format.visible_length(html) == len("Hello world & friends")


def test_trim_post_fields_within_budget():
    """trim_post_fields preserves all text when within budget."""
    lead_html = (
        'EHang kompaniyasi uchar taksi xizmatini <a href="https://example.com">boshladi</a>.'
    )
    body_1 = "Parvoz 20 daqiqa davom etadi va 800 yuan turadi."
    body_2 = "Sertifikatlar to'liq olingan."
    kicker = "Dunyo bo'yicha yagona xizmat."
    tag = "#robototexnika"

    b1, b2, k = post_format.trim_post_fields(lead_html, body_1, body_2, kicker, tag, max_chars=900)
    assert b1 == body_1
    assert b2 == body_2
    assert k == kicker


def test_trim_post_fields_drops_body_2_first_when_overbudget():
    """When over budget, body_2 sentences are dropped first, then body_1, then kicker."""
    lead_html = 'EHang kompaniyasi <a href="https://example.com">boshladi</a>.'
    body_1 = "Birinchi muhim fakt. Ikkinchi fakt."
    body_2 = "Ortiqcha uchinchi fakt. Ortiqcha to'rtinchi fakt."
    kicker = "Ixcham zarba."
    tag = "#robototexnika"

    b1, b2, k = post_format.trim_post_fields(lead_html, body_1, body_2, kicker, tag, max_chars=110)
    assert b2 == ""
    assert k == kicker
    assert "Birinchi muhim fakt" in b1


def test_validate_rendered_post_accepts_clean_post():
    """validate_rendered_post passes a conforming post."""
    post = (
        'EHang kompaniyasi uchar taksini <a href="https://example.com">boshladi</a>.\n\n'
        "Parvoz 20 daqiqa davom etadi.\n\n"
        "Dunyo bo'yicha yagona xizmat.\n\n"
        "#robototexnika"
    )
    violations = post_format.validate_rendered_post(post, max_chars=900)
    assert violations == []


def test_validate_rendered_post_rejects_forbidden_tags_and_multiple_links():
    """validate_rendered_post catches <b>, <i>, bullets, duplicate links, or invalid tags."""
    bad_post = (
        "<b>Sarlavha</b>\n\n"
        'EHang <a href="https://example.com">boshladi</a> '
        'va <a href="https://second.com">davom</a> etdi.\n\n'
        "• Birinchi punkt\n\n"
        "#not_a_valid_tag"
    )
    violations = post_format.validate_rendered_post(bad_post, max_chars=900)
    assert any("Forbidden HTML tags" in v for v in violations)
    assert any("exactly one <a>" in v for v in violations)
    assert any("Bullet points" in v for v in violations)
    assert any("approved hashtag" in v for v in violations)


def test_render_item_post_v2_fails_controlled_when_no_verb():
    """render_item_post_v2 raises ValueError when first sentence has no valid action verb."""
    item_data = {
        "url": "https://example.com/test",
        "lead_uz": "Yangi model va yangi dasturiy ta'minot haqida hisobot.",
        "link_anchor_uz": "",
        "body_1_uz": "Birinchi fakt.",
        "topic": Topic.ROBOTICS,
    }
    with pytest.raises(ValueError, match="Invalid or missing action verb anchor"):
        post_format.render_item_post_v2(item_data, max_chars=900)


def test_render_item_post_v2_end_to_end():
    """render_item_post_v2 produces conforming HTML."""
    item_data = {
        "url": "https://example.com/ehang-evtol",
        "lead_uz": "EHang kompaniyasi yo'lovchi uchar taksi xizmatini yo'lga qo'ymoqda.",
        "link_anchor_uz": "qo'ymoqda",
        "body_1_uz": "Parvoz 20 daqiqa davom etadi va bir o'rindiq 800 yuan turadi.",
        "body_2_uz": "Xitoy aviatsiya regulyatori to'liq sertifikat bergan.",
        "kicker_uz": "Uchuvchisiz parvozga chipta sotiladigan yagona joy.",
        "topic": Topic.ROBOTICS,
    }
    rendered = post_format.render_item_post_v2(item_data, max_chars=900)

    assert '<a href="https://example.com/ehang-evtol">qo&#x27;ymoqda</a>' in rendered
    assert "800 yuan" in rendered
    assert "#robototexnika" in rendered
    assert rendered.endswith("#robototexnika")
    assert post_format.visible_length(rendered) <= 900


def test_render_item_post_v2_ultra_concise_under_30_words():
    """render_item_post_v2 renders ultra-concise post with <= 30 words."""
    item_data = {
        "url": "https://t.me/customs_rf/12219",
        "lead_uz": (
            "Rossiya va Gruziya chegarasida yangi rekord qayd etildi: "
            "18-avgust kuni 20 ming kishi o'tdi."
        ),
        "link_anchor_uz": "etildi",
        "body_1_uz": "Yo'lovchilar oqimi keskin oshgan — bu o'tgan yillarga nisbatan ancha ko'p.",
        "topic": Topic.FINTECH,
    }
    rendered = post_format.render_item_post_v2(item_data, max_words=30)
    assert '<a href="https://t.me/customs_rf/12219">etildi</a>' in rendered
    assert "#fintex" in rendered
    assert post_format.count_words(rendered) <= 30
