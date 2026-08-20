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


def test_count_sentences():
    """count_sentences counts sentences across paragraphs excluding hashtags."""
    post = "Birinchi gap. Ikkinchi gap.\n\nUchinchi gap!\n\n#modellar"
    assert post_format.count_sentences(post) == 3


def test_trim_post_fields_within_budget():
    """trim_post_fields preserves all text when within budget."""
    lead_html = (
        'EHang kompaniyasi uchar taksi xizmatini <a href="https://example.com">boshladi</a>.'
    )
    body_1 = "Parvoz 20 daqiqa davom etadi va 800 yuan turadi."
    body_2 = "Sertifikatlar to'liq olingan."
    tag = "#robototexnika"

    b1, b2 = post_format.trim_post_fields(lead_html, body_1, body_2, tag, max_chars=900)
    assert b1 == body_1
    assert b2 == body_2


def test_trim_post_fields_drops_body_2_first_when_overbudget():
    """When over budget, body_2 sentences are dropped first, then body_1."""
    lead_html = 'EHang kompaniyasi <a href="https://example.com">boshladi</a>.'
    body_1 = "Birinchi muhim fakt. Ikkinchi fakt."
    body_2 = "Ortiqcha uchinchi fakt. Ortiqcha to'rtinchi fakt."
    tag = "#robototexnika"

    b1, b2 = post_format.trim_post_fields(lead_html, body_1, body_2, tag, max_chars=80)
    assert b2 == ""
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


def test_render_item_post_v2_end_to_end():
    """render_item_post_v2 produces conforming HTML."""
    item_data = {
        "url": "https://example.com/ehang-evtol",
        "lead_uz": "EHang kompaniyasi yo'lovchi uchar taksi xizmatini yo'lga qo'ymoqda.",
        "body_1_uz": "Parvoz 20 daqiqa davom etadi va bir o'rindiq 800 yuan turadi.",
        "body_2_uz": "Xitoy aviatsiya regulyatori to'liq sertifikat bergan.",
        "topic": Topic.ROBOTICS,
    }
    rendered = post_format.render_item_post_v2(item_data, max_chars=900)

    assert '<a href="https://example.com/ehang-evtol">qo&#x27;ymoqda</a>' in rendered
    assert "800 yuan" in rendered
    assert "#robototexnika" in rendered
    assert rendered.endswith("#robototexnika")
    assert post_format.visible_length(rendered) <= 900


def test_render_item_post_v2_stays_within_the_sentence_budget():
    """The budget is sentences now. The anchor pairs the light verb: 'qayd etildi'."""
    item_data = {
        "url": "https://example.com/customs",
        "lead_uz": ("Rossiya va Gruziya chegarasida yangi rekord qayd etildi."),
        "body_1_uz": "18-avgust kuni chegaradan 20 ming kishi o'tgan.",
        "topic": Topic.FINTECH,
    }
    rendered = post_format.render_item_post_v2(item_data, max_chars=450, max_sentences=4)
    assert '<a href="https://example.com/customs">qayd etildi</a>' in rendered
    assert "#fintex" in rendered
    assert post_format.count_sentences(rendered) <= 4
    assert post_format.validate_rendered_post(rendered, max_chars=450) == []


# --- v3: positional anchor and sentence budget --------------------------------


@pytest.mark.parametrize(
    "lead,expected",
    [
        ("Modular Mojo kompilyatorini ochiq kodga chiqardi.", "chiqardi"),
        ("Alibaba yangi Qwen modelini taqdim etdi.", "taqdim etdi"),
        ("Mojo tili Apache 2.0 ostida open-source qildi.", "open-source qildi"),
        ("Yangi ochiq vaznli model taqdim etildi.", "taqdim etildi"),
        ("Roboflow Playground ishga tushirdi. Keyin narxni oshirdi.", "tushirdi"),
        ("Google TurboQuant algoritmini chiqardi!", "chiqardi"),
        ("Chiqardi.", "Chiqardi"),
        ("Loyiha to'liq ochiq manba bo'ldi.", "manba bo'ldi"),
        ("Loyiha to’liq ochiq manba bo’ldi.", "manba bo’ldi"),
    ],
)
def test_anchor_from_lead(lead, expected):
    assert post_format.anchor_from_lead(lead) == expected


def test_anchor_from_lead_is_empty_for_empty_input():
    assert post_format.anchor_from_lead("") == ""
    assert post_format.anchor_from_lead("   ") == ""


