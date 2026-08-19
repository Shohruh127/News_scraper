"""Tests for deterministic translation quality gates (T1.16).

Tests use real measured failures from live runs as fixtures:
1. 2.4 -> 2 must fail the number gate (mimo-v2.5 defect).
2. 'ochiq-og'irlikli' must fail the glossary gate (calquing open-weight).
3. Title Cased headline must fail the headline case gate.
4. Clean output must pass all three gates.
"""

from apps.digest import translation_gates


def test_number_gate_catches_missing_numbers():
    """Gate 1: 2.4 trillion in English turned into 2 trillion in Uzbek must fail."""
    en_fields = {
        "headline_en": "Qwen Releases 2.4T Parameter Model",
        "summary_en": "Qwen released a new 2.4T open-weight model with 262144 context length.",
        "why_it_matters_en": "Outperforms 70B baselines.",
    }
    # Uzbek translation dropped '2.4' and wrote '2'
    uz_fields_bad = {
        "headline_uz": "Qwen 2T parametrli modelini chiqardi",
        "summary_uz": "Qwen 2T ochiq-vaznli yangi modelini 262144 context uzunligi bilan chiqardi.",
        "why_it_matters_uz": "70B ko'rsatkichlaridan ustun turadi.",
    }

    violations = translation_gates.check_numbers(en_fields, uz_fields_bad)
    assert len(violations) > 0
    assert any("2.4" in v for v in violations)


def test_number_gate_passes_clean_numbers():
    """Gate 1 passes when all numbers are preserved."""
    en_fields = {
        "headline_en": "Qwen Releases 2.4T Parameter Model",
        "summary_en": "Qwen released a new 2.4T open-weight model with 262144 context length.",
        "why_it_matters_en": "Outperforms 70B baselines.",
    }
    uz_fields_clean = {
        "headline_uz": "Qwen 2.4T parametrli modelini chiqardi",
        "summary_uz": (
            "Qwen yangi 2.4T open-weight modelini 262144 context uzunligi bilan chiqardi."
        ),
        "why_it_matters_uz": "70B bazaviy modellaridan ustun.",
    }

    violations = translation_gates.check_numbers(en_fields, uz_fields_clean)
    assert violations == []


def test_glossary_gate_catches_calques():
    """Gate 2: calquing 'open-weight' to 'ochiq-og'irlikli' must fail."""
    en_fields = {
        "headline_en": "Qwen Releases Open-Weight Model",
        "summary_en": "The open-weight model offers low latency inference.",
    }
    uz_fields_bad = {
        "headline_uz": "Qwen ochiq-og'irlikli modelini chiqardi",
        "summary_uz": "Ushbu ochiq-og'irlikli model past kechikishli inference taqdim etadi.",
    }

    violations = translation_gates.check_glossary(en_fields, uz_fields_bad)
    assert len(violations) > 0
    assert any("open-weight" in v and "ochiq-og'irlikli" in v for v in violations)


def test_glossary_does_not_require_a_term_to_survive_verbatim():
    """The presence half of this gate was removed, on its own record.

    Measured 2026-08-18: it fired three times in one live run -- `context` twice and
    `framework` once -- and every firing was a false positive. One of them lost a post
    entirely. Over the same corpus the terms it guarded survived anyway: `model` 21/21,
    `agent` 18/18, `API` 7/7, `inference` 4/4. It caught nothing and cost three posts.

    Calque detection stays, because a specific wrong rendering is evidence on its own.
    """
    en_fields = {
        "headline_en": "New Framework for AI Agent Benchmark",
        "summary_en": "A new framework for agent evaluation.",
    }
    uz_fields = {
        "headline_uz": "Sun'iy intellekt vakillari uchun yangi tizim",
        "summary_uz": "Yangi dasturiy ta'minot sinovdan o'tkazildi.",
    }

    assert translation_gates.check_glossary(en_fields, uz_fields) == []


def test_calque_detection_still_fires_after_the_presence_check_is_gone():
    """`framework` may become `asos`, never `ramka`."""
    en_fields = {"summary_en": "A new framework for agent evaluation."}
    uz_fields = {"summary_uz": "Agentlarni baholash uchun yangi ramka."}

    violations = translation_gates.check_glossary(en_fields, uz_fields)

    assert any("ramka" in v for v in violations)


