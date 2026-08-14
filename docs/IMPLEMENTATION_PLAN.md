# Implementation Plan — M0 & M1 (Django edition)

Version: 2.0 — supersedes v1.0 (minimal-stack edition)
Date: 2026-08-14
Project root: `D:\News_scraper`
Related: `PROJECT_PLAN.md`, `TECHNICAL_REVIEW.md`, `ENVIRONMENT_INVENTORY.md`,
`decisions/001-django-celery-stack.md`, `decisions/002-source-failure-policy.md`

---

## 0. How to work

### Design rules — these override any habit

1. **One Django app.** Everything lives in `apps/digest/`. Do not split into more apps.
2. **Functions over classes.** Use a class only when it holds state across calls.
3. **No abstraction before the second case.** No `Protocol`, no ABC, no base classes,
   no plugin registry beyond a plain dict.
4. **Use what Django gives you.** Admin instead of a custom UI. `django-celery-results`
   instead of a job-tracking table. Django ORM instead of raw SQL.
5. **One file per concern, not one file per class.** All four connectors live in
   `connectors.py`. All LLM code lives in `llm.py`.
6. **If a feature is not in this plan, it is not in M1.** Ideas go to the backlog.

### Execution rules

- One task at a time, in order. Acceptance check must pass before moving on.
- Every acceptance check is a command. Run it, paste the real output.
- Where it says STOP, stop and ask. There are only four STOPs in this document.
- Report deviations. A deviation nobody knows about is a defect.

### Do not

| Do not | Why |
|---|---|
| Touch `D:\IMV_IB_Support`, `D:\diarization`, `D:\Doni_project`, `D:\chatbot` | Unrelated production systems, read-only references |
| Commit `.env` or any token | Secrets stay out of git |
| Add DRF, views, templates (beyond admin), or a frontend | ADR-001. Telegram is the UI |
| Add LangChain, vector DB, Kubernetes, or fine-tuning | Out of scope |
| Auto-disable a failing source | ADR-002 |
| Write `:latest` in a pinned config field | Moving pointer; record the digest instead |

---

## 1. Verified facts

Measured or verified on 2026-08-14. Treat as given.

| Fact | Value |
|---|---|
| Ollama endpoint | `POST {OLLAMA_BASE_URL}/api/chat`, `stream:false`, `format` = JSON Schema object |
| Models on the server | **only two**: `gemma4:latest` (8.0B, Q4_K_M, digest `c6eb396dbd5992bb`) and `gemma4:31b` (31.3B, Q4_K_M, digest `6316f0629137b426`) |
| 8B latency | p50 **5.6s**, p95 **6.2s**, cold start 18.7s |
| 31B latency | p50 **11.9s**, p95 **27.5s**, mean 20.2s with the full prompt |
| Schema validity | 8B 20/20; 31B 10/10 with the defined-enum prompt |
| Determinism | `temperature: 0` gives identical output for identical input |
| Prompt cost | Enum definitions are free on 8B, **+37% on 31B** |
| Critical finding | Enum **definitions** matter more than model size. Without them 31B scored 1/10 on topic; with them, ~9.5/10 |
| Context | 8B 128K, 31B 256K — full articles fit, **no chunking needed** |
| OpenAI RSS | `https://openai.com/news/rss.xml` |
| DeepMind RSS | `https://deepmind.google/blog/feed/basic/` |
| Anthropic | **no RSS exists** — HTML connector on `https://www.anthropic.com/news` |
| HN Algolia | `https://hn.algolia.com/api/v1/search_by_date?tags=story` — no auth |
| HF papers | `https://huggingface.co/api/daily_papers?limit=100` — no auth |
| trafilatura | 2.2.0 |
| Telegram comment | Bot sees the auto-forwarded post in the linked group with `is_automatic_forward=true`; reply to that message id. `getDiscussionMessage` is MTProto, not Bot API |

### Model routing — decided by measurement

```
~200 articles
    ↓  8B triage        6s   → rejects obvious junk
~50 survivors
    ↓  31B classify    20s   → the real decision
top 3–5
    ↓  31B deep analysis
```

