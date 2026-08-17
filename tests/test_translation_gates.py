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


def test_glossary_gate_catches_missing_english_terms():
    """Gate 2: translating away required technical terms must fail."""
    en_fields = {
        "headline_en": "New Framework for AI Agent Benchmark",
        "summary_en": "A new framework for agent evaluation.",
    }
    uz_fields_bad = {
        "headline_uz": "Sun'iy intellekt vakillari uchun yangi tizim",
        "summary_uz": "Yangi dasturiy ta'minot sinovdan o'tkazildi.",
    }

    violations = translation_gates.check_glossary(en_fields, uz_fields_bad)
    assert len(violations) > 0
    assert any("framework" in v or "agent" in v or "benchmark" in v for v in violations)


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
