# Implementation Plan

Version: 3.0 — handover edition
Date: 2026-08-14
Project root: `D:\News_scraper`
Git: `master`, last commit `13ac749`

Companion documents, all authoritative:
`PROJECT_PLAN.md` · `CONTENT_SCHEMA.md` · `TECHNICAL_REVIEW.md` ·
`ENVIRONMENT_INVENTORY.md` · `decisions/001-django-celery-stack.md` ·
`decisions/002-source-failure-policy.md` · `spike/OLLAMA_BENCHMARK.md` ·
`spike/CONTENT_VOLUME.md` · `spike/LANGUAGE_QUALITY.md`

---

## 0. Read this first

You are continuing an existing codebase. **T1.1 through T1.4 are built, tested and
committed.** Do not rebuild them. Section 1 tells you exactly what exists.

The system fetches AI-industry news from eight sources, will classify it with a local
Ollama server, rank it, and publish a daily Uzbek digest to a Telegram channel with a
technical appendix in the linked discussion group.

### Design rules — these override habit

1. **One Django app.** Everything in `apps/digest/`. Do not create a second app.
2. **Functions over classes.** A class only when it holds state across calls.
3. **No abstraction before the second case.** No `Protocol`, no ABC, no base classes.
4. **Use what Django gives you.** Admin instead of custom UI. `django-celery-results`
   instead of a job table.
5. **One file per concern.** All connectors in `connectors.py`, all LLM code in `llm.py`.
6. **Not in this plan means not in M1.** New ideas go to the backlog, not into the code.

### Execution rules

- One task at a time, in order. The acceptance check must pass before you move on.
- Every acceptance check is a command. Run it and paste the real output.
- Where this document says STOP, stop and ask the human.
- Report deviations. A deviation nobody knows about is a defect.

### Do not

| Do not | Why |
|---|---|
| Touch `D:\IMV_IB_Support`, `D:\diarization`, `D:\Doni_project`, `D:\chatbot` | Unrelated production systems, read-only references |
| Commit `.env` or any token | Secrets stay out of git |
| Call Ollama without `options.num_predict` | An uncapped call wedged the shared server and caused 503s for others (§2) |
| Add DRF, views, templates beyond admin, or any frontend | Telegram is the UI (ADR-001) |
| Add LangChain, a vector DB, Kubernetes, `asyncio`, or fine-tuning | Out of scope |
| Auto-disable a failing source | ADR-002: alert only |
| Publish to the real channel before GATE 1 | §5 |
| Label `data/gold_set.jsonl` yourself | It is the standard your work is measured against |

---

## 1. What already exists

```
manage.py
config/
  settings.py     one file: DB, Redis, Celery routes, Ollama, Telegram, ranking weights
  celery.py       Celery app, autodiscover
  urls.py         admin only
apps/digest/
  models.py       Topic, Maturity, EXCLUDED_MATURITIES, Source, Article,
                  Analysis, Digest, DigestItem, Feedback
  admin.py        all six registered; Source list shows health, has clear_failures action
  connectors.py   StructureChanged, RetryableHTTPError, parse_date,
                  fetch_rss/github/hn/hf/html, FETCHERS, fetch(source)
  extract.py      ExtractionFailed, canonical_url, content_hash, looks_blocked,
                  fetch_text, page_title, normalize(item, source)
  tasks.py        fetch_all_sources, fetch_source, _prefilter, _store,
                  _record_success, _record_failure, _alert_once_per_day
  management/commands/  seed_sources.py, fetch_sources.py
  migrations/     0001_initial, 0002_alter_source_connector
tests/            test_settings, test_models, test_connectors, test_extract
docker-compose.yml   postgres :5433, redis :6380, both 127.0.0.1 only
spikes/           M0 throwaway probes — delete when M0 is signed off
```

State: `ruff` clean, **41 tests passing**, ingestion verified against the eight live
sources (185 articles in 4m33s, all inside the 7-day window, no source failures).

### Contracts you must not break

```python
connectors.fetch(source) -> list[dict]
# {"url", "title", "published_at" (aware|None), "raw_text", "meta"}

extract.normalize(item, source) -> dict      # kwargs for Article.objects.create
                                             # raises ExtractionFailed
tasks._prefilter(source, items) -> (todo, stale, already)
```

`_prefilter` drops items older than `ARTICLE_MAX_AGE_DAYS` and URLs already stored,
**before** extraction. Extraction costs one HTTP request per item; OpenAI's feed
returns over a thousand entries. Never move extraction ahead of these checks.

### Settings you will use