Budget: 200×6s + 50×20s ≈ **37 min**, inside the 60-minute limit.
Sending all 200 to 31B would take 67 min and blow the budget. That is why triage exists.

---

## 2. Environment

Already present: Python 3.12, uv 0.11.8, Docker 29.6.2, git 2.53.

`.env` keys (`.env.example` is committed, `.env` is not):

```
OLLAMA_BASE_URL, OLLAMA_FAST_MODEL, OLLAMA_DEEP_MODEL,
OLLAMA_FAST_TIMEOUT=60, OLLAMA_DEEP_TIMEOUT=300,
DATABASE_URL, REDIS_URL, DJANGO_SECRET_KEY, DJANGO_DEBUG,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, TELEGRAM_GROUP_ID, TELEGRAM_ADMIN_CHAT_ID,
PUBLISHING_ENABLED=false, TIME_ZONE=Asia/Tashkent
```

Telegram keys are needed from M1.7 onward, not before.

---

# MILESTONE 0 — SPIKE

Throwaway code in `spikes/`. Deleted at the start of M1.

## Status

| Task | State |
|---|---|
| T0.1 workspace, git, `.env` | **done** |
| T0.2 Ollama probe — tags, latency, schema, prompt A/B | **done** (see §1) |
| T0.2b concurrency test | **pending — next** |
| T0.3 Uzbek language quality | pending, needs human scoring |
| T0.4 content volume + gold set | pending, needs human labelling |
| T0.5 freeze `CONTENT_SCHEMA.md` | pending |

## T0.2b — concurrency

Run `probe_ollama.py concurrency --model gemma4:latest` at 1, 2, 4, 8 parallel requests.
Record wall time and effective speedup.

**This single number sets the `llm` Celery worker concurrency in M1.5.** A speedup near
1.0x means the server serialises and the worker must run `-c 1`.

Write results into `docs/spike/OLLAMA_BENCHMARK.md` together with the §1 measurements.

## T0.3 — Uzbek quality

`probe_language.py`: 10 articles × 3 strategies × 2 models.

- A: prompt in Uzbek, answer in Uzbek
- B: summarise in English, second call translates
- C: reason in English, emit `summary_uz` field

Output goes to `docs/spike/LANGUAGE_QUALITY.md` with an empty score column.

**STOP.** The human scores 1–5. Do not score Uzbek text yourself.

## T0.4 — Content volume + gold set

Collect 3 days from the five sources in §1. Report per-source per-day counts, duplicate
rate, and fetch reliability in `docs/spike/CONTENT_VOLUME.md`.

Produce `data/gold_set.jsonl`, 25–30 items spanning the quality range (include obvious
PR-fluff and obvious high-value releases), with `human_label`, `human_topic`,
`human_maturity` left `null`.

**STOP.** The human labels them. The gold set is how M1.5 is measured.

## T0.5 — Freeze the schema

`docs/CONTENT_SCHEMA.md`: the classification schema, the deep-analysis schema, the topic
and maturity enums as the single source of truth, the chosen Uzbek strategy, and the
pinned model tags with their measured latencies.

**Enum definitions are part of the schema, not the prompt.** M0 proved they decide
accuracy. Each definition must say how the category differs from its neighbour, not just
what it is. Specifically fix the known regression: an arXiv paper that merely promises
code is `paper_only`; `reproducible_open_source` requires a working link today.

## GATE 0

Report: concurrency ceiling · Uzbek strategy + scores · items/day passing the filter ·
gold set labelled yes/no. Then the human approves M1.

---

# MILESTONE 1 — THIN PRODUCT

## Layout

```
News_scraper/
├── manage.py
├── config/
│   ├── settings.py          # one file, one environment
│   ├── celery.py
│   └── urls.py              # admin only
├── apps/digest/
│   ├── models.py
│   ├── admin.py
│   ├── tasks.py             # celery tasks
│   ├── connectors.py        # all four fetchers
│   ├── extract.py           # trafilatura, canonical url, hash
│   ├── llm.py               # ollama client + pydantic schemas + prompts
│   ├── ranking.py
│   ├── publish.py           # telegram sendMessage via httpx
│   └── management/commands/
├── tests/
├── docker-compose.yml
└── pyproject.toml
```

