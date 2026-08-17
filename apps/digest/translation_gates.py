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
GLOSSARY = (
    "open-weight",
    "weights",
    "benchmark",
    "inference",
    "context",
    "token",
    "framework",
    "agent",
    "API",
    "quantization",
    "latency",
    "checkpoint",
    "embedding",
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
    "embedding": ["joylashtirish", "ichiga joylashtirish"],
    "inference": ["xulosa chiqarish", "inferensiya"],
}


def extract_numbers(text: str) -> set[str]:
    """Extract numeric tokens (e.g. '2.4', '100', '0.32.11', unit suffixes like '2.4T')."""
    # Match numbers with possible decimals or version points
    return set(re.findall(r"\d+(?:\.\d+)*", text))


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
    for term in GLOSSARY:
        t_low = term.lower()
        if t_low in en_lower:
            # Check for direct calques
            for bad in CALQUES.get(term, []):
                if bad.lower() in uz_lower:
                    violations.append(
                        f"Glossary violation: '{term}' was translated as '{bad}'"
                    )
            # Check that the English term itself appears in Uzbek text
            # (unless it's an acronym like API / MoE where case check is relevant)
            if t_low not in uz_lower:
                violations.append(
                    f"Glossary violation: English term '{term}' is missing from Uzbek translation"
                )

    return violations


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
