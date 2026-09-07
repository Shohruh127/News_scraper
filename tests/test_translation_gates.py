"""Tests for deterministic translation quality gates (T1.16).

Tests use real measured failures from live runs as fixtures:
1. 'ochiq-og'irlikli' must fail the glossary gate (calquing open-weight).
2. Title Cased headline must fail the headline case gate.
3. Clean output must pass the gates.
"""

from apps.digest import translation_gates


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


def test_embedding_is_not_a_glossary_term():
    """It is the one listed term that is commonly an ordinary English gerund.

    A real article read "embedding interactive quizzes to test comprehension", where
    `joylashtirish` is correct, and the gate rejected a good translation for it.
    """
    en = {"summary_en": "The methods include embedding interactive quizzes."}
    uz = {"summary_uz": "Usullar interaktiv testlarni joylashtirishni o'z ichiga oladi."}
    assert translation_gates.check_glossary(en, uz) == []


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


def test_link_anchor_gate_is_gone():
    assert not hasattr(translation_gates, "check_link_anchor")


def test_headline_case_accepts_a_proper_noun_the_headline_paraphrases_away():
    """Measured 2026-08-24: article 11 lost its translation to this false positive.

    `Enzyme` (the testing library Asana migrated off) is in lead_en, not headline_en. A
    headline-only vocabulary flagged it, the retry flagged it again, and the post was lost.
    """
    en_headline = "Asana removes legacy test system with Codex"
    en_context = "Asana used OpenAI Codex to remove its outdated Enzyme testing system."
    uz_headline = "Asana Enzyme sinov tizimini Codex bilan olib tashladi"
    assert (
        translation_gates.check_headline_case(en_headline, uz_headline, en_context=en_context) == []
    )


def test_headline_case_still_rejects_real_title_case():
    """Widening the vocabulary must not disarm the gate. Ordinary Uzbek words appear
    capitalised in no English field, so genuine Title Case is still caught."""
    en_headline = "Replit launches free mode with GPT-5.6 Luna"
    en_context = "Replit shipped a free tier powered by GPT-5.6 Luna."
    uz_headline = "Replit GPT-5.6 Luna Bilan Bepul Rejimni Ishga Tushirdi"
    violations = translation_gates.check_headline_case(
        en_headline, uz_headline, en_context=en_context
    )
    assert any("Bilan" in v for v in violations)


def test_a_number_the_article_does_not_contain_is_a_violation():
    """The reverse direction catches an invented number, which the forward one cannot."""
    from apps.digest.translation_gates import check_numbers_against_source

    violations = check_numbers_against_source(
        "The model has 123B parameters.",
        {"body_1_uz": "Model 456B parametrga ega."},
    )
    assert violations, "456B is not in the article"


def test_a_number_the_article_contains_passes():
    from apps.digest.translation_gates import check_numbers_against_source

    assert not check_numbers_against_source(
        "The model has 123B parameters and 128k context.",
        {"body_1_uz": "Model 123B parametr va 128k kontekstga ega."},
    )


def test_an_english_word_number_in_the_article_covers_an_uzbek_digit():
    """The article writes 'two weeks'; the Uzbek writes '2 hafta'. Both are correct.

    This is the mirror of the 2026-08-24 defect, where 'ikki hafta' was rejected because
    the English said '2'. Article 11 lost its translation permanently over it.
    """
    from apps.digest.translation_gates import check_numbers_against_source

    assert not check_numbers_against_source(
        "The migration finished in two weeks.",
        {"body_1_uz": "Ko'chirish 2 haftada yakunlandi."},
    )


def test_the_headline_is_exempt_from_the_source_number_gate():
    """A headline legitimately compresses a figure away. Gating it killed a correct post
    on 2026-08-24."""
    from apps.digest.translation_gates import check_numbers_against_source

    assert not check_numbers_against_source(
        "No figures here.",
        {"headline_uz": "Migratsiya 2 haftada tugadi"},
    )


def test_an_uzbek_decimal_comma_matches_an_english_decimal_point():
    """Uzbek writes 0,75 where English writes 0.75 (F8).

    Measured 2026-09-07 on article 630: a correct $0.75 price post was flagged as
    invented ("0" and "75" missing) because the comma split the number.
    """
    from apps.digest.translation_gates import check_numbers_against_source, extract_numbers

    # extract_numbers is deliberately a superset: a comma between digits has more than
    # one reading and it emits all of them, leaving the choice to the run-by-run
    # comparison in check_numbers_against_source.
    assert "0.75" in extract_numbers("0,75 dollar")
    assert "5000" in extract_numbers("5,000 websites")
    assert not check_numbers_against_source(
        "Available at $0.75 per million input tokens.",
        {"body_1_uz": "Million kiruvchi tokenga 0,75 dollar turadi."},
    )


