"""Translation quality gates (T1.16).

Three mechanical checks that catch the highest-severity translation defects:
1. Numbers: every numeric token in English must appear in Uzbek.
2. Calques: a term must not be rendered as a known-wrong Uzbek form. Terms are no longer
   required to appear verbatim -- see the note above CALQUES.
3. Headline case: Uzbek does not use English Title Case.
4. Kicker: maximum 8 words, no clichés, no ungrounded numbers.
5. Link anchor: exactly one word token, no URLs or domain names.
"""

import logging
import re

log = logging.getLogger(__name__)

# The presence requirement -- "a term in the English must appear verbatim in the Uzbek" -- was
# removed on 2026-08-18 after being measured rather than assumed.
#
# In one live run it fired three times, on `context` twice and `framework` once, and all three
# were false positives; one lost a post. Over the same corpus the terms it guarded survived
# without it: model 21/21, agent 18/18, API 7/7, inference 4/4, open-weight 3/3. The prompt
# does this work, not the gate.
#
# Calque detection below stays. It looks for a specific wrong rendering rather than the absence
# of a right one, so it cannot fire on a correct translation: `framework` may legitimately
# become `asos`, but never `ramka`.
CALQUES = {
    "open-weight": ["ochiq-og'irlikli", "ochiq og'irlikli", "ochiq vaznli", "ochiq-vaznli"],
    "weights": ["og'irliklar", "og'irliklari", "vaznlar", "vaznlari"],
    "framework": ["freymvork", "ramka"],
    "quantization": ["kvantlash", "kvantizatsiya"],
    "checkpoint": ["nazorat nuqtasi", "chekpoint"],
    "inference": ["xulosa chiqarish", "inferensiya"],
}


#: Thousand separators differ by locale: English writes 5,000 and Uzbek writes 5000.
#: Without normalising, the comma splits 5,000 into 5 and 000 and the gate reports both
#: as missing from a translation that carried the number correctly. Measured on a real
#: article: "5,000+ websites" against "5000+ veb-saytda" was rejected wrongly.
_THOUSANDS = re.compile(r"(?<=\d)[,  ](?=\d{3}(?!\d))")


def extract_numbers(text: str) -> set[str]:
    """Numeric tokens, with thousand separators removed so 5,000 == 5000 == 5 000."""
    return set(re.findall(r"\d+(?:\.\d+)*", _THOUSANDS.sub("", text)))


def check_numbers(en_fields: dict, uz_fields: dict) -> list[str]:
    """Gate 1: every number in English must appear in Uzbek.

    Catches errors like 2.4 trillion -> 2 trillion (mimo-v2.5 defect).
    """
    en_text = " ".join(str(v) for v in en_fields.values())
    uz_text = " ".join(str(v) for v in uz_fields.values())

    en_numbers = extract_numbers(en_text)
    uz_numbers = extract_numbers(uz_text)

    missing = en_numbers - uz_numbers
    if missing:
        return [f"Numbers missing in Uzbek: {', '.join(sorted(missing))}"]
    return []


def check_glossary(en_fields: dict, uz_fields: dict) -> list[str]:
    """Gate 2: terms that appear in English must not be calqued/transliterated in Uzbek."""
    en_text = " ".join(str(v) for v in en_fields.values())
    uz_text = " ".join(str(v) for v in uz_fields.values())
    uz_lower = uz_text.lower()
    en_lower = en_text.lower()

    violations = []

    # Calque detection is the low-noise half: a specific wrong rendering is evidence on
    # its own. It runs for every CALQUES key, including terms no longer required to
    # appear verbatim -- `framework` may legitimately become `asos`, but never `ramka`.
    for term, bad_forms in CALQUES.items():
        if term.lower() not in en_lower:
            continue
        for bad in bad_forms:
            if bad.lower() in uz_lower:
                violations.append(f"Glossary violation: '{term}' was translated as '{bad}'")

    return violations


#: Strips a trailing run of lowercase letters, leaving the capitalised core. Applied to
#: BOTH languages: English wrote the plural `CVEs` while Uzbek agglutinated onto the
#: singular to make `CVEni`, so comparing whole words finds nothing.
_LOWER_TAIL = re.compile(r"[a-z’']+$")


def _acronym_stem(word: str) -> str:
    return _LOWER_TAIL.sub("", word)


def _is_suffixed_acronym(word: str, en_caps: set[str]) -> bool:
    """True for an English acronym carrying an Uzbek case suffix: CVEni, APIga, MoEda.

    Uzbek agglutinates onto the borrowed token, so the result is neither all-caps nor
    equal to the English word. Measured: `CVEni` was rejected twice and article 851 lost
    its translation permanently, though `CVE` is exactly the kind of term the glossary
    exists to preserve.

    Two uppercase letters are required in the stem, which is what keeps this from
    swallowing the gate whole: `Qonunining` stems to `Q` and `Muammosi` to `M`, so an
    ordinary Title Cased Uzbek word can never match an English token this way.
    """
    stem = _acronym_stem(word)
    if len(stem) < 2 or sum(c.isupper() for c in stem) < 2:
        return False
    return any(_acronym_stem(cap) == stem for cap in en_caps)


