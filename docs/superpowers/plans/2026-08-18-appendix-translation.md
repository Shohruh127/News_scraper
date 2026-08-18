# Appendix Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the group appendix read as Uzbek instead of Uzbek labels wrapped around English sentences, and retire a gate that has caught nothing and broken three posts.

**Architecture:** The technical block's prose fields are passed into the existing translation call with an `_en` suffix, so the dynamic schema built in the archetype work converts them to `_uz` with no new machinery. URLs and install commands stay English. `_item_data` prefers the Uzbek value and falls back to the English one, so stored digests keep rendering.

**Tech Stack:** Django templates, Ollama `gemma4:latest`, pytest.

**Spec:** none — bounded change agreed in chat on 2026-08-18, from the measurements quoted below.

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- The suite must stay green: `uv run pytest -q` → **164 passed** before this plan
- Do **not** change the "Keep in English" term list in `TRANSLATION_PROMPT`. It was measured working and adding to it only creates new false positives
- Do **not** translate `repo_url`, `api_url`, `install` or `license`
- Stored `editorial_en` payloads are not migrated. Old digests must still render
- One Django app, functions over classes, no abstraction before the second case

---

## Why this change exists

Measured 2026-08-18 across digest #11's twelve items, counting English prose words in the
rendered output:

```
channel post      11 hits across 12 posts    nearly clean
group appendix   101 hits across 12 posts    half English
```

The appendix pairs Uzbek labels with English values:

```
Nima qurildi:  Ollama, an open-source tool for running large language models locally
Benchmarklar:  7-8% faster prefill on NVFP4 MLX models with a global scale
Cheklovlar:    Models that relied on the previous default may behave differently
```

This is deliberate — `class Translation` says "`technical` is not translated" — but the result
is the worst of both languages.

Two further measurements shaped the scope:

**Product names need no help.** Of 38 unambiguous product names in the stored English
(CamelCase, digit-bearing, or `vLLM`-style), **38 survived into the Uzbek**. `NanoClaw`,
`LangGraph`, `DeepSeek`, `DigitalOcean` all came through untouched.

**Technical terms need no help either.** Every term the prompt protects survives at or near
100%: `model` 21/21, `agent` 18/18, `API` 7/7, `inference` 4/4. The three apparent losses were
the ordinary senses — "loss of context", "embedding quizzes" — which *should* be translated.

So the policy "keep established terms and names in English" is already working, and this plan
does not touch it.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/digest/llm.py` | which technical fields go to translation | modify |
| `apps/digest/ranking.py` | prefer the Uzbek value, fall back to English | modify `_item_data` |
| `apps/digest/translation_gates.py` | drop the presence check, keep calque detection | modify |
| `tests/test_editorial.py` | field selection and suffixing | modify |
| `tests/test_publish.py` | appendix rendering and fallback | modify |
| `tests/test_translation_gates.py` | invert the one test that pinned the presence check | modify |

### Context an engineer new to this repo needs

The translation stage builds its JSON schema at call time from the keys it is given:
`translation_schema_for(fields)` rewrites a trailing `_en` into `_uz`. That is why the technical
fields are passed in as `what_was_built_en` rather than `what_was_built` — the suffix is what
makes the existing machinery produce `what_was_built_uz`. Nothing about the stored payload
changes; the suffix exists only inside the call.

`_item_data(item)` in `ranking.py` builds one context dict for both the post and the appendix.
The appendix template reads plain names like `{{ what_was_built }}`, so the fallback belongs in
`_item_data`, not in the template.

`validate_translation(en_fields, uz_fields)` runs on whatever dicts it is handed. Once the
technical prose is in those dicts, the number gate covers benchmark figures for the first time —
`"7-8% faster prefill"` is currently unchecked.

---

## Task 1: Translate the technical prose fields

**Files:**
- Modify: `apps/digest/llm.py` — near `COMMON_TRANSLATED_FIELDS` (line 408) and the translation call (line 1123)
- Modify: `apps/digest/ranking.py` — the technical block of `_item_data`
- Test: `tests/test_editorial.py`, `tests/test_publish.py`

**Interfaces:**
- Consumes: `translation_schema_for(fields)` and the `fields` dict built at the translation call, both from the archetype work
- Produces: `TECHNICAL_PROSE_FIELDS: tuple[str, ...]` and
  `technical_fields(payload: dict) -> dict[str, str]`, returning keys already suffixed `_en`.
  Task 2 uses neither.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_editorial.py`:

```python
def test_technical_fields_selects_prose_and_suffixes_it():
    """Prose is translated; URLs and commands are not.

    `install` is excluded because it is mixed: of five stored values two were prose and one was
    the bare command `ollama run muse-glimmer`. A mangled command is actively wrong — someone
    may run it — while an untranslated short phrase is merely suboptimal. The appendix already
    renders it inside <code>.
    """
    from apps.digest.llm import technical_fields

    payload = {
        "technical": {
            "what_was_built": "A minor version update for the checkpoint library.",
            "architecture": "Uses a custom database called DeltaDB.",
            "limitations": "Limited to American Sign Language.",
            "benchmarks": "Scores 70 BLEURT on FLEURS-ASL.",
            "hardware": "Spare smartphone or PC with a webcam.",
            "install": "ollama run muse-glimmer",
            "repo_url": "https://github.com/langchain-ai/langgraph",
            "api_url": "https://example.com/api",
            "license": "",
            "local_deployable": True,
        }
    }

    out = technical_fields(payload)

    assert set(out) == {
        "what_was_built_en",
        "architecture_en",
        "limitations_en",
        "benchmarks_en",
        "hardware_en",
    }
    assert out["what_was_built_en"].startswith("A minor version update")


def test_technical_fields_skips_empty_values():
    """A field the model could not ground stays out of the translation call."""
    from apps.digest.llm import technical_fields

    payload = {"technical": {"what_was_built": "Something", "architecture": "   "}}

    assert technical_fields(payload) == {"what_was_built_en": "Something"}


def test_technical_fields_handles_a_missing_block():
    """An article with no technical block must not raise."""
    from apps.digest.llm import technical_fields

    assert technical_fields({}) == {}


def test_technical_prose_reaches_the_translation_schema():
    """The `_en` suffix is what makes the existing dynamic schema produce `_uz`."""
    from apps.digest.llm import technical_fields, translation_schema_for

    fields = technical_fields({"technical": {"benchmarks": "7-8% faster prefill"}})

    assert set(translation_schema_for(fields)["properties"]) == {"benchmarks_uz"}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_editorial.py -q -k technical_
```

Expected: FAIL, `ImportError: cannot import name 'technical_fields'`

- [ ] **Step 3: Write the selector**

In `apps/digest/llm.py`, add this directly below `COMMON_TRANSLATED_FIELDS`:

```python
#: Technical fields that are prose and therefore translated. Measured 2026-08-18 over 37
#: stored rows: what_was_built 37 values averaging 14 words, limitations 29 at 20 words,
#: architecture 15 at 24, benchmarks 15 at 23, hardware 5 at 11 — all full sentences.
#:
#: repo_url and api_url are single URLs. license was empty in every row. `install` is
#: excluded because it is mixed: two of its five values were prose and one was the bare
#: command `ollama run muse-glimmer`, which the appendix renders inside <code>. Translating a
#: command is worse than leaving a phrase in English, because someone may run the result.
TECHNICAL_PROSE_FIELDS = (
    "what_was_built",
    "architecture",
    "limitations",
    "benchmarks",
    "hardware",
)


def technical_fields(payload: dict) -> dict[str, str]:
    """The technical block's prose, suffixed `_en` so the dynamic schema yields `_uz`.

    The suffix exists only inside the translation call. The stored payload keeps its plain
    names, so nothing needs migrating and old digests keep rendering.
    """
    block = payload.get("technical") or {}
    return {
        f"{name}_en": block[name]
        for name in TECHNICAL_PROSE_FIELDS
        if isinstance(block.get(name), str) and block[name].strip()
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_editorial.py -q -k technical_
```

Expected: PASS, 4 passed

- [ ] **Step 5: Add them to the translation call**

In `analyse_for_digest_logic`, this block currently reads (near line 1123):

```python
            fields = {k: en.payload.get(k, "") for k in COMMON_TRANSLATED_FIELDS}
            fields.update(archetype_fields(en.payload))
```

Add one line, so it becomes:

```python
            fields = {k: en.payload.get(k, "") for k in COMMON_TRANSLATED_FIELDS}
            fields.update(archetype_fields(en.payload))
            fields.update(technical_fields(en.payload))
```