Nine Python files. If a tenth is needed, ask why first.

---

## T1.1 — Django + Celery skeleton

**Files:** `pyproject.toml`, `manage.py`, `config/`, `apps/digest/`, `docker-compose.yml`

1. `uv init`; add `django`, `celery[redis]`, `django-celery-beat`,
   `django-celery-results`, `psycopg[binary]`, `django-environ`, `httpx`, `pydantic`,
   `feedparser`, `trafilatura`, `python-dateutil`, `tenacity`.
   Dev: `pytest`, `pytest-django`, `respx`, `ruff`.
2. `django-admin startproject config .`, then `apps/digest/` as the single app.
3. `config/settings.py` — single file, reads `.env` via `django-environ`.
   `TIME_ZONE = "Asia/Tashkent"`, `USE_TZ = True`.
4. `config/celery.py` — standard Django-Celery wiring, two queues:

```python
task_routes = {
    "digest.fetch_source": {"queue": "fetch"},
    "digest.classify_*":   {"queue": "llm"},
}
```

5. `docker-compose.yml`: postgres + redis, both bound to `127.0.0.1`, both with a
   healthcheck and a named volume. Django, worker and beat run from the host in M1.
6. `pyproject.toml`: `[tool.pytest.ini_options]` with `DJANGO_SETTINGS_MODULE = "config.settings"`.

**Acceptance:**
```bash
docker compose up -d && uv run python manage.py check && uv run python manage.py migrate && uv run pytest -q
```

---

## T1.2 — Models and admin

**Files:** `apps/digest/models.py`, `apps/digest/admin.py`, migration

Six models. No more.

| Model | Fields |
|---|---|
| `Source` | name, connector (choices: rss/github/hn/html), url, config (JSON), stream, enabled, priority, last_fetched_at, consecutive_failures, is_degraded |
| `Article` | source FK, canonical_url (unique), content_hash (unique), title, published_at, fetched_at, language, **extracted_text**, status (fetched/classified/skipped), meta (JSON) |
| `Analysis` | article FK, model_tag, model_digest, payload (JSON), latency_ms, created_at |
| `Digest` | digest_date (unique), status, composed_at, published_at |
| `DigestItem` | digest FK, article FK, position, score, channel_message_id, group_message_id |
| `Feedback` | digest_item FK, user_id, reaction, created_at — table created now, used in M2 |

Notes:
- Extracted text lives **on** `Article`. No separate content table.
- `model_digest` records the Ollama digest so a repointed `:latest` tag is detectable.
- No `job_runs` table — `django-celery-results` provides `TaskResult` with admin.

Admin must make source review a two-click job (ADR-002):
`Source` list shows name, connector, enabled, consecutive_failures, is_degraded,
last_fetched_at; filters on `is_degraded` and `enabled`.
`Article` list shows title, source, published_at, status.

**Acceptance:**
```bash
uv run python manage.py makemigrations && uv run python manage.py migrate && uv run python manage.py createsuperuser --noinput && uv run pytest tests/test_models.py -q
```
Then open `/admin/`, add one Source by hand, confirm it saves.

---

## T1.3 — Connectors

**Files:** `apps/digest/connectors.py`, `apps/digest/tasks.py`, `tests/test_connectors.py`

Four plain functions plus a dict. No classes.

```python
def fetch_rss(source) -> list[dict]: ...
def fetch_github(source) -> list[dict]: ...
def fetch_hn(source) -> list[dict]: ...
def fetch_html(source) -> list[dict]: ...

FETCHERS = {"rss": fetch_rss, "github": fetch_github,
            "hn": fetch_hn, "html": fetch_html}
```

Each returns dicts with `url`, `title`, `published_at`, `raw_text`, `meta`.
`fetch_html` reads its CSS selectors from `source.config` — editable in admin, no code
change to add a site.

