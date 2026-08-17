# Remaining Work — M1 completion

Version: 1.0
Date: 2026-08-17
Continues: `IMPLEMENTATION_PLAN.md` v3 (T1.1–T1.13 are done)
Decisions: `decisions/003-m1-scope-correction.md`, `decisions/004-architecture-and-product-corrections.md`
Evidence: `spike/FIRST_PIPELINE_RUN.md`, `spike/DEDUP_MEASUREMENT.md`, `spike/OLLAMA_BENCHMARK.md`

---

## 0. Read this first

T1.1–T1.13 are built, tested and committed. This document covers what is left. Do not
rebuild anything in §1.

### Rules

1. One task at a time, in order. Its acceptance check must pass before the next starts.
2. Every acceptance check is a command. Run it, paste the real output.
3. Where it says STOP, stop and ask.
4. Report deviations. A deviation nobody knows about is a defect.
5. One Django app, functions over classes, no abstraction before the second case.

### Do not

| Do not | Why |
|---|---|
| Touch `D:\IMV_IB_Support`, `D:\diarization`, `D:\Doni_project`, `D:\chatbot` | Unrelated production systems |
| Commit `.env` or any token | Secrets stay out of git |
| Relax `CLUSTER_JACCARD_THRESHOLD` | Measured over 17,020 pairs with a 0.79 separation gap. It is not a tuning knob |
| Remove the source-based maturity ceiling | It is the only thing making the anti-vapourware filter work. Without it seven arXiv papers reach the digest |
| Reintroduce title-based similarity | Scored 0.000 on both decisive cases. `tests/test_clustering.py` pins this |
| Send translation to `gemma4:31b` | Measured worse: it is the model that garbled Uzbek. Translation belongs on `gemma4:latest` |
| Route triage or classification to MiMo | Only the editorial stages are routed. `gemma4` measured precision 0.83 and costs nothing |
| Raise `DIGEST_MAX_ITEMS` above 15 without asking | It is a post count now, not a list length |

### Verified facts you will need

| Fact | Value |
|---|---|
| Editorial providers | `EDITORIAL_EN_PROVIDER=mimo`, `TRANSLATION_PROVIDER=ollama` (`gemma4:latest`) |
| Translation `num_predict` | **2500**. At 1200 the JSON truncated mid-object and 5 of 7 translations were lost |
| MiMo structured output | `response_format` must be `json_schema` with `strict: true`. `json_object` conformed 2/7 |
| Clustering | char 5-gram Jaccard on text, 0.80, source ignored |
| Maturity ceiling | URL-keyed. arXiv/HF-papers → `paper_only`. A HuggingFace *model card* is not a paper |
| Ollama concurrency | 2. Eight parallel requests are slower than serial |
| Deep-model context | 32768 tokens on this server |

---

## 1. What is already done

- **T1.1–T1.13** — Django + Celery, six models, five connectors, extraction, triage,
  classification, ranking, publishing, Celery Beat, Docker with per-queue workers,
  Python 3.13.
- **Editorial in two stages** — English analysis then Uzbek translation, separate
  `Analysis` rows (`editorial_en`, `editorial_uz`), different providers per stage.
- **Clustering Tier A** — verified on live data: the Qwen duplicate merges at 0.900,
  consecutive Ollama releases stay apart at 0.110.
- **Source-based maturity ceiling** — 49 stored analyses corrected; the recomposed digest
  contains zero papers where it had seven.
- **96 tests, ruff clean.**

### GATE 1 status

| Claim | State |
|---|---|
| Beat tasks consumed by a worker | routing correct and tested; **not yet shown from a worker log** |
| Non-empty `summary_uz` per item | **passing** — 7/7 |
| One story from several sources = one item | **passing** — verified on the live pool |
| No article in two digests | passing, but only one digest has ever existed |
| Kill switch leaves digest `composed` | **not exercised** — `PUBLISHING_ENABLED=true` |
| Edit and delete on a real post | not done |
| 7 consecutive automatic days | not started |

---

## 2. Tasks

### T1.14 — One post per news item

**This is the product-shape change and everything visible depends on it.**

Currently `publish_digest` renders one `render_channel_post(digest)` and sends one
message, so all items share a `channel_message_id` — in the first run every one of the
seven stored `2`.

**Files:** `apps/digest/publish.py`, `apps/digest/ranking.py`,
`apps/digest/templates/digest/`, `tests/test_publish.py`