Nothing else in the call changes: `translation_schema_for` and the `create_model` line already
follow whatever `fields` contains.

- [ ] **Step 6: Write the failing rendering test**

Append to `tests/test_publish.py`:

```python
@pytest.mark.django_db
def test_appendix_prefers_uzbek_and_falls_back_to_english(digest_item_factory):
    """A stored digest from before this change still renders, in English.

    `_item_data` prefers the `_uz` value. Old payloads have none, so they fall back rather
    than rendering blank labels.
    """
    item = digest_item_factory(archetype="release", detail={})

    en = item.article.analyses.get(stage=Analysis.Stage.EDITORIAL_EN)
    en.payload["technical"] = {
        "what_was_built": "An English sentence",
        "limitations": "An English limitation",
        "local_deployable": True,
    }
    en.save(update_fields=["payload"])

    html = ranking.render_item_appendix(item)
    assert "An English sentence" in html

    uz = item.article.analyses.get(stage=Analysis.Stage.EDITORIAL_UZ)
    uz.payload["what_was_built_uz"] = "O'zbekcha jumla"
    uz.save(update_fields=["payload"])

    html = ranking.render_item_appendix(item)
    assert "O'zbekcha jumla" in html
    assert "An English sentence" not in html
    assert "An English limitation" in html
```

- [ ] **Step 7: Run it to verify it fails**

```bash
uv run pytest tests/test_publish.py -q -k appendix_prefers
```

Expected: FAIL on the second assertion — the Uzbek value is stored but not read.

- [ ] **Step 8: Prefer the Uzbek value in `_item_data`**

In `apps/digest/ranking.py`, the technical block of `_item_data` currently reads:

```python
        # Technical appendix fields (English)
        "what_was_built": technical.get("what_was_built", ""),
        "architecture": technical.get("architecture", ""),
        "license": technical.get("license", ""),
        "repo_url": technical.get("repo_url", ""),
        "api_url": technical.get("api_url", ""),
        "hardware": technical.get("hardware", ""),
        "install": technical.get("install", ""),
        "benchmarks": technical.get("benchmarks", ""),
        "limitations": technical.get("limitations", ""),
        "local_deployable": technical.get("local_deployable", False),
```

Replace it with:

```python
        # Technical appendix. Prose comes from the translation when it exists and from the
        # English otherwise, so digests stored before appendix translation still render.
        # URLs and the install command are never translated.
        "what_was_built": uz_payload.get("what_was_built_uz")
        or technical.get("what_was_built", ""),
        "architecture": uz_payload.get("architecture_uz") or technical.get("architecture", ""),
        "hardware": uz_payload.get("hardware_uz") or technical.get("hardware", ""),
        "benchmarks": uz_payload.get("benchmarks_uz") or technical.get("benchmarks", ""),
        "limitations": uz_payload.get("limitations_uz") or technical.get("limitations", ""),
        "license": technical.get("license", ""),
        "repo_url": technical.get("repo_url", ""),
        "api_url": technical.get("api_url", ""),
        "install": technical.get("install", ""),
        "local_deployable": technical.get("local_deployable", False),
```

- [ ] **Step 9: Run the test to verify it passes**

```bash
uv run pytest tests/test_publish.py -q -k appendix_prefers
```

Expected: PASS

- [ ] **Step 10: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `169 passed` and `All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add apps/digest/llm.py apps/digest/ranking.py tests/test_editorial.py tests/test_publish.py
git commit -m "Translate the appendix prose, leaving URLs and commands in English"
```

---

## Task 2: Retire the glossary presence check

**Files:**
- Modify: `apps/digest/translation_gates.py` — `GLOSSARY` near line 16, `check_glossary` near line 105
- Test: `tests/test_translation_gates.py` — the test at line 67

**Interfaces:**
- Consumes: nothing from Task 1
- Produces: `check_glossary(en_fields, uz_fields)` keeps its signature and returns only calque
  violations. `GLOSSARY` is removed; `CALQUES` stays

- [ ] **Step 1: Invert the test that pinned the removed behaviour**

In `tests/test_translation_gates.py`, replace the whole of
`test_glossary_gate_catches_missing_english_terms` with:

```python
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
```

