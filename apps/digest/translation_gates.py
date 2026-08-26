"""Translation quality gates (T1.16).

Three mechanical checks that catch the highest-severity translation defects:
1. Numbers: every numeric token in English must appear in Uzbek.
2. Calques: a term must not be rendered as a known-wrong Uzbek form. Terms are no longer
   required to appear verbatim -- see the note above CALQUES.
3. Headline case: Uzbek does not use English Title Case.
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


#: Uzbek words for the small numbers, which prose spells out rather than writing as digits.
#:
#: Measured 2026-08-24: the English headline said "in 2 weeks" and the Uzbek correctly said
#: "ikki hafta". The gate saw no "2", called it a lost number, and article 11 lost its
#: translation permanently on a translation that was right.
#:
#: The list stops at ten on purpose. The defect this gate exists for is a *changed* precise
#: figure — mimo-v2.5 turning 2.4 trillion into 2 trillion — and no one writes 2.4, 123 or 84%
#: as words. Accepting a spelled-out small number costs none of that protection.
UZBEK_SMALL_NUMERALS = {
    "1": ("bir",),
    "2": ("ikki",),
    "3": ("uch",),
    "4": ("to'rt", "tort", "toʻrt"),
    "5": ("besh",),
    "6": ("olti",),
    "7": ("yetti", "etti"),
    "8": ("sakkiz",),
    "9": ("to'qqiz", "toqqiz", "toʻqqiz"),
    "10": ("o'n", "on", "oʻn"),
}


def _carries_number(number: str, uz_numbers: set[str], uz_text: str) -> bool:
    """True when the Uzbek carries `number` as digits or, for 1-10, as a word."""
    if number in uz_numbers:
        return True
    lowered = uz_text.lower()
    return any(re.search(rf"\b{word}\b", lowered) for word in UZBEK_SMALL_NUMERALS.get(number, ()))


#: English words for the small numbers, for the reverse-direction gate.
#:
#: The mirror of UZBEK_SMALL_NUMERALS and needed for the same reason. Against an article the
#: gate asks whether a number in the Uzbek exists in the source, and English prose writes
#: "two weeks" where the Uzbek writes "2 hafta". Without this the gate rejects a correct
#: post — the shape of defect that cost article 11 its translation on 2026-08-24.
ENGLISH_SMALL_NUMERALS = {
    "1": ("one",),
    "2": ("two",),
    "3": ("three",),
    "4": ("four",),
    "5": ("five",),
    "6": ("six",),
    "7": ("seven",),
    "8": ("eight",),
    "9": ("nine",),
    "10": ("ten",),
}


def check_numbers_against_source(article_text: str, uz_fields: dict) -> list[str]:
    """Gate 1, reverse direction: every number the Uzbek states must be in the article.

    The forward gate (check_numbers) catches a number lost in translation. This one also
    catches a number the model invented, which is the objection CONTENT_SCHEMA.md section 7
    raised against writing the post directly in Uzbek.

    Keys starting with `headline` are exempt, as in the forward gate: a headline compresses
    a figure away legitimately.
    """
    source_numbers = extract_numbers(article_text)
    lowered_source = article_text.lower()

    violations = []
    for key, value in uz_fields.items():
        if key.startswith("headline"):
            continue
        for number in extract_numbers(str(value)):
            if number in source_numbers:
                continue
            words = ENGLISH_SMALL_NUMERALS.get(number, ())
            if any(re.search(rf"\b{word}\b", lowered_source) for word in words):
                continue
            violations.append(f"Number not in the article: {number} (in {key})")
    return violations


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


def check_headline_case(en_headline: str, uz_headline: str, en_context: str = "") -> list[str]:
    """Gate 3: Uzbek headlines must not use English Title Case.

    Only the first word and proper nouns / acronyms appearing capitalized in English
    are allowed to be capitalized.

    `en_context` is the rest of the English payload, and it is not optional in practice.
    Measured 2026-08-24 on a live run: the Uzbek headline for article 11 named `Enzyme`, the
    testing library Asana migrated off. It appears in `lead_en` but not in `headline_en`, so
    a headline-only vocabulary flagged a correct proper noun, the retry flagged it again, and
    the article lost its translation permanently.

    This is the same shape as the presence gate removed on 2026-08-18: firing on the absence
    of a known-good token rather than the presence of a known-bad one. Widening the
    vocabulary keeps the real detection — genuine Title Case capitalises ordinary Uzbek
    words like `Bilan` or `Bepul`, which appear capitalised in no English field.
    """
    if not uz_headline or not en_headline:
        return []

    # Extract all capitalized words and components (e.g. Copilot from Copilot-Approved)
    en_raw_tokens = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", f"{en_headline} {en_context}")
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


def validate_against_source(
    article_title: str,
    article_text: str,
    uz_fields: dict,
    technical: dict | None = None,
) -> list[str]:
    """Gates for the single-stage Uzbek editorial (2026-08-26 design).

    The two-stage flow compared the Uzbek against four English fields. One stage has no such
    fields, so the English side becomes the article itself — which is larger than the four
    fields ever were, so the calque gate sees more terms, not fewer.
    """
    english_source = {"title": article_title, "text": article_text}
    if technical:
        english_source.update({k: str(v) for k, v in technical.items()})

    violations = []
    violations.extend(check_numbers_against_source(article_text, uz_fields))
    violations.extend(check_glossary(english_source, uz_fields))

    uz_hl = uz_fields.get("headline_uz", "")
    if article_title and uz_hl:
        violations.extend(check_headline_case(article_title, uz_hl, en_context=article_text))

    if violations:
        log.warning("Uzbek gate violation: %s", "; ".join(violations))

    return violations