def test_anchor_from_lead_never_fails_on_real_shapes():
    for lead in (
        "openleetcode — bu Haskell tilida yozilgan test tushiruvchisi.",
        "Mojo tili endi to'liq open source qilingan, shu bilan birga ham",
        "Tadqiqotchilar agent framework'ini 14,560 ta holatda foydalanganlar.",
    ):
        assert post_format.anchor_from_lead(lead) != ""


def test_linkify_lead_accepts_a_two_word_anchor():
    lead = "Alibaba yangi Qwen modelini taqdim etdi."
    out = post_format.linkify_lead(lead, "https://example.com/q", "taqdim etdi")
    assert '<a href="https://example.com/q">taqdim etdi</a>' in out
    assert out.endswith(".")


def test_linkify_lead_no_longer_requires_an_approved_verb():
    lead = "Bu yangi tushiruvchisi."
    out = post_format.linkify_lead(lead, "https://example.com/x", "tushiruvchisi")
    assert '<a href="https://example.com/x">tushiruvchisi</a>' in out


def test_the_verb_machinery_is_gone():
    for name in (
        "BANNED_ANCHOR_TOKENS",
        "KNOWN_ACTION_VERBS",
        "is_valid_action_verb",
        "resolve_anchor",
        "count_words",
    ):
        assert not hasattr(post_format, name), f"{name} should have been deleted"


def test_validate_rejects_an_anchor_that_does_not_end_the_lead():
    html = (
        'Modular <a href="https://example.com/m">chiqardi</a> yangi kompilyatorni.'
        "\n\nIkkinchi gap shu yerda."
        "\n\nQisqa yakun."
        "\n\n#infratuzilma"
    )
    violations = post_format.validate_rendered_post(html, max_chars=450)
    assert any("end the lead" in v for v in violations)


def test_validate_accepts_an_anchor_at_the_end_of_the_lead():
    html = (
        'Modular yangi kompilyatorni <a href="https://example.com/m">chiqardi</a>.'
        "\n\nIkkinchi gap shu yerda."
        "\n\nQisqa yakun."
        "\n\n#infratuzilma"
    )
    assert post_format.validate_rendered_post(html, max_chars=450) == []


def test_count_sentences_ignores_the_hashtag_line():
    html = (
        'Model <a href="https://e.com/x">chiqardi</a>.'
        "\n\nIkkinchi gap."
        "\n\nUchinchi gap."
        "\n\n#modellar"
    )
    assert post_format.count_sentences(html) == 3


V3_DATA = {
    "url": "https://example.com/a",
    "topic": "frontier_models",
    "lead_uz": "Alibaba yangi Qwen modelini taqdim etdi.",
    "body_1_uz": "Model 52 ball to'pladi.",
    "body_2_uz": "Litsenziya tijoriy foydalanishga ruxsat beradi.",
}


def test_render_drops_body_2_first_when_over_the_sentence_budget():
    out = post_format.render_item_post_v2(dict(V3_DATA), max_chars=450, max_sentences=2)
    assert "Litsenziya" not in out
    assert post_format.count_sentences(out) == 2


def test_render_keeps_all_three_sentences_within_budget():
    out = post_format.render_item_post_v2(dict(V3_DATA), max_chars=450, max_sentences=3)
    assert post_format.count_sentences(out) == 3
    assert "Litsenziya" in out
    assert '<a href="https://example.com/a">taqdim etdi</a>' in out


@pytest.mark.parametrize(
    "lead_uz,body_1_uz",
    [
        ("Alibaba yangi Qwen modelini taqdim etdi.", "Model 52 ball to'pladi."),
        ("Modular kompilyatorni ochiq kodga chiqardi.", "Til 1.0 ga yetdi."),
        ("Mojo tili Apache 2.0 ostida open-source qildi.", "GPU uchun 2x tez."),
        ("openleetcode mahalliy runner sifatida chiqdi.", "14,560 ta test bor."),
    ],
)
def test_every_lead_shape_renders(lead_uz, body_1_uz):
    data = {
        "url": "https://example.com/a",
        "topic": "frontier_models",
        "lead_uz": lead_uz,
        "body_1_uz": body_1_uz,
        "body_2_uz": "",
    }
    out = post_format.render_item_post_v2(data, max_chars=450, max_sentences=4)
    assert post_format.validate_rendered_post(out, max_chars=450) == []
    assert out.count("<a href=") == 1