- [ ] **Step 2: Run the tests to verify the first fails**

```bash
uv run pytest tests/test_translation_gates.py -q -k "does_not_require_a_term or calque_detection_still"
```

Expected: `1 failed, 1 passed`. The first fails because the presence check still reports
`framework` and `agent` as missing; the second already passes.

- [ ] **Step 3: Remove the presence check**

In `apps/digest/translation_gates.py`, delete the `GLOSSARY` tuple entirely, then replace the
second half of `check_glossary`. The function currently ends:

```python
    # Presence requirement: only for terms that cannot be anything but the technical one.
    for term in GLOSSARY:
        t_low = term.lower()
        if t_low in en_lower and t_low not in uz_lower:
            violations.append(
                f"Glossary violation: English term '{term}' is missing from Uzbek translation"
            )

    return violations
```

Delete those lines so the function ends after the calque loop, with `return violations`.

Then replace the long comment block that begins `# `context` and `framework` were removed from
GLOSSARY` with:

```python
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
```

Finally, update the module docstring: it currently promises three checks, and the second is now
narrower. Change the line reading

```
2. Glossary: technical terms required to stay in English must appear in Uzbek and
   not be calqued/transliterated into Uzbek.
```

to

```
2. Calques: a term must not be rendered as a known-wrong Uzbek form. Terms are no longer
   required to appear verbatim -- see the note above CALQUES.
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_translation_gates.py -q
```

Expected: PASS

- [ ] **Step 5: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `170 passed` and `All checks passed!`

If `tests/test_llm.py` fails here, it is asserting the old gate behaviour end to end — report it
rather than adjusting the assertion.

- [ ] **Step 6: Commit**

```bash
git add apps/digest/translation_gates.py tests/test_translation_gates.py
git commit -m "Retire the glossary presence check, which caught nothing and cost three posts"
```

---

## After the plan: rebuild before the next run

The containers carry the source, because the Dockerfile ends with `COPY . .`:

```bash
docker compose build
docker compose up -d
```

`docker compose up -d` alone recreates a container from the **existing image** and would leave
this change out of the running system. That happened on 2026-08-18 with the archetype work: the
suite was green, the tree clean, and none of it was running.

Confirm afterwards:

```bash
docker compose exec -T worker-publish uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.llm import TECHNICAL_PROSE_FIELDS
print('technical prose fields in the container:', len(TECHNICAL_PROSE_FIELDS))
"
```

Expected: `5`

---

## Self-review

**Coverage of the agreed design**

| Agreed in chat | Task |
|---|---|
| Translate `what_was_built`, `architecture`, `limitations`, `benchmarks`, `hardware` | Task 1 Step 3 |
| Leave `repo_url`, `api_url`, `install`, `license` in English | Task 1 Step 3, pinned by `test_technical_fields_selects_prose_and_suffixes_it` |
| No schema change, `_en` suffix only inside the call | Task 1 Step 3, pinned by `test_technical_prose_reaches_the_translation_schema` |
| Old digests keep rendering | Task 1 Step 8, pinned by `test_appendix_prefers_uzbek_and_falls_back_to_english` |
| Benchmark numbers gain number-gate coverage | falls out of Task 1 Step 5 — the fields now reach `validate_translation` |
| Remove the glossary presence check, keep calques | Task 2 |
| Do not touch the "Keep in English" prompt list | Global Constraints; no task edits `TRANSLATION_PROMPT` |

**Placeholder scan:** none. Every code step carries its code, every command its expected output.

**Type consistency:** `technical_fields` returns `dict[str, str]` with `_en`-suffixed keys, which
is what `translation_schema_for` expects and what `fields.update(...)` in Task 1 Step 5 merges.
The `_uz` names read in Task 1 Step 8 — `what_was_built_uz`, `architecture_uz`, `hardware_uz`,
`benchmarks_uz`, `limitations_uz` — are exactly the five that suffix rewrite produces. Task 2
changes no signature.

**One thing worth flagging to the reviewer:** Task 2 removes a guard. That is unusual enough to
deserve a second opinion, and the case for it is entirely in the measurement quoted in the test
docstring — three firings, three false positives, zero true positives, one post lost. If a
reviewer disagrees with removing it, the fallback is to keep `check_glossary` as it is and ship
Task 1 alone; the two tasks are independent.
