"""Pure post formatting for new dayjest posts and legacy three-sentence posts.

HTML and source-backed links are assembled in code. New dayjests preserve paragraphs
and feature lists; the legacy renderer remains for already stored editorial rows.
"""

import logging
import re
from html import escape as html_escape
from html import unescape as html_unescape
from urllib.parse import urlparse

from .models import Topic

log = logging.getLogger(__name__)

PLAIN_PHOTO_STYLE = "plain_photo_v1"
#: Kept as a set: `post_style` is matched against it in three modules, and a second
#: style would be added here rather than by widening three equality checks.
DAYJEST_STYLES = {PLAIN_PHOTO_STYLE}
#: A Telegram photo caption holds 1024 characters. These are the hard ceiling; the
#: DAYJEST_MAX_* settings may tighten them and cannot raise them.
DAYJEST_MAX_CHARS = 900
DAYJEST_MAX_SENTENCES = 7

TOPIC_TAGS: dict[Topic, str] = {
    Topic.FRONTIER_MODELS: "#modellar",
    Topic.AI_AGENTS: "#agentlar",
    Topic.NEW_APPROACHES: "#tadqiqot",
    Topic.SPEECH_VOICE: "#nutq",
    Topic.ROBOTICS: "#robototexnika",
    Topic.FINTECH: "#fintex",
    Topic.GOVTECH: "#davlat",
    Topic.PRODUCTION_ENGINEERING: "#infratuzilma",
    Topic.STARTUPS: "#startap",
    Topic.TECHNICAL_TALKS: "#suhbat",
    Topic.SAFETY_SECURITY: "#xavfsizlik",
}

# Verify invariant: every Topic except IRRELEVANT must have an approved tag
assert set(TOPIC_TAGS.keys()) == set(Topic) - {Topic.IRRELEVANT}, (
    "TOPIC_TAGS must cover every Topic except IRRELEVANT"
)


def get_topic_tag(topic: Topic | str | None) -> str:
    """Return the approved lowercase hashtag for a Topic."""
    if isinstance(topic, str):
        try:
            topic = Topic(topic)
        except ValueError as err:
            raise ValueError(f"Unknown topic: {topic}") from err

    if topic is None or topic == Topic.IRRELEVANT:
        raise ValueError(f"Topic {topic} is irrelevant or untagged")

    if topic not in TOPIC_TAGS:
        raise ValueError(f"No approved tag configured for topic {topic}")

    return TOPIC_TAGS[topic]


def strip_markdown_formatting(text: str) -> str:
    """Strip markdown bolding, italics, and backticks from text."""
    if not text or not isinstance(text, str):
        return ""
    # Strip bolding **word** or __word__
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    cleaned = re.sub(r"__([^_]+)__", r"\1", cleaned)
    # Strip backticks
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    return cleaned


_FIRST_SENTENCE_RE = re.compile(r"^(.*?)(?:[.!?](?:\s+|$))", re.DOTALL)
_CLEAN_TOKEN_RE = re.compile(r"^[.,!?:;\"'()«»“”’`]+|[.,!?:;\"'()«»“”’`]+$")
_WORD_CHAR_PATTERN = r"[a-zA-Z0-9_ʻ‘’'`]"


def clean_token(token: str) -> str:
    """Strip leading and trailing punctuation from a token."""
    return _CLEAN_TOKEN_RE.sub("", token.strip())


def split_first_sentence(text: str) -> tuple[str, str, str]:
    """Extract first sentence components (inner text, full match with punct, rest of text).

    Handles abbreviations (such as U.S., e.g., i.e., Inc., Dr., AQSH) by checking whether
    the preceding token is an abbreviation before splitting at punctuation followed by whitespace.
    """
    if not text or not text.strip():
        return ("", "", "")

    clean = text.strip()
    matches = list(re.finditer(r"([.!?]+)\s+(?=[A-ZА-ЯЁOʻGʻQKh\"«])", clean))

    for m in matches:
        punct_end = m.end(1)
        prefix = clean[:punct_end]
        words = prefix.split()
        if not words:
            continue
        last_word = words[-1].rstrip(".!?")
        if (
            re.match(r"^[A-Za-z](\.[A-Za-z])+$", last_word)
            or len(last_word) == 1
            or last_word.lower()
            in ("inc", "ltd", "corp", "dr", "mr", "mrs", "ms", "vs", "e.g", "i.e", "u.s", "aqsh")
        ):
            continue

        first_sent_full = clean[: m.end()].rstrip()
        rest = clean[m.end() :].lstrip()
        inner_text = re.sub(r"[.!?]+$", "", prefix).strip()
        return (inner_text, first_sent_full, rest)

    inner_text = re.sub(r"[.!?]+$", "", clean).strip()
    return (inner_text, clean, "")