`OLLAMA_BASE_URL` · `OLLAMA_FAST_MODEL` · `OLLAMA_DEEP_MODEL` · `OLLAMA_FAST_TIMEOUT` ·
`OLLAMA_DEEP_TIMEOUT` · `OLLAMA_MAX_CONCURRENCY` (=2) · `ARTICLE_MIN_CHARS` (=400) ·
`ARTICLE_MAX_AGE_DAYS` (=7) · `SOURCE_DEGRADED_AFTER` (=3) · `RANKING_WEIGHTS` ·
`DIGEST_MAX_ITEMS` (=7) · `DIGEST_MAX_PER_TOPIC` (=2) · `PUBLISHING_ENABLED` (=False) ·
`TELEGRAM_*`

### Getting running

```bash
docker compose up -d
uv run python manage.py migrate
uv run python manage.py seed_sources
uv run python manage.py fetch_sources
uv run pytest -q
```

---

## 2. Measured facts

From `spike/OLLAMA_BENCHMARK.md` and `spike/CONTENT_VOLUME.md`. Do not re-derive these,
and do not contradict them without a new measurement.

| Fact | Value |
|---|---|
| Models installed | **only two**: `gemma4:latest` (8.0B, Q4_K_M, digest `c6eb396dbd5992bb`) and `gemma4:31b` (31.3B, Q4_K_M, digest `6316f0629137b426`) |
| 8B latency | p50 5.59s, p95 6.20s |
| 31B latency | p50 11.93s, p95 27.52s |
| Schema validity | 100% on both, with enum definitions in the prompt |
| Determinism | `temperature: 0` reproduces output exactly |
| Concurrency ceiling | **2**. Eight parallel requests are slower than serial |
| Model swap cost | ~11s. Batch all triage, then all classification. Never alternate |
| Server context | 31B runs at `context_length: 20480` |
| Enum definitions | Decide accuracy: 31B scored ~1/10 on topic without them, ~9.5/10 with them |
| Content volume | ~31 items/day reach the classifier across the eight sources |
| Extraction failure | ~10-19%, mostly HN links behind paywalls and bot walls |

### `num_predict` is mandatory

An uncapped free-text call to `gemma4:31b` never terminated, consumed a 600s timeout,
held the server's slot, and caused `503` for the next seven requests. The server is
**shared with other teams**. Caps: triage/classify 400, Uzbek summary 500, deep analysis
1200. Client timeouts 60s fast, 180s deep.

Structured output (`format`) makes runaway far less likely because the grammar forces
the object closed — every classification call in M0 finished normally. Set `num_predict`
anyway.

### Model routing

```
~200 candidates
    ↓  8B triage        5.6s each   reject obvious junk
~50 survivors
    ↓  31B classify    20.2s each   the real decision
top 3-5
    ↓  31B deep analysis
```

At concurrency 2 this is roughly 23 minutes, inside the 60-minute budget.

---

## 3. Blocked on the human — check before starting T1.5

| Input | File | Needed by |
|---|---|---|
| Uzbek strategy chosen (A, B or C) | `spike/LANGUAGE_QUALITY.md`, then `CONTENT_SCHEMA.md` §7 | T1.6 |
| 26 rows labelled | `data/gold_set.jsonl` | T1.5 |

**STOP if either is missing.** T1.5's acceptance is precision measured against the gold
set; without labels the gate cannot be evaluated and GATE 1 cannot pass. Neither can be
done by an agent — the Uzbek judgement needs a native speaker, and labelling your own
test set means grading your own work.

---

## 4. Remaining M1 tasks

### T1.5 — Classification

**Files:** `apps/digest/llm.py`, additions to `tasks.py`,
`management/commands/eval_classifier.py`, `tests/test_llm.py`

1. **`ollama_chat(model, prompt, schema, timeout, num_predict) -> tuple[dict, int]`**
   returning `(parsed_payload, latency_ms)`. `stream: false`, `format=schema`,
   `options={"temperature": 0, "num_predict": num_predict}`. Retry with `tenacity` on
   timeout and 5xx only — reuse the `RetryableHTTPError` pattern from `connectors.py`.

2. **Pydantic `Classification`** matching `CONTENT_SCHEMA.md` §4 exactly:
   `primary_topic`, `maturity`, `novelty`, `evidence`, `production_readiness`, `reason`.
   There is no `relevant` field; rejection is `primary_topic == "irrelevant"` plus score
   thresholds.

3. **Prompts as module constants**, embedding the enum definitions from
   `CONTENT_SCHEMA.md` §2 and §3 **verbatim**, including the mandatory `irrelevant` rule
   and the `paper_only` boundary. This is the single highest-leverage thing in the file.