Health checks (from ADR-002), in this order:
1. `fetch_html` asserts `source.config["min_items"]`; fewer matches raises
   `StructureChanged` rather than returning an empty list.
2. Text shorter than 400 chars, or containing `enable JavaScript` / `Access denied` /
   `captcha`, counts as an extraction failure.
3. On failure: `consecutive_failures += 1`. At 3, set `is_degraded=True` and alert once
   per day. **Never set `enabled=False`.**
4. One source failing must not abort the run.

`tenacity`: 3 attempts, exponential backoff, retry only on timeout, connection error,
5xx, 429. Never on other 4xx.

Celery: `fetch_all_sources` fans out one `fetch_source` task per source on the `fetch`
queue. Worker concurrency is the global limit; per-host politeness uses Celery
`rate_limit`.

Sources for M1 (created via admin or a data migration):
OpenAI RSS · DeepMind RSS · Anthropic HTML · HF papers · LangGraph GitHub ·
MCP GitHub · Ollama GitHub · HN Algolia.

**Tests:** `respx` mocks with saved fixtures. No test touches the network.

**Acceptance:**
```bash
uv run pytest tests/test_connectors.py -q && uv run python manage.py fetch_sources
```
Reports per-source counts; degraded sources are listed, not fatal.

**STOP if** a source needs JavaScript rendering. Playwright is out of M1 scope.

---

## T1.4 — Extraction and dedup

**Files:** `apps/digest/extract.py`, `tests/test_extract.py`

- `trafilatura.extract` for article text.
- Canonical URL: strip `utm_*` and tracking params, lowercase host, resolve redirects.
- `content_hash` = SHA-256 of the normalised text.
- Dates to timezone-aware UTC; missing date falls back to fetch time and is flagged in `meta`.
- Dedup by canonical URL, then content hash. Both are DB unique constraints, so the
  database enforces it, not the code.

Fuzzy title matching is **M2**, not M1. URL plus hash is enough to start.

**Acceptance:**
```bash
uv run pytest tests/test_extract.py -q
```
Tests: two URLs differing only by `utm_` collapse to one; identical text under different
URLs collapses; a 200-char stub is rejected.

---

## T1.5 — Classification

**Files:** `apps/digest/llm.py`, `apps/digest/tasks.py`,
`apps/digest/management/commands/eval_classifier.py`, `tests/test_llm.py`

1. `ollama_chat(model, prompt, schema, timeout)` — one function. `stream:false`,
   `format=schema`, `options={"temperature": 0}`.
2. Pydantic `Classification` model matching `CONTENT_SCHEMA.md`. Prompts live in
   `llm.py` as module constants and **must include the enum definitions** — M0 proved
   this is what decides accuracy.
3. Rule pre-filter before any LLM call: older than 7 days, under 400 chars, blocklisted
   domain. Log how many the rules removed.
4. Two Celery tasks on the `llm` queue:
   - `triage_article` → 8B, cheap reject
   - `classify_article` → 31B, the real decision, only for triage survivors
   Worker concurrency comes from the T0.2b measurement.
5. On Pydantic validation failure: retry once with the error appended, then mark the
   article `skipped` and continue. Never crash the batch.
6. Persist every call to `Analysis` with `model_tag`, `model_digest`, `latency_ms`.
7. `eval_classifier` command runs over `data/gold_set.jsonl` and prints precision,
   recall and a confusion matrix.

**Acceptance:**
```bash
uv run pytest tests/test_llm.py -q && uv run python manage.py eval_classifier
```
Tests pass with mocked Ollama. Eval prints **precision ≥ 0.80**.

**STOP if** precision stays below 0.80 after two prompt revisions. Report the confusion
matrix — the taxonomy may be wrong, and that is a human decision.

---

## T1.6 — Ranking and digest

**Files:** `apps/digest/ranking.py`, `apps/digest/templates/digest/*.html`,
`tests/test_ranking.py`

- Score: novelty .25, technical significance .20, evidence .20, production readiness .15,
  source credibility .10, audience relevance .10. Weights live in `settings.py`.