def test_headline_case_gate_catches_title_case():
    """Gate 3: English Title Case carried over into Uzbek must fail."""
    en_headline = "Qwen Releases 2.4T Open-Weight Model"
    # In Uzbek, words after the first shouldn't be Title Cased unless capitalized in English
    uz_headline_bad = "Qwen 2.4T Yangi Open-Weight Modelini Chiqardi"

    violations = translation_gates.check_headline_case(en_headline, uz_headline_bad)
    assert len(violations) > 0
    assert any("Modelini" in v or "Chiqardi" in v or "Yangi" in v for v in violations)


def test_headline_case_gate_allows_acronyms_and_proper_nouns():
    """Gate 3: first word, acronyms (API, MoE, GPU), and English-capitalized names are allowed."""
    en_headline = "Anthropic Updates Claude API for Tool Use"
    uz_headline_clean = "Anthropic Claude API uchun yangilanish chiqardi"

    violations = translation_gates.check_headline_case(en_headline, uz_headline_clean)
    assert violations == []


def test_validate_translation_full_suite():
    """Complete validation pass on clean vs defective translation."""
    en = {
        "headline_en": "Ollama v0.32.11 Adds DeepSeek Support",
        "summary_en": "Ollama v0.32.11 release introduces support for DeepSeek agent framework.",
        "why_it_matters_en": "Enables local deployment with FP8 quantization and 32k context.",
    }

    # Clean translation
    uz_clean = {
        "headline_uz": "Ollama v0.32.11 DeepSeek qo'llab-quvvatlashini qo'shdi",
        "summary_uz": "Ollama v0.32.11 relizi DeepSeek agent framework uchun imkoniyat yaratdi.",
        "why_it_matters_uz": (
            "FP8 quantization va 32k context bilan mahalliy deploy imkonini beradi."
        ),
    }
    assert translation_gates.validate_translation(en, uz_clean) == []

    # Defective translation (missing number, bad glossary, title case)
    uz_bad = {
        "headline_uz": "Ollama Yangi Dasturiy Vositalarni Qo'shdi",
        "summary_uz": "Ollama yangi relizi agent ramkasi uchun imkoniyat yaratdi.",
        "why_it_matters_uz": "Kvantlash va kontekst bilan mahalliy deploy imkonini beradi.",
    }
    bad_violations = translation_gates.validate_translation(en, uz_bad)
    assert len(bad_violations) >= 3


def test_thousand_separators_do_not_trigger_the_number_gate():
    """Measured false positive: English "5,000+ websites" against Uzbek "5000+ veb-saytda".

    The comma split 5,000 into 5 and 000, so the gate reported both as missing from a
    translation that had carried the number correctly. Locales differ on thousand
    separators and the gate must not.
    """
    en = {"summary_en": "Scans hit 5,000+ websites across 1 000 000 requests."}
    uz = {"summary_uz": "Skanerlar 5000+ veb-saytda 1000000 so'rov bilan ishladi."}
    assert translation_gates.validate_translation(en, uz) == []


def test_a_genuinely_changed_number_is_still_caught():
    """The separator fix must not weaken the gate. mimo-v2.5 turned 2.4 trillion into 2."""
    en = {"summary_en": "A 2.4 trillion parameter model."}
    uz = {"summary_uz": "2 trillion parametrli model."}
    assert any("2.4" in v for v in translation_gates.validate_translation(en, uz))


def test_embedding_is_not_a_glossary_term():
    """It is the one listed term that is commonly an ordinary English gerund.

    A real article read "embedding interactive quizzes to test comprehension", where
    `joylashtirish` is correct, and the gate rejected a good translation for it.
    """
    en = {"summary_en": "The methods include embedding interactive quizzes."}
    uz = {"summary_uz": "Usullar interaktiv testlarni joylashtirishni o'z ichiga oladi."}
    assert translation_gates.validate_translation(en, uz) == []