#: Uzbek light verbs that carry no meaning on their own. A lead ending in one of these
#: gives up its preceding word to the anchor, so the link reads "taqdim etdi" rather than
#: the bare auxiliary "etdi". Measured on 28 stored leads: 32% end this way.
AUXILIARY_TAILS: frozenset[str] = frozenset(
    {
        "etdi",
        "etadi",
        "etildi",
        "etiladi",
        "qildi",
        "qiladi",
        "qilindi",
        "qilinadi",
        "bo'ldi",
        "bo'ladi",
        "oldi",
        "oladi",
        "berdi",
        "beradi",
    }
)


def normalise_apostrophes(token: str) -> str:
    """Fold every apostrophe variant real data uses onto the plain ASCII one."""
    for variant in ("’", "‘", "ʻ", "`"):
        token = token.replace(variant, "'")
    return token


def anchor_from_lead(lead: str) -> str:
    """The link anchor: the tail of the lead's first sentence.

    Uzbek is SOV, so the predicate already lands last. The anchor therefore needs no
    model input, no verb list and no company-name blocklist - only position. This cannot
    fail to find an anchor for a non-empty lead, which is why there is no fallback path.
    """
    first_sentence, _, _ = split_first_sentence(lead or "")
    tokens = [clean_token(t) for t in first_sentence.split() if clean_token(t)]
    if not tokens:
        return ""
    if len(tokens) > 1 and normalise_apostrophes(tokens[-1].lower()) in AUXILIARY_TAILS:
        return " ".join(tokens[-2:])
    return tokens[-1]


def linkify_lead(lead: str, url: str, anchor: str = "") -> str:
    """Insert exactly one <a href="..."> inside the first sentence of lead on anchor.

    The anchor is positional (see `anchor_from_lead`), so an empty argument is normal:
    it is derived here. Uses boundary-aware matching so linking 'etdi' does not also
    match inside 'ketdi'.
    """
    if not lead or not lead.strip():
        raise ValueError("Cannot linkify empty lead")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid URL scheme or missing host: {url}")
    if parsed.hostname in {"t.me", "telegram.me"} and parsed.path.rstrip("/") == "/iv":
        raise ValueError("Instant View links are not allowed; use the original article URL")

    escaped_url = html_escape(url, quote=True)
    first_sent_text, first_sent_full, rest = split_first_sentence(lead)

    clean_anch = clean_token(anchor) if anchor else ""
    if not clean_anch:
        clean_anch = anchor_from_lead(lead)

    if not clean_anch:
        raise ValueError(f"Could not determine link anchor from sentence: '{first_sent_text}'")

    def _find(needle: str):
        pattern = re.compile(
            rf"(?<!{_WORD_CHAR_PATTERN})({re.escape(needle)})(?!{_WORD_CHAR_PATTERN})",
            re.IGNORECASE,
        )
        return pattern.search(first_sent_text)

    match_anch = _find(clean_anch)
    if not match_anch:
        # A caller-supplied anchor that is not in the sentence falls back to the derived
        # one. The pattern must be rebuilt for the new needle, not merely re-searched.
        derived = anchor_from_lead(lead)
        match_anch = _find(derived) if derived and derived != clean_anch else None
        if not match_anch:
            raise ValueError(f"Anchor '{clean_anch}' not found in sentence: '{first_sent_text}'")
        clean_anch = derived

    start, end = match_anch.span(1)
    matched_word = first_sent_text[start:end]

    linked_first_text = (
        html_escape(first_sent_text[:start])
        + f'<a href="{escaped_url}">{html_escape(matched_word)}</a>'
        + html_escape(first_sent_text[end:])
    )

    punct_suffix = first_sent_full[len(first_sent_text) :]
    space_after = " " if rest else ""
    return linked_first_text + html_escape(punct_suffix) + space_after + html_escape(rest)


_TAG_STRIP_RE = re.compile(r"<[^>]+>")
#: A headline line is exactly one <b>...</b> and nothing else. Bold is positional (the
#: renderer applies it, the model never does), so this pattern is both the way the line is
#: written and the way it is recognised.
_HEADLINE_LINE_RE = re.compile(r"^<b>.*</b>$", re.DOTALL)