4. **Rule pre-filter before any LLM call**: blocklisted domains, and anything
   `_prefilter` would not already have caught. Log how many the rules removed.

5. **Two Celery tasks on the `llm` queue**:
   - `triage_article(article_id)` → fast model, sets `status='triaged'` or `'skipped'`
   - `classify_article(article_id)` → deep model, sets `status='classified'`
   Plus `triage_and_classify()` which runs **all** triage first, then **all**
   classification, to pay the model swap once.

6. Persist every call to `Analysis` with `model_tag`, `model_digest`, `latency_ms`.
   Read the digest from Ollama's response so a repointed `:latest` is detectable.

7. On Pydantic validation failure: retry once with the error appended to the prompt,
   then set `status='skipped'` and continue. **Never crash the batch.**

8. **`eval_classifier` command**: run over `data/gold_set.jsonl`, print precision,
   recall and a confusion matrix against `human_label` / `human_topic` /
   `human_maturity`. Support `--model` so both tiers can be compared.

**Acceptance:**
```bash
uv run pytest tests/test_llm.py -q && uv run python manage.py eval_classifier
```
Tests pass with mocked Ollama (`respx`). Eval prints **precision ≥ 0.80**.

**STOP if** precision stays under 0.80 after two prompt revisions. Report the confusion
matrix — the taxonomy may be wrong, and that is a human decision, not a prompt fix.

---

### T1.6 — Ranking and digest composition

**Files:** `apps/digest/ranking.py`, `apps/digest/templates/digest/*.html`,
`tests/test_ranking.py`

1. Weighted score from `settings.RANKING_WEIGHTS`. Weights are configuration; do not
   hard-code them.
2. Bonuses: open weights, public repo, local deployment possible.
   Penalties: announcement-only, unverified benchmark claim, no original source.
3. **Hard exclusion**: `maturity in EXCLUDED_MATURITIES` never enters a digest. The
   constant is already in `models.py`.
4. At most `DIGEST_MAX_PER_TOPIC` per topic, at most `DIGEST_MAX_ITEMS` total.
5. **Never pad.** Two qualifying items produce a two-item digest.
6. `compose_digest(date)` creates the `Digest` and its `DigestItem` rows. A second call
   for the same date must fail on the unique constraint, not on an `if`.
7. Two Django templates: channel post (leadership framing) and group comment (technical
   appendix). Escape HTML — Telegram accepts a restricted tag set only.
8. Uzbek text follows the strategy recorded in `CONTENT_SCHEMA.md` §7.

**Acceptance:**
```bash
uv run pytest tests/test_ranking.py -q
```
Must include a rendered-output snapshot test and an explicit test that a two-item day
yields a two-item digest.

---

### T1.7 — Publishing

**Files:** `apps/digest/publish.py`, `management/commands/publish_digest.py`,
`tests/test_publish.py`

Publishing is a plain `httpx.post` to the Telegram Bot API. **No aiogram in M1** —
Celery tasks are synchronous and `sendMessage` is one POST. aiogram arrives in M2 with
the feedback bot, which is the only part that needs long polling.

1. `POST /bot{token}/sendMessage`, `parse_mode=HTML` → store `channel_message_id`.
2. **Technical appendix**: the channel post is auto-forwarded to the linked group. Read
   updates for `message.is_automatic_forward == true`, match it to the channel post,
   reply to that group message id, store `group_message_id`. The bot must be an
   administrator in both channel and group. `getDiscussionMessage` is MTProto and is
   **not** available in the Bot API.
3. Edit and delete: two management commands taking a `DigestItem` id, calling
   `editMessageText` and `deleteMessage`.
4. **Kill switch**: when `PUBLISHING_ENABLED` is false, compose and store but send
   nothing. Default is false.
5. Send degraded-source alerts to `TELEGRAM_ADMIN_CHAT_ID`. `tasks._alert_once_per_day`
   currently only logs — wire it here.

No feedback buttons in M1. A button with no handler leaves the reader watching a spinner.

**Acceptance:**
```bash
uv run pytest tests/test_publish.py -q
```
Then a manual run against the **test** channel, reporting: post appeared, appendix
appeared as a comment under it, edit worked, delete worked, kill switch suppressed
sending.

**STOP if** the appendix cannot be posted as a comment. The channel-to-group link is
misconfigured and that needs the human, not a workaround.

---

### T1.8 — Schedule

**Files:** `config/celery.py` beat entries, `management/commands/run_pipeline.py`

| Time (Asia/Tashkent) | Task |
|---|---|
| 08:00 | `fetch_all_sources` |
| 17:00 | `fetch_all_sources` |
| 18:00 | `triage_and_classify` |
| 19:00 | `compose_and_publish` |