def test_headline_case_gate_allows_acronym_with_uzbek_suffix():
    """Gate 3: an acronym that took an Uzbek case suffix is not Title Case.

    Measured on a live run: `CVEni` was rejected twice and article 851 lost its
    translation permanently, although `CVE` is the English term the glossary requires
    to be preserved. Uzbek agglutinates onto the borrowed token, so the result is
    neither all-caps nor equal to the English word.
    """
    en_headline = "New CVE Affects the MoE Router in vLLM"
    uz_headline = "Yangi CVEni MoEga tegishli router muammosi"

    assert translation_gates.check_headline_case(en_headline, uz_headline) == []


def test_headline_case_gate_still_catches_title_case_next_to_acronyms():
    """The suffix allowance must not blanket-approve the rest of the headline."""
    en_headline = "New CVE Affects the MoE Router"
    uz_headline = "Yangi CVEni Router Muammosi Aniqlandi"

    violations = translation_gates.check_headline_case(en_headline, uz_headline)
    assert any("Muammosi" in v for v in violations)
    assert not any("CVEni" in v for v in violations)


def test_glossary_allows_ordinary_sense_but_still_catches_calque():
    """A term with an ordinary English sense may be translated, but not calqued.

    Measured on article 830: English read `legal frameworks`, an ordinary-sense phrase.
    The gate demanded the English word, and the retry it forced produced
    `huquqiy frameworkga` instead of the natural `huquqiy asoslarga` -- then passed.
    """
    en = {"uzbekistan_application_en": "Success depends on adoption and legal frameworks."}

    natural = {
        "uzbekistan_application_uz": "Muvaffaqiyat joriy etish va huquqiy asoslarga bog'liq."
    }
    assert translation_gates.check_glossary(en, natural) == []

    calqued = {"uzbekistan_application_uz": "Muvaffaqiyat huquqiy ramka bilan bog'liq."}
    violations = translation_gates.check_glossary(en, calqued)
    assert any("ramka" in v for v in violations)


def test_glossary_does_not_require_context_at_all():
    """`context` is out of the required list, in either its bare or compound form.

    Measured on article 839: the model rendered `context window` as `kontekst oynasi`,
    which is correct Uzbek, and the gate blocked the whole post for it three times.
    """
    en = {"summary_en": "The context window grew to 262144 tokens."}
    uz = {"summary_uz": "Kontekst oynasi 262144 tokengacha kengaydi."}
    assert translation_gates.check_glossary(en, uz) == []


def test_check_kicker_passes_valid_kicker():
    """Valid punchy kicker under 8 words with no cliché and no new numbers passes."""
    kicker = "Lokal serverda qulay ishlaydi."
    assert translation_gates.check_kicker(kicker, "Model 24 milliard parametr.") == []


def test_check_kicker_rejects_over_8_words():
    """Kicker with more than 8 words fails the kicker gate."""
    kicker = (
        "Bu yangi texnologiya butun dunyo bo'ylab barcha dasturchilar uchun "
        "juda katta yengillik beradi."
    )
    violations = translation_gates.check_kicker(kicker)
    assert any("exceeds 8 words" in v for v in violations)


def test_check_kicker_rejects_banned_cliches():
    """Banned clichés such as 'yangi davr boshlanmoqda' or 'kelajak keldi' fail."""
    for cliche in translation_gates.BANNED_KICKER_CLICHES:
        kicker = f"Tizimda {cliche}."
        violations = translation_gates.check_kicker(kicker)
        assert any("banned cliché" in v for v in violations)


def test_check_kicker_rejects_new_numbers_not_in_body():
    """Kicker must not introduce a new numeric token not present in the body text."""
    body = "Model 24 milliard parametrga ega."
    kicker = "Narxi 110 dollar turadi."
    violations = translation_gates.check_kicker(kicker, body)
    assert any("repeats or introduces number not in body" in v for v in violations)


def test_check_link_anchor_passes_single_token():
    """Single-word link anchor passes the gate."""
    assert translation_gates.check_link_anchor("chiqardi") == []
    assert translation_gates.check_link_anchor("tushirdi") == []


def test_check_link_anchor_rejects_multiword_or_url():
    """Multiword phrases or URLs fail the link anchor translation gate."""
    v1 = translation_gates.check_link_anchor("yo'lga qo'ymoqda")
    assert any("single word token" in msg for msg in v1)

    v2 = translation_gates.check_link_anchor("https://github.com/zed")
    assert any("URLs or domains" in msg for msg in v2)