1. New renderer `render_item_post(item) -> str`: one self-contained post for one item.
   Keep `render_channel_post` only if something still needs a combined view; otherwise
   delete it rather than leaving two ways to render.
2. `publish_digest` loops items, sends one message each, stores that item's own
   `channel_message_id`.
3. Each post gets its own appendix: match its own auto-forward in the linked group by
   `forward_origin.message_id == item.channel_message_id`, reply there, store
   `group_message_id` per item.
4. Partial failure must be visible: if 12 of 15 post, the digest is `failed`, not
   `published`, and the admin alert names which items failed. Do not roll back sent posts.
5. Order: publish in `position` order so the channel reads top-ranked first.
6. Telegram allows roughly 20 messages per minute to a channel. At 15 posts add a small
   delay between sends rather than relying on luck.

**Acceptance:**
```bash
uv run pytest tests/test_publish.py -q
```
Tests must include: 15 items produce 15 distinct `channel_message_id` values; a failure
on item 8 leaves items 1–7 sent and the digest `failed`; each item's appendix matches its
own post and not a neighbour's.

---

### T1.15 — Three carried defects

Small, independent, and each has bitten once already.

**Files:** `apps/digest/management/commands/run_pipeline.py`, `apps/digest/tasks.py`,
`apps/digest/llm.py`

1. **`run_pipeline` crashes when the lock is held.** `triage_and_classify` returns
   `{"status": "skipped", "reason": "lock_held"}`, and line 63 reads
   `llm_result["triage_survivors"]` unconditionally → `KeyError`. A working safety
   mechanism currently presents as a crash. Handle the skipped shape and exit cleanly.
2. **The evening lock has no heartbeat.** `ex=3600` with no refresh means a killed worker
   blocks the pipeline for up to an hour. This happened: stopping a watcher shell on
   Windows did not kill its `python` child, which kept running orphaned and holding the
   lock. Refresh the TTL periodically while the task runs, and record the holder
   (host + pid) in the value so a stale lock is identifiable.
3. **`fetch_model_digest` calls `/api/tags` once per classification.** For 185 articles
   that is 185 redundant round trips against a server whose concurrency ceiling is 2.
   Cache it per process per model.

**Acceptance:**
```bash
uv run pytest tests/test_scheduler.py tests/test_llm.py -q
```
Tests: a held lock produces a clean skip rather than `KeyError`; the digest lookup issues
one `/api/tags` call for N classifications, not N.

---

### T1.16 — Deterministic translation gates

The translation model is good but not perfect, and at 10–15 independent posts a day a
99%-reliable prompt still ships a broken post every few days. These checks are cheap and
catch the highest-severity classes mechanically.

Measured failures to catch: `mimo-v2.5` turned *2.4 trillion* into *2 trillion*;
`open-weight` was calqued to `ochiq-og'irlikli`; headlines came back in English Title
Case, which Uzbek does not use.

**Files:** `apps/digest/llm.py`, `tests/test_translation_gates.py`

1. **Numbers.** Extract every numeric token from the English fields and from the Uzbek
   fields. Any number present in English and absent in Uzbek fails the gate. This is the
   most important of the three — a corrupted figure destroys the product's premise.
2. **Glossary.** Terms the prompt requires to stay English (`open-weight`, `weights`,
   `benchmark`, `inference`, `context`, `token`, `framework`, `agent`, `API`,
   `quantization`, `latency`, `checkpoint`, `embedding`, `prompt`, `MoE`) must appear
   untranslated in the Uzbek when they appear in the English.
3. **Headline case.** After the first character, capitals only inside words that are
   capitalised in the English source. Catches Title Case carried over from English.
4. On failure: retry once with the specific violation named in the prompt. On second
   failure, do not create the `editorial_uz` row. Rendering already refuses to publish
   without it, so a bad post cannot reach the channel.
5. Log every gate failure with the field and the violation. That log is the measurement
   of translation quality over time.

**Acceptance:**
```bash
uv run pytest tests/test_translation_gates.py -q
```
Tests must use the real measured failures as fixtures: 2.4 → 2 must fail the number gate;
`ochiq-og'irlikli` must fail the glossary gate; a Title Cased headline must fail the case
gate; clean output must pass all three.

**STOP if** the gates reject more than a third of real translations. That would mean the
gates are miscalibrated rather than the model being poor, and tightening them further
would block the pipeline instead of protecting it.

---

### T1.17 — Prompt fixes for the defects the first posts exposed