- Bonuses: open weights, public repo, local deployment possible.
  Penalties: announcement-only, unverified benchmark, no original source.
- Hard exclusion: maturity `announcement_only` or `paper_only` never enters a digest.
- At most 2 items per topic per digest. Maximum 7 items.
- **Never pad.** Two qualifying items means a two-item digest.
- Two Django templates: channel post (leadership) and group comment (technical).
  Escape HTML; Telegram allows a restricted tag set only.

**Acceptance:**
```bash
uv run pytest tests/test_ranking.py -q
```
Includes a snapshot test of rendered output and an explicit test that a 2-item day
produces a 2-item digest.

---

## T1.7 — Publishing

**Files:** `apps/digest/publish.py`, `tests/test_publish.py`

Publishing is a plain `httpx.post` to the Telegram Bot API. **No aiogram in M1** —
Celery tasks are synchronous and `sendMessage` is one POST. aiogram arrives in M2 with
the feedback bot, which genuinely needs long polling.

1. `POST /bot{token}/sendMessage` with `parse_mode=HTML` → store `channel_message_id`.
2. Technical appendix: the channel post is auto-forwarded to the linked group. The bot
   must be admin in both. In M1, resolve the group message id by reading updates for
   `is_automatic_forward=true`, then reply to it. Store `group_message_id`.
3. Edit and delete: two management commands taking a `DigestItem` id, calling
   `editMessageText` / `deleteMessage`.
4. **Kill switch:** if `PUBLISHING_ENABLED` is false, compose and store but send nothing.
   Default false.

No feedback buttons in M1. Buttons without a handler leave the user tapping a spinner.
Buttons, the bot process and feedback learning all land together in M2.

**Acceptance:**
```bash
uv run pytest tests/test_publish.py -q
```
Then a manual run against the test channel: post appeared, appendix appeared as a
comment under it, edit worked, delete worked, kill switch suppressed sending.

**STOP if** the appendix cannot be posted as a comment — the channel/group link is
misconfigured and that needs the human, not a workaround.

---

## T1.8 — Schedule

**Files:** `config/celery.py`, beat entries

`django-celery-beat`, timezone `Asia/Tashkent`:

| Time | Task |
|---|---|
| 08:00 | `fetch_all_sources` |
| 17:00 | `fetch_all_sources` |
| 18:00 | `triage_and_classify` |
| 19:00 | `compose_and_publish` |

- Idempotency comes from `Digest.digest_date` being unique. A second run raises
  `IntegrityError` and stops — the database enforces it, not the code.
- Article `status` makes each stage resumable: classify picks up `status='fetched'`.
- Unhandled task exception → log, then send a message to `TELEGRAM_ADMIN_CHAT_ID`.
- Task results are visible in admin via `django-celery-results`.

**Acceptance:**
```bash
uv run celery -A config worker -Q fetch,llm -c 4 --loglevel=info   # terminal 1
uv run celery -A config beat --loglevel=info                       # terminal 2
uv run python manage.py run_pipeline --date today                  # terminal 3
```
Pipeline completes end to end; `TaskResult` rows appear in admin; a second
`run_pipeline` for the same date is refused.

---

## GATE 1

- [ ] 7 consecutive days of automatic digests, no manual intervention
- [ ] The human has read all 7 and accepts the quality
- [ ] `eval_classifier` precision ≥ 0.80
- [ ] Full pipeline under 60 minutes
- [ ] Kill switch verified
- [ ] Edit and delete verified on a real post
- [ ] `ruff` and `pytest` clean
- [ ] No secret in git history

Only then does `TELEGRAM_CHANNEL_ID` move from the test channel to the public one.

---

## M2 preview

Feedback bot (aiogram) + buttons + learning · story clustering + fuzzy dedup ·
31B deep analysis and technical appendix · independent benchmark verification ·
25–40 sources · monitoring, baselines and backup · Docker deployment of all processes ·
breaking-news path.

---

## Reporting

Per task: what changed, the acceptance command, its verbatim output, any deviation and
why. Nothing else.
