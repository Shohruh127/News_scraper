# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

News Radar aggregates AI/engineering news from ~23 sources, scores and clusters it, writes an
English editorial pass and an Uzbek translation with local and hosted LLMs, and publishes one
Telegram post per selected item. Django 5 + Celery + Redis + PostgreSQL 17, deployed as a Docker
Compose stack on a single server.

## Commands

The suite needs a database and a broker. Without them most of it reports `ERROR at setup`
(`psycopg ConnectionTimeout`) instead of failing — those tests never run at all, so a pass count
that looks like a result is really a count of the few tests that touch no database.

```bash
docker compose up -d postgres redis
uv run pytest -q
uv run pytest tests/test_llm.py::test_triage_and_classify_batch -q
uv run ruff check .
uv run ruff format --check .
```

Ruff is configured for line-length 100 and excludes migrations. `pytest` collects from both
`tests/` and `apps/`.

## Where work happens

All operational work happens on the server, in the project checkout. Local runs are for verifying
your own changes, never a step to hand to the operator. Deploy sequence:

```bash
git pull --ff-only
sh ops/linux/deploy.sh --allow-publishing
docker compose exec -T web python manage.py shell -c "from django_celery_beat.models import PeriodicTask; print(PeriodicTask.objects.filter(task='digest.compose_and_publish').delete())"
docker compose restart beat
sh ops/linux/health-check.sh
```

`deploy.sh` runs: clean-checkout check → compose config → DB backup → build → preflight →
`up -d` → health wait. There is no separate migrate step: the one-shot `migrate` service runs
first inside `up -d`, and every app service waits on it via `service_completed_successfully`, so
no worker can start against an out-of-date schema. Preflight sits before all of that
deliberately — a configuration error is found before the database is touched, and a failure
leaves the running stack untouched.

## Publishing is causal, not scheduled

**The most important invariant in this codebase.** `config/celery.py` schedules fetch and
triage only. `digest.compose_and_publish` has no crontab entry and must not be given one.
`triage_and_classify` calls `compose_and_publish.delay(edition=...)` when it actually finishes.

The LLM stage has no bounded duration — it depends on backlog size and GPU contention and can run
for hours. On 2026-08-21 a 09:00 publish crontab fired while classification was still working
through 265 articles: it selected nothing, published an empty digest, and the uniqueness
constraint on `(digest_date, edition)` then refused every later attempt, including the pipeline's
own. No post went out that day.

Consequences that follow from this and are easy to undo by accident:

- The triage beat entries carry **no `expires`**. The whole edition now hangs off that one
  message; discarding it because the worker was busy means nothing publishes that cycle.
- `edition` travels in the beat entry's `kwargs` and through the chain. Never re-derive it from
  the clock — a morning cycle finishing after 14:00 would publish into the evening slot.
- `publish.publish_digest` leaves a digest with zero items as `COMPOSED`. Marking it published
  burns the slot for the day.

## django_celery_beat does not prune

`CELERY_BEAT_SCHEDULER` is `DatabaseScheduler`, so the live schedule is rows in `PeriodicTask`,
not the dict in `config/celery.py`. Beat copies that dict into the database with
`update_or_create`: it adds and updates, and **never deletes rows for entries removed from the
code**. Any change that drops a schedule entry needs the row deleted by hand and beat restarted,
or the old entry keeps firing from the database.

`PeriodicTask.total_run_count` means beat *dispatched* the task. Whether a worker executed it is a
separate question, answered by `django_celery_results.TaskResult` (`CELERY_RESULT_BACKEND` is
`django-db`). A row stuck at `STARTED` is a task that began and never finished.

## LLM providers

Four stages route independently, each accepting `ollama | gateway | mimo`:

| Setting | Stage | Default |
|---|---|---|
| `LLM_PROVIDER` | global fallback | `ollama` |
| `EDITORIAL_EN_PROVIDER` | English analysis | inherits `LLM_PROVIDER` |
| `TRANSLATION_PROVIDER` | Uzbek translation | `ollama` |
| `CLASSIFIER_PROVIDER` | triage + classification | `ollama` |

