"""Pure post-format layer (v2 redesign).

Renders clean Uzbek prose: safe photo/text caption with no headers, no bullets,
exactly one inline link on the lead verb, closed topic hashtag, and <=900 visible chars.
"""

import logging
import re
from html import escape as html_escape
from html import unescape as html_unescape
from urllib.parse import urlparse

from .models import Topic

log = logging.getLogger(__name__)

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


_FIRST_SENTENCE_RE = re.compile(r"^(.*?)(?:[.!?](?:\s+|$))", re.DOTALL)
_CLEAN_TOKEN_RE = re.compile(r"^[.,!?:;\"'()«»“”’`]+|[.,!?:;\"'()«»“”’`]+$")
_WORD_CHAR_PATTERN = r"[a-zA-Z0-9_ʻ‘’'`]"

# Common non-verb tokens (source names, domains, common nouns, pronouns, conjunctions, tech terms)
BANNED_ANCHOR_TOKENS: set[str] = {
    # Sources / Companies / Products
    "nextgov",
    "github",
    "zed",
    "wiz",
    "openai",
    "google",
    "meta",
    "anthropic",
    "apple",
    "microsoft",
    "amazon",
    "techcrunch",
    "huggingface",
    "gitlab",
    "snowflake",
    "jira",
    "docker",
    "redis",
    "postgres",
    "python",
    "javascript",
    "fedscoop",
    "statescoop",
    "arstechnica",
    "theverge",
    "venturebeat",
    "reuters",
    "bloomberg",
    "copilot",
    "chatgpt",
    "claude",
    "gemini",
    "llama",
    "deepseek",
    # Countries / Entities / Abbreviations
    "aqsh",
    "u.s",
    "u.s.",
    "us",
    "usa",
    "uzbekistan",
    "o'zbekiston",
    "xitoy",
    "rossiya",
    "evropa",
    # Technical nouns & Archetypes
    "model",
    "modellar",
    "modeli",
    "modelini",
    "agent",
    "agentlar",
    "agentlik",
    "dastur",
    "dasturiy",
    "tizim",
    "tizimi",
    "tizimini",
    "platforma",
    "platformasi",
    "kompaniya",
    "kompaniyasi",
    "loyiha",
    "tadqiqot",
    "tadqiqoti",
    "infratuzilma",
    "xavfsizlik",
    "fintex",
    "davlat",
    "startap",
    "suhbat",
    "nutq",
    "robototexnika",
    "arxiv",
    "paper",
    "maqola",
    "post",
    "blog",
    "yangilik",
    "kod",
    "server",
    "muhit",
    "baza",
    "kodlash",
    "xizmat",
    "xizmati",
    "xizmatini",
    "xatolik",
    "kamchilik",
    # Common function words (pronouns, conjunctions, adverbs, numbers)
    "bu",
    "shu",
    "ushbu",
    "ular",
    "ularning",
    "u",
    "biz",
    "siz",
    "men",
    "sen",
    "yangi",
    "katta",
    "kichik",
    "uchun",
    "bilan",
    "hamda",
    "ammo",
    "lekin",
    "biroq",
    "chunki",
    "barcha",
    "har",
    "bir",
    "ikki",
    "uch",
    "to'rt",
    "besh",
    "oltin",
    "yetti",
    "sakkiz",
    "to'qqiz",
    "o'n",
    "foiz",
    "dollar",
    "dollarli",
    "grant",
    "ega",
    "emas",
    "kerak",
    "mumkin",
    "lozim",
    "bo'yicha",
    "haqida",
    "orqali",
    "asosida",
    "asosiy",
    "aniq",
    "to'liq",
    "optimal",
    "oddiy",
    "native",
    "klassik",
}