def visible_length(html_text: str) -> int:
    """Calculate character count of text as seen by readers (tags stripped, entities unescaped)."""
    plain = _TAG_STRIP_RE.sub("", html_text)
    return len(html_unescape(plain))


def telegram_length(html_text: str) -> int:
    """Conservative UTF-16 length after entity parsing, including emoji surrogate pairs."""
    plain = html_unescape(_TAG_STRIP_RE.sub("", html_text))
    return len(plain.encode("utf-16-le")) // 2


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁO‘OʻG‘Gʻ\d\W])")


def split_sentences(text: str) -> list[str]:
    """Split text into sentences while preserving decimal numbers and abbreviations."""
    if not text or not text.strip():
        return []
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p and p.strip()]


def count_sentences(html_text: str) -> int:
    """Sentences a reader sees. The headline and hashtag lines are labels, not sentences.

    Sentences, not words: Uzbek folds prepositions into suffixes, so a word count that
    reads correctly in one language is meaningless in the other. A sentence carries
    roughly one fact in both.
    """
    lines = [line.strip() for line in html_text.strip().splitlines() if line.strip()]
    total = 0
    in_list = False
    for index, line in enumerate(lines):
        if index == 0 and _HEADLINE_LINE_RE.match(line):
            continue
        plain = html_unescape(_TAG_STRIP_RE.sub("", line)).strip()
        if not plain or plain.startswith("#"):
            continue
        # A run of bullets is one unit, not one per item. The item count is already
        # bounded on its own (2-3 for a photo caption), so charging each bullet against
        # the sentence budget bounded the same thing twice. Measured 2026-09-08: a
        # 647-character post -- well inside the 900 limit -- was discarded for "10
        # sentences" because a three-item list, its intro line, the lead and the kicker
        # added up past seven. The budget exists to stop rambling prose; the character
        # limit is what bounds a list.
        if plain.startswith("– "):
            if not in_list:
                total += 1
                in_list = True
            continue
        in_list = False
        total += len(split_sentences(plain))
    return total


def _assemble_candidate(
    headline_html: str, lead_html: str, body_1: str, kicker: str, tag: str
) -> str:
    parts = []
    if headline_html.strip():
        parts.append(headline_html.strip())
    parts.append(lead_html.strip())
    if body_1.strip():
        parts.append(html_escape(body_1.strip()))
    if kicker.strip():
        parts.append(html_escape(kicker.strip()))
    if tag.strip():
        parts.append(tag.strip())
    return "\n\n".join(parts)


def trim_post_fields(
    headline_html: str,
    lead_html: str,
    body_1: str,
    kicker: str,
    tag: str,
    max_chars: int = 500,
    max_sentences: int = 3,
) -> str:
    """Trim `body_1` until the post is in budget, and return what survived.

    `body_2` was removed on 2026-08-24. It was always the first thing trimmed, and the model
    could not reliably tell it apart from `body_1` — in the live test its "cause or context"
    sentence turned up in `body_1` instead. Generating a field to discard it costs output
    tokens on two calls and buys nothing.

    The headline and the kicker are never trimmed. They are the two elements the reference
    channel always carries and this channel had lost, so dropping them under pressure would
    reproduce the exact defect this format corrects.
    """

    def fits(b1: str) -> bool:
        candidate = _assemble_candidate(headline_html, lead_html, b1, kicker, tag)
        return (
            visible_length(candidate) <= max_chars and count_sentences(candidate) <= max_sentences
        )

    if fits(body_1):
        return body_1

    sentences = split_sentences(body_1)
    while len(sentences) > 1:
        sentences.pop()
        if fits(" ".join(sentences)):
            return " ".join(sentences)

    body_1 = " ".join(sentences)
    if fits(body_1):
        return body_1
    if fits(""):
        return ""

    vis = visible_length(_assemble_candidate(headline_html, lead_html, "", kicker, tag))
    raise ValueError(
        f"Headline, lead, kicker and tag alone exceed max_chars budget ({vis} > {max_chars})"
    )