**Files:** `apps/digest/llm.py`

1. **Headline hallucination.** One English headline read *"Rust MCP Server **Meld**
   Launches…"* for an article about `MCP-stama`. "Meld" appears nowhere in the source.
   Add an explicit rule: every proper noun in the headline must appear verbatim in the
   article text.
2. **Empty `technical` block.** One item returned an empty `what_was_built` and no
   `evidence_level` despite `strict: true` requiring both. Strict mode constrains shape,
   not substance. Add a post-check: if `what_was_built` is empty, retry once.
3. **Per-stage prompts.** Triage, classification and editorial still share the enum
   definitions. Triage needs only enough to reject obvious noise; classification needs the
   full boundary definitions; editorial needs voice and format and no taxonomy at all.
   Splitting them shortens the triage prompt, which is the one that runs 185 times.

**Acceptance:** re-run the editorial stage on the current digest items and show that no
headline contains a proper noun absent from its source, and that every `technical` block
has a non-empty `what_was_built`.

---

### T1.18 — Live publishing verification

The channel and group are now linked, which was the blocker in the first run. This is the
first honest test of publishing.

**Prerequisite:** confirm `getChat` returns a non-null `linked_chat_id` for both. If it
does not, STOP — this is configuration, not code.

1. Set `PUBLISHING_ENABLED=false` and run the pipeline. Verify the digest stays
   `composed`, no `channel_message_id` is written, and nothing reaches Telegram. That is
   the kill-switch GATE 1 claim, still unexercised.
2. Set `PUBLISHING_ENABLED=true` against the **test** channel and run again. Verify each
   item became its own post, each has its own appendix in the group, and the digest is
   `published`.
3. Exercise edit and delete on a real published post.
4. Measure the full pipeline wall time and record it.

**Acceptance:** paste the resulting `DigestItem` rows showing distinct
`channel_message_id` and non-null `group_message_id` per item, plus the digest status and
the measured duration.

---

### T1.19 — Post format and UX

Requested by the project owner and deliberately left until the shape was settled. Do not
start before T1.14, or the research will describe a format the code cannot produce.

1. Study established technical news channels and platforms — what a good standalone post
   contains, how it is ordered, how links and code are presented, how long it is.
2. Produce **two or three concrete format options** as rendered examples using real
   content from the current digest, not descriptions.
3. Write them up for the project owner to choose from. **STOP there.** The choice is
   editorial and belongs to the owner, not the implementer.
4. Only after a choice: implement the template.

---

### T1.20 — Source expansion

Last, so new sources are not wired into a format about to change.

Current: 12 sources. `fintech`, `govtech` and `technical_talks` still have no feed, and
`new_approaches` lost its only feed when `hf_papers` was capped to `paper_only`.

1. Add sources through the existing five connectors. If a source needs a new connector
   type, say so rather than forcing it.
2. Adding a source must remain an admin edit. If it requires a code change, the connector
   config is too narrow.
3. Re-measure volume afterwards: the funnel numbers in `spike/CONTENT_VOLUME.md` are from
   8 sources and will be stale.

---

## 3. M2, not now

| Task | Why it waits |
|---|---|
| Embeddings Tier B (cosine ~0.85, cross-source) | Tier A handles verbatim duplicates. Tier B catches the same story written independently, which the current mostly-primary source mix rarely produces. It becomes real when aggregators and newsletters are added |
| Artifact verification | Would let genuine open-source papers back in by checking whether a promised repo actually resolves. Until then `hf_papers` publishes nothing and `new_approaches` has no feed |
| Feedback bot (aiogram) + buttons + learning | Needs one post per item first, so each post has its own thread |
| Independent benchmark verification | `evidence_level` is always `vendor_claim_only` today |
| Health baselines, backup, breaking-news path | Operational hardening after the product works |

---

## 4. Order and rationale

```
T1.14  one post per item        product shape; everything visible depends on it
T1.15  three carried defects    small, independent, each has bitten once
T1.16  translation gates        protects 10-15 daily posts mechanically
T1.17  prompt fixes             cheaper to judge once the gates exist
T1.18  live publishing          the first honest publishing test
T1.19  post format              needs T1.14; ends in a human choice
T1.20  more sources             needs the format settled
```

T1.14 and T1.15 are independent and can run in parallel. T1.16 and T1.17 both touch
`llm.py` and should not.

## 5. Reporting

Per task: what changed, the acceptance command, its verbatim output, any deviation and
why. Nothing else.