def check_headline_case(en_headline: str, uz_headline: str) -> list[str]:
    """Gate 3: Uzbek headlines must not use English Title Case.

    Only the first word and proper nouns / acronyms appearing capitalized in English
    are allowed to be capitalized.
    """
    if not uz_headline or not en_headline:
        return []

    # Extract all capitalized words and components (e.g. Copilot from Copilot-Approved)
    en_raw_tokens = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", en_headline)
    en_caps = set()
    for w in en_raw_tokens:
        if w and w[0].isupper():
            en_caps.add(w)
            for part in re.split(r"[-_]", w):
                if part and part[0].isupper():
                    en_caps.add(part)
                    en_caps.add(_acronym_stem(part))

    uz_words = uz_headline.split()
    violations = []
    for i, raw_word in enumerate(uz_words):
        word = raw_word.strip(",.:;\"'()«»“”")
        if i == 0 or not word:
            continue  # First word is always allowed to be capitalized
        if word[0].isupper():
            # Allowed if word is an acronym (all caps) or present in English capitalized words
            if word in en_caps or word.isupper() or word.replace("-", "").isupper():
                continue
            if _is_suffixed_acronym(word, en_caps):
                continue
            # Allow suffixed borrowed nouns: Delta'ni -> Delta, Repo'dagi -> Repo, etc.
            stem = _acronym_stem(word)
            apostrophe_stem = re.split(r"['’`]", word)[0]
            if stem in en_caps or apostrophe_stem in en_caps:
                continue
            # Check singular/plural stem match against English capitalized words
            if any(
                stem.startswith(cap) or apostrophe_stem.startswith(cap)
                for cap in en_caps
                if len(cap) >= 3
            ):
                continue
            violations.append(
                f"Headline case violation: '{word}' is capitalized in Uzbek headline (Title Case)"
            )

    return violations


BANNED_KICKER_CLICHES = (
    "yangi davr boshlanmoqda",
    "kelajak keldi",
    "hammasi o'zgardi",
    "o'yin qoidalari o'zgardi",
    "bu faqat boshlanishi",
    "vaqt ko'rsatadi",
    "bir narsa aniq",
    "dunyo o'zgarmoqda",
)


def check_kicker(kicker_uz: str, body_text: str = "") -> list[str]:
    """Kicker validation gate:

    1. Maximum 8 words.
    2. Must not contain banned clichés.
    3. Must not introduce a new number not already present in the body text.
    """
    if not kicker_uz or not kicker_uz.strip():
        return []

    words = kicker_uz.strip().split()
    violations = []

    # Length check: <= 8 words
    if len(words) > 8:
        violations.append(f"Kicker exceeds 8 words ({len(words)} words): '{kicker_uz}'")

    # Cliché check
    k_lower = kicker_uz.lower()
    for cliche in BANNED_KICKER_CLICHES:
        if cliche in k_lower:
            violations.append(f"Kicker contains banned cliché '{cliche}': '{kicker_uz}'")

    # Number check: kicker must not introduce a new number not present in body_text
    if body_text:
        body_numbers = extract_numbers(body_text)
        kicker_numbers = extract_numbers(kicker_uz)
        new_numbers = kicker_numbers - body_numbers
        if new_numbers:
            violations.append(
                f"Kicker repeats or introduces number not in body: {', '.join(sorted(new_numbers))}"
            )

    return violations


def check_link_anchor(link_anchor_uz: str, lead_uz: str = "") -> list[str]:
    """Link anchor validation gate:

    1. Must be a single word token (no whitespace).
    2. Must not be a URL, domain, or multiword phrase.
    """
    if not link_anchor_uz or not link_anchor_uz.strip():
        return []

    clean = link_anchor_uz.strip()
    violations = []
    if " " in clean:
        violations.append(
            f"Link anchor must be a single word token, got multiword: '{link_anchor_uz}'"
        )
    if "/" in clean or "http" in clean or ".com" in clean or ".org" in clean:
        violations.append(f"Link anchor must not contain URLs or domains: '{link_anchor_uz}'")

    return violations


def validate_translation(en_fields: dict, uz_fields: dict) -> list[str]:
    """Run deterministic translation gates. Returns list of violation messages."""
    violations = []
    violations.extend(check_numbers(en_fields, uz_fields))
    violations.extend(check_glossary(en_fields, uz_fields))

    en_hl = en_fields.get("headline_en", "")
    uz_hl = uz_fields.get("headline_uz", "")
    if en_hl and uz_hl:
        violations.extend(check_headline_case(en_hl, uz_hl))

    kicker_uz = uz_fields.get("kicker_uz", "")
    if kicker_uz:
        body_parts = [
            str(uz_fields.get(k, ""))
            for k in ("lead_uz", "body_1_uz", "body_2_uz", "summary_uz")
            if uz_fields.get(k)
        ]
        violations.extend(check_kicker(kicker_uz, " ".join(body_parts)))

    anchor_uz = uz_fields.get("link_anchor_uz", "")
    if anchor_uz:
        violations.extend(check_link_anchor(anchor_uz, uz_fields.get("lead_uz", "")))

    if violations:
        log.warning("Translation gate violation: %s", "; ".join(violations))

    return violations