# Known Uzbek tech action verbs
KNOWN_ACTION_VERBS: set[str] = {
    "chiqardi",
    "chiqargan",
    "chiqaradi",
    "chiqarildi",
    "chiqarilmoqda",
    "chiqarmoqda",
    "tushirdi",
    "tushirgan",
    "tushiradi",
    "tushirildi",
    "tushirilmoqda",
    "tushirmoqda",
    "qildi",
    "qilgan",
    "qiladi",
    "qilindi",
    "qilinmoqda",
    "qilmoqda",
    "etdi",
    "etgan",
    "etadi",
    "etildi",
    "etilmoqda",
    "etmoqda",
    "boshladi",
    "boshlagan",
    "boshlaydi",
    "boshlandi",
    "boshlanmoqda",
    "boshlamoqda",
    "yaratdi",
    "yaratgan",
    "yaratadi",
    "yaratildi",
    "yaratilmoqda",
    "yaratmoqda",
    "kiritdi",
    "kiritgan",
    "kiritadi",
    "kiritildi",
    "kiritilmoqda",
    "kiritmoqda",
    "o'rnatdi",
    "o'rnatgan",
    "o'rnatadi",
    "o'rnatildi",
    "o'rnatilmoqda",
    "o'rnatmoqda",
    "ochdi",
    "ochgan",
    "ochadi",
    "ochildi",
    "ochilmoqda",
    "ochmoqda",
    "berdi",
    "bergan",
    "beradi",
    "berildi",
    "berilmoqda",
    "bermoqda",
    "ko'rsatdi",
    "ko'rsatgan",
    "ko'rsatadi",
    "ko'rsatildi",
    "ko'rsatilmoqda",
    "ko'rsatmoqda",
    "qurdi",
    "qurgan",
    "quradi",
    "qurildi",
    "qurilmoqda",
    "qurmoqda",
    "joylashtirdi",
    "joylashtirgan",
    "joylashtiradi",
    "joylashtirildi",
    "joylashtirilmoqda",
    "ishlaydi",
    "ishlagan",
    "ishladi",
    "ishlamoqda",
    "ishlamoqchi",
    "aniqladi",
    "aniqlagan",
    "aniqlaydi",
    "aniqlandi",
    "aniqlangan",
    "aniqlanmoqda",
    "yangiladi",
    "yangilagan",
    "yangilaydi",
    "yangilandi",
    "yangilanmoqda",
    "kengaytirdi",
    "kengaytirgan",
    "kengaytiradi",
    "kengaytirildi",
    "qo'shdi",
    "qo'shgan",
    "qo'shadi",
    "qo'shildi",
    "qo'shilmoqda",
    "o'tkazdi",
    "o'tkazgan",
    "o'tkazadi",
    "o'tkazildi",
    "o'tkazilmoqda",
    "ulashdi",
    "ulashgan",
    "ulashadi",
    "ulashildi",
    "yubordi",
    "yuborgan",
    "yuboradi",
    "yuborildi",
    "yuborilmoqda",
    "tizdi",
    "tizgan",
    "tizadi",
    "tizildi",
    "saqlaydi",
    "saqlagan",
    "saqladi",
    "saqlandi",
    "saqlangan",
    "yozdi",
    "yozgan",
    "yozadi",
    "yozildi",
    "yozilmoqda",
    "oshirdi",
    "oshirgan",
    "oshiradi",
    "oshirildi",
    "kamaytirdi",
    "kamaytirgan",
    "kamaytiradi",
    "kamaytirildi",
    "qaytardi",
    "qaytargan",
    "qaytaradi",
    "qaytarildi",
    "foydalandi",
    "foydalangan",
    "foydalanadi",
    "foydalanildi",
    "birlashdi",
    "birlashtirdi",
    "birlashtirgan",
    "birlashtiradi",
    "birlashtirildi",
    "avtomatlashtirdi",
    "avtomatlashtirgan",
    "avtomatlashtiradi",
    "avtomatlashtirildi",
    "blokladi",
    "bloklagan",
    "bloklaydi",
    "bloklandi",
    "topdi",
    "topgan",
    "topadi",
    "topildi",
    "topilmoqda",
    "rivojlantirdi",
    "rivojlantirgan",
    "rivojlantiradi",
    "rivojlantirildi",
    "o'rgatadi",
    "o'rgatgan",
    "o'rgatdi",
    "o'rgatildi",
    "tekshiradi",
    "tekshirgan",
    "tekshirdi",
    "tekshirildi",
    "boshqaradi",
    "boshqargan",
    "boshqardi",
    "boshqarildi",
    "baholaydi",
    "baholagan",
    "baholadi",
    "baholandi",
    "qo'ydi",
    "qo'ygan",
    "qo'yadi",
    "qo'yildi",
    "qo'ymoqda",
    "olindi",
    "olingan",
    "oladi",
    "oldi",
    "olmoqda",
    "yutdi",
    "yutqazdi",
    "sinadi",
    "yopildi",
    "yopdi",
    "yopgan",
    "yopadi",
    "bog'ladi",
    "bog'lagan",
    "bog'laydi",
    "bog'landi",
    "yetkazdi",
    "yetkazgan",
    "yetkazadi",
    "yetkazildi",
    "o'tdi",
    "o'tgan",
    "o'tadi",
    "o'tildi",
}