_FORBIDDEN_TAGS_RE = re.compile(r"<(?!/?a(?:\s+[^>]*)?>|/?b>)[^>]+>", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[•\*\-]\s+", re.MULTILINE)
_A_TAG_RE = re.compile(r'<a\s+href="[^"]+">.*?</a>', re.DOTALL | re.IGNORECASE)


def validate_rendered_post(
    rendered_html: str, max_chars: int = 500, max_sentences: int = 3
) -> list[str]:
    """Validate rendered HTML satisfies all post contract constraints."""
    violations = []

    # Exactly one <a> tag
    a_matches = _A_TAG_RE.findall(rendered_html)
    if len(a_matches) != 1:
        violations.append(f"Post must contain exactly one <a> tag, found {len(a_matches)}")
    else:
        # The anchor is positional now: it must close the lead. Anything other than
        # sentence punctuation after </a> means the link is not on the tail.
        blocks = rendered_html.split("\n\n")
        lead_index = 1 if blocks and _HEADLINE_LINE_RE.match(blocks[0].strip()) else 0
        first_block = blocks[lead_index] if len(blocks) > lead_index else ""
        tail_match = re.search(r"</a>(.*)$", first_block, re.DOTALL)
        if tail_match is None:
            violations.append("The <a> tag must be inside the lead (the first block)")
        else:
            trailing = html_unescape(_TAG_STRIP_RE.sub("", tail_match.group(1)))
            if trailing.strip(" .!?"):
                violations.append(
                    f"Anchor must end the lead sentence; text follows it: '{trailing.strip()}'"
                )

    # No forbidden tags (<b>, <i>, <code>, <blockquote>, etc.)
    forbidden = _FORBIDDEN_TAGS_RE.findall(rendered_html)
    if forbidden:
        violations.append(f"Forbidden HTML tags found: {', '.join(set(forbidden))}")

    lines = [line.strip() for line in rendered_html.strip().splitlines() if line.strip()]
    bold_lines = [i for i, line in enumerate(lines) if "<b>" in line]
    if bold_lines and bold_lines != [0]:
        violations.append("Bold is allowed only on the headline line")
    elif bold_lines == [0] and not _HEADLINE_LINE_RE.match(lines[0]):
        violations.append("The headline line must be exactly <b>...</b>")

    # No bullet formatting
    if _BULLET_RE.search(rendered_html):
        violations.append("Bullet points or markdown lists found in post")

    # Final line must be exactly one approved topic hashtag
    if not lines:
        violations.append("Post is empty")
    else:
        last_line = lines[-1]
        approved_tags = set(TOPIC_TAGS.values())
        if last_line not in approved_tags:
            tag_list = ", ".join(sorted(approved_tags))
            violations.append(
                f"Final line must be an approved hashtag ({tag_list}), got '{last_line}'"
            )

    # Check visible character length
    vis_len = visible_length(rendered_html)
    if vis_len > max_chars:
        violations.append(f"Visible length {vis_len} exceeds max budget of {max_chars} characters")

    # Check sentence count
    num_sentences = count_sentences(rendered_html)
    if num_sentences > max_sentences:
        violations.append(
            f"Post exceeds sentence limit ({num_sentences} > {max_sentences} sentences)"
        )

    return violations


def render_item_post_v2(item_data: dict, max_chars: int = 500, max_sentences: int = 3) -> str:
    """Render a post: headline, lead, body_1, kicker, one hashtag."""
    url = item_data.get("url", "")
    lead = strip_markdown_formatting(item_data.get("lead_uz") or item_data.get("summary_uz") or "")
    if not lead.strip():
        raise ValueError("Cannot render post: lead_uz / summary_uz is empty")

    anchor = anchor_from_lead(lead)
    lead_html = linkify_lead(lead, url, anchor)

    # Bold is applied here and nowhere else. Stored payloads from before v3 carry no
    # headline_uz; `_item_data` falls back to the article title, which always exists.
    headline = strip_markdown_formatting(item_data.get("headline_uz") or "").strip()
    headline_html = f"<b>{html_escape(headline)}</b>" if headline else ""

    body_1 = strip_markdown_formatting(item_data.get("body_1_uz") or "")
    # Payloads stored before v3 have no kicker. They render without one rather than failing.
    kicker = strip_markdown_formatting(item_data.get("kicker_uz") or "")

    topic = item_data.get("topic") or item_data.get("primary_topic")
    tag = get_topic_tag(topic)

    b1_trimmed = trim_post_fields(
        headline_html=headline_html,
        lead_html=lead_html,
        body_1=body_1,
        kicker=kicker,
        tag=tag,
        max_chars=max_chars,
        max_sentences=max_sentences,
    )

    rendered = _assemble_candidate(headline_html, lead_html, b1_trimmed, kicker, tag)

    violations = validate_rendered_post(rendered, max_chars=max_chars, max_sentences=max_sentences)
    if violations:
        raise ValueError(f"Rendered post contract violation: {'; '.join(violations)}")

    return rendered


_SOURCE_URL_RE = re.compile(r"https?://[^\s<>\"']+")

#: Bullet markers the model actually emits, folded onto one. The class is written as a
#: range from U+2010 to U+2015 so every dash Unicode offers is covered: the em dash was
#: missing before, and an em-dash list slipped past the item-count check entirely.
_BULLET_MARKER_RE = re.compile(r"(?m)^[^\S\r\n]*[•*‐-―\-][^\S\r\n]+")
_NORMALISED_BULLET_RE = re.compile(r"(?m)^– ")


def render_dayjest_post(
    item_data: dict,
    max_chars: int = DAYJEST_MAX_CHARS,
    max_sentences: int = DAYJEST_MAX_SENTENCES,
) -> str:
    """Render a plain-photo post without silently removing facts or access restrictions.

    One style, because one style exists. This renderer shipped with a second `dayjest_v1`
    shape - a 14-word headline, 3-5 bullets, a two-link footer, a topic hashtag, a
    4096-character ceiling - that nothing ever wrote: both producers in `llm.py` stamp
    `plain_photo_v1` unconditionally, and the field is new, so no stored row carries the
    other value either. Five of this renderer's tests exercised that dead half while the
    half that runs had three, and the model was asked for `links_uz` in the schema only
    for the renderer to refuse any non-empty value - a field generated to be discarded.

    The caller still passes `max_chars`/`max_sentences`, so the operator's settings can
    tighten the post; they cannot loosen it past what a Telegram photo caption holds.
    """
    fields = {
        field: strip_markdown_formatting(item_data.get(field) or "").strip()
        for field in ("lead_uz", "body_1_uz", "kicker_uz")
    }
    lead, body, kicker = fields.values()
    max_chars = min(max_chars, DAYJEST_MAX_CHARS)
    max_sentences = min(max_sentences, DAYJEST_MAX_SENTENCES)

    # No headline, and no bold anywhere. Read against 63 technology posts on the channel
    # this format is modelled on (2026-09-08): none carries a separate headline; the first
    # paragraph is the hook and the news in one. A `headline_uz` a stored row still has is
    # a legacy field and is ignored here; render_item_post_v2 keeps rendering it for the
    # rows that were written with one.
    if not lead:
        raise ValueError("Cannot render dayjest: lead_uz is empty")
    if "\n" in lead or len(split_sentences(lead)) > 2:
        raise ValueError("Dayjest lead must be one paragraph, at most 2 sentences")
    if "\n" in kicker or len(split_sentences(kicker)) > 1:
        raise ValueError("Dayjest kicker must be one paragraph, at most 1 sentence")
    for value in fields.values():
        if _SOURCE_URL_RE.search(value):
            raise ValueError("Reader-facing URLs are not allowed; the source is linked in the lead")

    # Standardise bullet markers, keeping the paragraph breaks and every word of every
    # item. The leading class is horizontal whitespace only: `\s*` matches newlines, and
    # in MULTILINE `^` also matches at the start of a blank line, so the match began on
    # the blank separator and ate it. A prose paragraph opening with a dash was welded
    # onto the paragraph above and counted as a one-item list, which fails the band below
    # and discarded the article; a genuine list silently lost the blank line before it.
    body = _BULLET_MARKER_RE.sub("– ", body)
    bullets = _NORMALISED_BULLET_RE.findall(body)
    # A single marked line is not a list; it is one sentence the model happened to
    # bullet. Unmarking it is the same mechanical normalisation strip_markdown_formatting
    # performs, and it costs nothing. Raising discarded a whole article over a dash.
    if len(bullets) == 1:
        body = _NORMALISED_BULLET_RE.sub("", body)
        bullets = []
    if bullets and not 2 <= len(bullets) <= 3:
        raise ValueError("Dayjest lists must contain 2-3 items")
    if not bullets and len([p for p in body.split("\n\n") if p.strip()]) > 3:
        raise ValueError("Dayjest body must contain at most 3 paragraphs")

    parts = [linkify_lead(lead, item_data.get("url", ""))]
    if body:
        parts.append(html_escape(body))
    if kicker:
        parts.append(html_escape(kicker))

    rendered = "\n\n".join(parts)
    if count_sentences(rendered) > max_sentences:
        raise ValueError(f"Dayjest exceeds {max_sentences} sentences/list items")
    if telegram_length(rendered) > max_chars:
        raise ValueError(
            f"Dayjest exceeds {max_chars} characters; shorten without losing qualifiers"
        )
    return rendered