def test_a_three_digit_uzbek_fraction_is_not_read_as_thousands():
    """0,895 is a fraction, not a thousands separator (F8, second half).

    The thousands rule fires on a comma before exactly three digits, so it claimed
    "0,895" first and produced "0895" -- a token in neither language -- and the correct
    post was reported as inventing a number. Since the editorial stage now *discards* an
    article whose gates fail, this cost the whole post rather than a log line.
    """
    from apps.digest.translation_gates import check_numbers_against_source

    assert not check_numbers_against_source(
        "The model reaches 0.895 accuracy on the held-out set.",
        {"body_1_uz": "Model 0,895 aniqlikka erishdi."},
    )
    assert not check_numbers_against_source(
        "Latency dropped to 0.752 seconds.",
        {"body_1_uz": "Kechikish 0,752 soniyaga tushdi."},
    )


def test_a_comma_joined_run_in_the_source_still_matches_one_of_its_numbers():
    """trafilatura flattens tables into runs like "2024,2025"; the years must survive.

    Reading the source run only as a decimal removed the individual years from the
    source set, so an Uzbek post correctly repeating 2025 was flagged as invented.
    """
    from apps.digest.translation_gates import check_numbers_against_source

    assert not check_numbers_against_source(
        "The roadmap covers 2024,2025 and 2026.",
        {"body_1_uz": "2025-yilda chiqadi."},
    )


def test_a_genuinely_invented_decimal_still_fails():
    """The comma fix must not blind the gate: 0,80 is not 0.75."""
    from apps.digest.translation_gates import check_numbers_against_source

    assert check_numbers_against_source(
        "Available at $0.75 per million input tokens.",
        {"body_1_uz": "Million kiruvchi tokenga 0,80 dollar turadi."},
    )


def test_source_validation_runs_all_three_gates():
    from apps.digest.translation_gates import validate_against_source

    violations = validate_against_source(
        article_title="A new framework ships",
        article_text="A new framework ships with 123B parameters.",
        uz_fields={
            "headline_uz": "Yangi framework chiqdi",
            "lead_uz": "Jamoa 456B parametrli freymvork chiqardi.",
            "body_1_uz": "Batafsil ma'lumot yo'q.",
            "kicker_uz": "Sinab ko'ring.",
        },
    )
    joined = " ".join(violations)
    assert "456" in joined, "the invented number must be caught"
    assert "freymvork" in joined, "the calque must be caught"


def test_source_validation_passes_a_clean_post():
    from apps.digest.translation_gates import validate_against_source

    assert not validate_against_source(
        article_title="Mistral ships Mistral-Large-2",
        article_text="Mistral released Mistral-Large-2 with 123B parameters.",
        uz_fields={
            "headline_uz": "Mistral-Large-2 chiqdi",
            "lead_uz": "Mistral 123B parametrli modelni taqdim etdi.",
            "body_1_uz": "Model ochiq vazn bilan tarqatiladi.",
            "kicker_uz": "Shartnomasiz kuchli model.",
        },
    )


def test_the_article_title_supplies_the_headline_vocabulary():
    """check_headline_case returns early on an empty English headline, which would disable
    the gate entirely after the merge. The article title is what keeps it alive."""
    from apps.digest.translation_gates import validate_against_source

    violations = validate_against_source(
        article_title="Ollama ships a new runner",
        article_text="Ollama ships a new runner today.",
        uz_fields={"headline_uz": "Ollama Yangi Runner Chiqardi"},
    )
    assert violations, "English Title Case in the Uzbek headline must be caught"


def test_a_proper_noun_from_the_article_body_is_not_a_headline_violation():
    """The 2026-08-24 defect in its new shape, and the reason en_context is passed.

    `Enzyme` appeared in `lead_en` and not in `headline_en`. A headline-only vocabulary
    flagged a correct proper noun, the retry flagged it again, and article 11 lost its
    translation permanently. After the merge the vocabulary is the article text, so
    dropping it reintroduces exactly that failure.

    Written 2026-08-26 because the mutation `en_context=article_text` -> `en_context=""`
    survived: three tests covered the entry point and none of them covered the context.
    """
    from apps.digest.translation_gates import validate_against_source

    assert not validate_against_source(
        article_title="Asana replaces its testing stack",
        article_text="Asana migrated off Enzyme, the testing library, in two weeks.",
        uz_fields={"headline_uz": "Asana Enzyme dan voz kechdi"},
    )