_VERB_SUFFIX_RE = re.compile(
    r"^[a-zA-Zʻ‘’'`]+(di|tdi|ydi|gan|qan|kan|moqda|yapti|adi|yadi|ildi|tildi|rildi|ndi|shdi|shtirdi|tirdi|yotgan)$",
    re.IGNORECASE,
)


def is_valid_action_verb(token: str) -> bool:
    """Check if token is a valid, single-word Uzbek action verb."""
    if not token or not token.strip():
        return False

    clean = _CLEAN_TOKEN_RE.sub("", token.strip())
    # Must be single token (no spaces, no slashes, no dots)
    forbidden_chars = (" ", "/", "\\", ".", ":", "@")
    if any(c in clean for c in forbidden_chars):
        return False

    if len(clean) < 2:
        return False

    # Check that it contains only letters and Uzbek apostrophe variants
    if not re.match(r"^[a-zA-Zʻ‘’'`]+$", clean):
        return False

    clean_lower = (
        clean.lower().replace("’", "'").replace("‘", "'").replace("ʻ", "'").replace("`", "'")
    )

    if clean_lower in BANNED_ANCHOR_TOKENS:
        return False

    if clean_lower in KNOWN_ACTION_VERBS:
        return True

    # Fallback to verb suffix morphology if not explicitly banned
    if _VERB_SUFFIX_RE.match(clean_lower):
        return True

    return False


def clean_token(token: str) -> str:
    """Strip leading and trailing punctuation from a token."""
    return _CLEAN_TOKEN_RE.sub("", token.strip())


