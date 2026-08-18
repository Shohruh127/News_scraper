"""Translation quality gates (T1.16).

Three mechanical checks that catch the highest-severity translation defects:
1. Numbers: every numeric token in English must appear in Uzbek.
2. Glossary: technical terms required to stay in English must appear in Uzbek and
   not be calqued/transliterated into Uzbek.
3. Headline case: Uzbek does not use English Title Case.
"""

import logging
import re

log = logging.getLogger(__name__)

# Established technical terms that prompt requires to stay in English
#: Terms required to appear verbatim in the Uzbek. Every entry here must have no
#: ordinary English sense, or the gate rejects correct translations -- see the note below.
GLOSSARY = (
    "open-weight",
    "weights",
    "benchmark",
    "inference",
    "token",
    "agent",
    "API",
    "quantization",
    "latency",
    "checkpoint",
    "prompt",
    "MoE",
)

# Known bad transliterations / calques measured in live runs
CALQUES = {
    "open-weight": ["ochiq-og'irlikli", "ochiq og'irlikli", "ochiq vaznli", "ochiq-vaznli"],
    "weights": ["og'irliklar", "og'irliklari", "vaznlar", "vaznlari"],
    "framework": ["freymvork", "ramka"],
    "quantization": ["kvantlash", "kvantizatsiya"],
    "checkpoint": ["nazorat nuqtasi", "chekpoint"],
    "inference": ["xulosa chiqarish", "inferensiya"],
}

# `context` and `framework` were removed from GLOSSARY on 2026-08-18 for the same reason
# as `embedding`, with evidence from a live run of 15 translations:
#
#   article 839  one article used both `context window` (technical) and `loss of context`
#                (ordinary) -- the gate cannot tell them apart. Narrowing the entry to the
#                compound did not help: the model rendered it `kontekst oynasi`, which is
#                correct Uzbek, and the post was blocked a third time. The word is out.
#   article 830  English read `legal frameworks`; the retry the gate forced produced
#                `huquqiy frameworkga` instead of the natural `huquqiy asoslarga`, and
#                then passed. A gate that degrades the text it approves is worse than
#                no gate. Both words appear in `uzbekistan_application_uz`, which
#                discusses policy and adoption and is where ordinary senses concentrate.
#
# `embedding` was removed from both lists earlier. It is the one entry that is commonly an
# ordinary English gerund rather than the ML term: a real article read "embedding
# interactive quizzes to test comprehension", where `joylashtirish` is the correct
# translation, and the gate rejected a good translation.
#
# The remaining terms carry the same risk in principle — `context`, `prompt` and `weights`
# all have ordinary senses — but in AI news text they appear overwhelmingly in the
# technical one. Detecting part of speech to do better would cost more than it saves;
# if another term produces a false positive in practice, remove it the same way.


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
    """Gate 2: terms that appear in English must stay in English in Uzbek."""
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

    # Presence requirement: only for terms that cannot be anything but the technical one.
    for term in GLOSSARY:
        t_low = term.lower()
        if t_low in en_lower and t_low not in uz_lower:
            violations.append(
                f"Glossary violation: English term '{term}' is missing from Uzbek translation"
            )

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

    en_words = en_headline.split()
    en_caps = {w.strip(",.:;\"'()") for w in en_words if w and w[0].isupper()}

    uz_words = uz_headline.split()
    violations = []
    for i, raw_word in enumerate(uz_words):
        word = raw_word.strip(",.:;\"'()")
        if i == 0 or not word:
            continue  # First word is always allowed to be capitalized
        if word[0].isupper():
            # Allowed if word is an acronym (all caps) or present in English capitalized words
            if word in en_caps or word.isupper() or word.replace("-", "").isupper():
                continue
            if _is_suffixed_acronym(word, en_caps):
                continue
            violations.append(
                f"Headline case violation: '{word}' is capitalized in Uzbek headline (Title Case)"
            )

    return violations


def validate_translation(en_fields: dict, uz_fields: dict) -> list[str]:
    """Run all three deterministic translation gates. Returns list of violation messages."""
    violations = []
    violations.extend(check_numbers(en_fields, uz_fields))
    violations.extend(check_glossary(en_fields, uz_fields))

    en_hl = en_fields.get("headline_en", "")
    uz_hl = uz_fields.get("headline_uz", "")
    violations.extend(check_headline_case(en_hl, uz_hl))

    if violations:
        log.warning("Translation gate violation: %s", "; ".join(violations))

    return violations