- `CLASSIFIER_PROVIDER` deliberately does **not** inherit `LLM_PROVIDER`. These two stages make
  several hundred calls a day; inheriting would move that volume silently when the editorial
  provider changes. Any new provider setting must default to preserving current behaviour.
- Translation belongs on Ollama. Measured 2026-08-17: `mimo-v2.5` turned 2.4 trillion into
  2 trillion and calqued terms; `gemma4:latest` lost 0/7 numbers. The reasoning lives next to the
  settings in `config/settings.py` — read it before changing a provider.
- The Ollama tag settings name the fast and deep **tiers** for every provider: the gateway branch
  reads them to pick between its `fast`/`smart` aliases. They must stay set even when nothing
  talks to Ollama.
- The gateway addresses models by tier alias only; sending a real model name is a 404.
- `Analysis.model_tag` records the model that actually served the call, not the one requested.
  `model_digest` is only meaningful for Ollama, which is the only provider exposing `/api/tags`.

Two behaviours measured against the live gateway on 2026-08-21, neither documented in its API
guide:

- It accepts `json_schema` with `strict: true` and then wraps the answer in a markdown fence
  anyway. `_strip_code_fence` in `_openai_chat` handles that. Without it the stages that retry
  burned two calls per article and the stages that do not dropped the article outright.
- The `smart` alias is a reasoning model, and its reasoning is charged to `max_tokens` before it
  writes any answer. Too small a budget returns `finish_reason: "length"` with empty content,
  and `_openai_chat` raises a message naming the cause rather than letting an empty string reach
  `json.loads`.

Token budgets, all verified live against the gateway on 2026-08-21 with the real prompts:

| Stage | Budget | Tier | Verified |
|---|---|---|---|
| Triage | 1000 | fast | passes |
| Classification | 2000 | smart | passes, ~15-18s |
| Editorial EN | `EDITORIAL_NUM_PREDICT`, default 4000 | smart | **1500 fails**, 3000 passes, ~50-70s |
| Translation | `TRANSLATION_NUM_PREDICT`, default 2500 | fast | passes, ~25s |

The editorial budget was a hardcoded 1500, which is enough on Ollama and MiMo and empties every
article on the gateway. An unused cap costs nothing because the model stops when it is done, so
these defaults deliberately take the generous side.

## Ops scripts

`ops/linux/common.sh` derives the project root from the script's own location. Never hardcode a
deployment path; `NEWS_RADAR_PROJECT_DIR` overrides it. The systemd units keep `/opt/news-radar`
as a placeholder that `install-systemd.sh` substitutes at install time.

`deploy.sh` executes `backup.sh` and `preflight.sh` directly, so every `ops/linux/*.sh` must be
mode `100755` in git — a fresh checkout of a 644 script fails with `Permission denied`.

`preflight.sh` runs its checks inside the freshly built image via `docker compose run`, which is
why it must come after `compose build` and cannot be run standalone against a stale image. It
bootstraps Django itself (`DJANGO_SETTINGS_MODULE` + `django.setup()`); `python -` is not
`manage.py`. `--allow-publishing` downgrades only the kill-switch check to a warning.

## Configuration

`.env` is untracked and excluded from the image; `preflight.sh` verifies it never reached one.
`.env.example` is the contract for which keys exist. `ops/format-env.py` regroups a real `.env`
into commented blocks without touching any value, and normalises CRLF — a trailing CR silently
corrupts the values Compose passes through `env_file`.

The Compose project name comes from the directory name, and volume names come from the project
name. Moving or renaming the checkout gives the stack different volumes and an apparently empty
database. Pin `COMPOSE_PROJECT_NAME` in `.env` rather than relying on the directory.

## Verification

Judge a command by its own exit code. `cmd | tail` reports `tail`'s status, so a failed deploy
reads as success — capture the status directly or write to a file.

`docker compose down -v` followed by a full `deploy.sh` is the acceptance test for anything
touching migrations or startup order: it proves the stack comes up from an empty database. The
`migrate` service is one-shot and shows as `Exited (0)` in `docker compose ps` — that is success.

State plainly which paths you could not exercise. The execute-bit failure above is invisible on a
Windows checkout, and no amount of local testing would have found it.