def split_first_sentence(text: str) -> tuple[str, str, str]:
    """Extract first sentence components (inner text, full match with punct, rest of text).

    Handles abbreviations (such as U.S., e.g., i.e., Inc., Dr.) by checking whether
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


def resolve_anchor(lead: str, requested_anchor: str = "") -> str:
    """Find a valid one-word Uzbek action verb anchor in the first sentence of lead.

    Scans the first sentence:
    1. If requested_anchor is a valid action verb present in the first sentence, use it.
    2. Otherwise, scans first sentence tokens from end to beginning for the first valid action verb.
    3. Returns '' if no valid action verb is found.
    """
    if not lead or not lead.strip():
        return ""

    first_sent_text, _, _ = split_first_sentence(lead)
    tokens = [clean_token(t) for t in first_sent_text.split() if clean_token(t)]

    # 1. If requested_anchor is a valid action verb and present in first sentence tokens, use it
    req_clean = clean_token(requested_anchor)
    if req_clean and is_valid_action_verb(req_clean):
        for tok in tokens:
            if tok.lower() == req_clean.lower():
                return tok

    # 2. Deterministically scan from end of first sentence backwards for the first valid action verb
    for tok in reversed(tokens):
        if is_valid_action_verb(tok):
            return tok

    # 3. No valid action verb found in first sentence
    return ""


def linkify_lead(lead: str, url: str, anchor: str) -> str:
    """Insert exactly one <a href="..."> inside the first sentence of lead on anchor.

    Requires a valid one-word action verb anchor matching a distinct token in the first sentence.
    Uses boundary-aware token matching so matching 'etdi' does not link 'ketdi'.
    """
    if not anchor or not is_valid_action_verb(anchor):
        raise ValueError(f"Invalid or missing action verb anchor: '{anchor}'")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid URL scheme or missing host: {url}")

    escaped_url = html_escape(url, quote=True)
    clean_anch = clean_token(anchor)

    # Separate first sentence and rest of lead
    first_sent_text, first_sent_full, rest = split_first_sentence(lead)

    # Boundary-aware token replacement in first sentence text
    # Avoid matching inside longer tokens like 'ketdi' when anchor is 'etdi'
    token_pattern = re.compile(
        rf"(?<!{_WORD_CHAR_PATTERN})({re.escape(clean_anch)})(?!{_WORD_CHAR_PATTERN})",
        re.IGNORECASE,
    )

    match_anch = token_pattern.search(first_sent_text)
    if not match_anch:
        raise ValueError(f"Anchor '{clean_anch}' not found in sentence: '{first_sent_text}'")

    start, end = match_anch.span(1)
    matched_word = first_sent_text[start:end]

    linked_first_text = (
        html_escape(first_sent_text[:start])
        + f'<a href="{escaped_url}">{html_escape(matched_word)}</a>'
        + html_escape(first_sent_text[end:])
    )

    # Trailing punctuation of the first sentence (e.g. '.', '!')
    punct_suffix = first_sent_full[len(first_sent_text) :]

    return linked_first_text + html_escape(punct_suffix) + html_escape(rest)


_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def visible_length(html_text: str) -> int:
    """Calculate character count of text as seen by readers (tags stripped, entities unescaped)."""
    plain = _TAG_STRIP_RE.sub("", html_text)
    return len(html_unescape(plain))


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁO‘OʻG‘Gʻ\d\W])")


def split_sentences(text: str) -> list[str]:
    """Split text into sentences while preserving decimal numbers and abbreviations."""
    if not text or not text.strip():
        return []
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p and p.strip()]


def _assemble_candidate(lead_html: str, body_1: str, body_2: str, kicker: str, tag: str) -> str:
    parts = [lead_html.strip()]
    if body_1.strip():
        parts.append(html_escape(body_1.strip()))
    if body_2.strip():
        parts.append(html_escape(body_2.strip()))
    if kicker.strip():
        parts.append(html_escape(kicker.strip()))
    parts.append(tag.strip())
    return "\n\n".join(parts)


def trim_post_fields(
    lead_html: str,
    body_1: str,
    body_2: str,
    kicker: str,
    tag: str,
    max_chars: int = 900,
) -> tuple[str, str, str]:
    """Progressively trim body_2, body_1, then kicker until visible length <= max_chars."""
    if visible_length(_assemble_candidate(lead_html, body_1, body_2, kicker, tag)) <= max_chars:
        return body_1, body_2, kicker

    # Step 1: Drop trailing sentences from body_2
    b2_sentences = split_sentences(body_2)
    while (
        b2_sentences
        and visible_length(
            _assemble_candidate(lead_html, body_1, " ".join(b2_sentences), kicker, tag)
        )
        > max_chars
    ):
        b2_sentences.pop()
    body_2 = " ".join(b2_sentences)

    if visible_length(_assemble_candidate(lead_html, body_1, body_2, kicker, tag)) <= max_chars:
        return body_1, body_2, kicker

    # Step 2: Drop trailing sentences from body_1 (down to 1 sentence)
    b1_sentences = split_sentences(body_1)
    while (
        len(b1_sentences) > 1
        and visible_length(
            _assemble_candidate(lead_html, " ".join(b1_sentences), body_2, kicker, tag)
        )
        > max_chars
    ):
        b1_sentences.pop()
    body_1 = " ".join(b1_sentences)

    if visible_length(_assemble_candidate(lead_html, body_1, body_2, kicker, tag)) <= max_chars:
        return body_1, body_2, kicker

    # Step 3: Drop kicker if still over budget
    kicker = ""
    if visible_length(_assemble_candidate(lead_html, body_1, body_2, kicker, tag)) <= max_chars:
        return body_1, body_2, kicker

    # Step 4: Drop remaining body_1 sentences if still over budget
    while (
        b1_sentences
        and visible_length(
            _assemble_candidate(lead_html, " ".join(b1_sentences), body_2, kicker, tag)
        )
        > max_chars
    ):
        b1_sentences.pop()
    body_1 = " ".join(b1_sentences)

    candidate = _assemble_candidate(lead_html, body_1, body_2, kicker, tag)
    vis = visible_length(candidate)
    if vis > max_chars:
        raise ValueError(f"Lead and tag alone exceed max_chars budget ({vis} > {max_chars})")

    return body_1, body_2, kicker


_FORBIDDEN_TAGS_RE = re.compile(r"<(?!/?a(?:\s+[^>]*)?>)[^>]+>", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[•\*\-]\s+", re.MULTILINE)
_A_TAG_RE = re.compile(r'<a\s+href="[^"]+">.*?</a>', re.DOTALL | re.IGNORECASE)


def validate_rendered_post(rendered_html: str, max_chars: int = 900) -> list[str]:
    """Validate rendered HTML satisfies all post contract constraints."""
    violations = []

    # Exactly one <a> tag
    a_matches = _A_TAG_RE.findall(rendered_html)
    if len(a_matches) != 1:
        violations.append(f"Post must contain exactly one <a> tag, found {len(a_matches)}")
    else:
        # Check that anchor text inside <a> is exactly one approved action verb
        anchor_inner = re.sub(
            r'<a\s+href="[^"]+">(.*?)</a>', r"\1", a_matches[0], flags=re.DOTALL | re.IGNORECASE
        )
        plain_anchor = html_unescape(anchor_inner).strip()
        if not plain_anchor or " " in plain_anchor or not is_valid_action_verb(plain_anchor):
            violations.append(
                f"Anchor inside <a> must be a single approved action verb, got '{plain_anchor}'"
            )

    # No forbidden tags (<b>, <i>, <code>, <blockquote>, etc.)
    forbidden = _FORBIDDEN_TAGS_RE.findall(rendered_html)
    if forbidden:
        violations.append(f"Forbidden HTML tags found: {', '.join(set(forbidden))}")

    # No bullet formatting
    if _BULLET_RE.search(rendered_html):
        violations.append("Bullet points or markdown lists found in post")

    # Final line must be exactly one approved topic hashtag
    lines = [line.strip() for line in rendered_html.strip().splitlines() if line.strip()]
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

    return violations


def render_item_post_v2(item_data: dict, max_chars: int = 900) -> str:
    """Render a clean v2 post conforming to the approved spec."""
    url = item_data.get("url", "")
    lead = item_data.get("lead_uz") or item_data.get("summary_uz") or ""
    if not lead.strip():
        raise ValueError("Cannot render post: lead_uz / summary_uz is empty")

    requested_anchor = item_data.get("link_anchor_uz") or ""
    anchor = resolve_anchor(lead, requested_anchor)
    lead_html = linkify_lead(lead, url, anchor)

    body_1 = item_data.get("body_1_uz") or ""
    body_2 = item_data.get("body_2_uz") or ""
    kicker = item_data.get("kicker_uz") or ""

    topic = item_data.get("topic") or item_data.get("primary_topic")
    tag = get_topic_tag(topic)

    # Trim fields if over budget
    b1_trimmed, b2_trimmed, k_trimmed = trim_post_fields(
        lead_html=lead_html,
        body_1=body_1,
        body_2=body_2,
        kicker=kicker,
        tag=tag,
        max_chars=max_chars,
    )

    rendered = _assemble_candidate(lead_html, b1_trimmed, b2_trimmed, k_trimmed, tag)

    violations = validate_rendered_post(rendered, max_chars=max_chars)
    if violations:
        raise ValueError(f"Rendered post contract violation: {'; '.join(violations)}")

    return rendered