- `misfire_grace_time` set explicitly; `coalesce=True` so a missed job fires once.
- Idempotency is `Digest.digest_date` being unique — the database refuses the second
  run, so two concurrent workers cannot both pass a check.
- `Article.status` makes every stage resumable.
- Unhandled task exception → log, then alert `TELEGRAM_ADMIN_CHAT_ID`. The scheduler
  must never die.
- Task history is visible in admin through `django-celery-results`.

**Acceptance:**
```bash
uv run celery -A config worker -Q fetch,llm -c 4 --loglevel=info   # terminal 1
uv run celery -A config beat --loglevel=info                       # terminal 2
uv run python manage.py run_pipeline --date today                  # terminal 3
```
Completes end to end; `TaskResult` rows appear in admin; a second `run_pipeline` for the
same date is refused.

---

## 5. GATE 1 — before the public channel

- [ ] 7 consecutive days of automatic digests, no manual intervention
- [ ] The human has read all 7 and accepts the quality
- [ ] `eval_classifier` precision ≥ 0.80
- [ ] Full pipeline under 60 minutes
- [ ] Kill switch verified
- [ ] Edit and delete verified on a real published post
- [ ] `ruff` and `pytest` clean
- [ ] No secret anywhere in git history

Only then does `TELEGRAM_CHANNEL_ID` move from the test channel to the public one.
Delete `spikes/` at this point.

---

## 6. M2 — harden and go public

Execute in this order. Each is independently shippable.

| # | Task | Substance |
|---|---|---|
| T2.1 | Feedback bot | `aiogram 3` long polling as a separate process. Inline buttons 👍 👎 🛠 on channel posts, callback writes to `Feedback`. One reaction per user per item is already enforced by a constraint |
| T2.2 | Story clustering | One news item arriving from four sources becomes one post with four pieces of evidence. Start with canonical URL, fuzzy title (`rapidfuzz`, threshold ~92) and entity matching. Embeddings only if this measurably fails |
| T2.3 | 31B deep analysis | Top 3-5 items only. Fills the deep-analysis schema in `CONTENT_SCHEMA.md` §5. Empty string means "not in the source" — the model must never invent a URL, licence or benchmark |
| T2.4 | Verification layer | Vendor benchmark claims checked against Arena, Artificial Analysis, SWE-bench, Terminal-Bench. Sets `evidence_level` to `multiple_evidence` or leaves `vendor_claim_only` |
| T2.5 | Feedback learning | 👍 raises topic and source weight, 👎 lowers it, 🛠 raises applicability. Exponential moving average. **No fine-tuning** |
| T2.6 | Source expansion | 25-40 sources through the existing five connectors. Should require no new code — if it does, the connector config is too narrow |
| T2.7 | Health baselines | Rolling 14-day median per source of items found, extraction success rate and mean text length. Alert when today falls outside the band. This catches the silent failure: a source that still returns items but now returns 200-character paywall stubs |
| T2.8 | Monitoring and backup | JSON structured logging, healthcheck, PostgreSQL backup with a **tested** restore |
| T2.9 | Deployment | Dockerfile plus Compose for django, worker, beat, bot, postgres, redis. Healthchecks, restart policy, deployment preflight |
| T2.10 | Breaking news path | A major release does not wait for 19:00 |

### GATE 2

- [ ] 30 days stable
- [ ] Clustering works — no duplicate stories across posts
- [ ] Feedback measurably moves ranking
- [ ] Backup restore tested, not assumed
- [ ] Runs on the server under Docker

---

## 7. Known issues carried forward

| Issue | Where | Note |
|---|---|---|
| Anthropic items have no date | `fetch_html` | All 13 fall back to fetch time and are flagged `date_missing`. Extract a real date from the article page |
| Anthropic titles come from the URL slug | `fetch_html` | `page_title()` fixes this during extraction; verify it actually fires |
| HN extraction failure ~19% | `fetch_hn` | HN links to arbitrary sites; paywalls and bot walls are expected. Track the rate, do not chase zero |
| `maturity` regression | prompts | Papers that merely promise code get labelled `reproducible_open_source`. The boundary wording in `CONTENT_SCHEMA.md` §3 is the fix — use it verbatim |
| Server VRAM unknown | Ollama | 31B occupies 33.2 GB. If both models fit simultaneously, `keep_alive` could remove the ~11s swap cost. Unmeasured |

---

## 8. Reporting

Per task: what changed, the acceptance command, its verbatim output, any deviation and
why. Nothing else.
