# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

News Radar aggregates AI/engineering news from ~23 sources, scores and clusters it, writes an
English editorial pass and an Uzbek translation with local and hosted LLMs, and publishes one
Telegram post per selected item. Django 6 + Celery + Redis + PostgreSQL 17, deployed as a Docker
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
sh ops/linux/update.sh --allow-publishing
```

That is the whole sequence: `update.sh` is `git pull --ff-only` followed by `deploy.sh`,
flags passed through. It used to be two commands, and before that the deploy carried a
hand-written `PeriodicTask` delete, a `restart beat` and a separate health check;
everything folded into one command for the same reason each time - a manual step in a
deploy is a step that eventually does not happen.

The delete was also wrong. It read
`PeriodicTask.objects.exclude(task__in=[...four digest task names...]).delete()`, and
`celery.backend_cleanup` — which Celery installs itself and which is not in
`app.conf.beat_schedule` — is not one of those four. Running it deleted the only thing that
trims `django_celery_results`. `prune_schedule` replaces it: it deletes rows whose task
starts with `digest.` and whose name the current schedule does not have, so Celery's own
entry is never a candidate.

`deploy.sh` runs: clean-checkout check → compose config → DB backup → build → preflight →
`up -d` → health wait → `seed_sources` → `prune_schedule` → `restart beat` →
`runtime_health`. There is no separate migrate step: the one-shot `migrate` service runs
first inside `up -d`, and every app service waits on it via `service_completed_successfully`,
so no worker can start against an out-of-date schema. Preflight sits before all of that
deliberately — a configuration error is found before the database is touched, and a failure
leaves the running stack untouched. The two management commands sit after the health gate
because both need `web` answering, and both are idempotent.

`seed_sources` runs on every deploy, so it must not overwrite an operational decision.
`enabled` is applied through `create_defaults` — set when a source is first created, never
written again — because `pipeline_stats` ends its source-yield table by telling the operator
to switch off a source that publishes nothing, and six specs ship `enabled: True`. Everything
else in a spec belongs to the code and is still updated in place.

To inspect the live schedule without changing it:

```bash
docker compose exec -T web python manage.py prune_schedule --dry-run
```

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
- `publish.publish_digest` and `publish.refresh_digest_status` leave a digest with zero items
  as `COMPOSED`. Marking it published burns the slot for the day.

## Composition is causal, emission is on a clock

Since 2026-08-24 the invariant above splits in two, and both halves matter.

**Composition stays causal.** `triage_and_classify` chains into `compose_and_publish`, which
selects, runs the editorial stage and writes six `DigestItem` rows — then stops. It does not
send the block.

**Emission is scheduled.** `digest.publish_next_item` runs every two hours and sends exactly
one item: the lowest `position` still `PENDING` or `SENDING`, from the oldest digest by
`composed_at`. A clock tick can only send an item that already exists, so the 2026-08-21
failure cannot recur through it — an empty queue posts nothing and burns no slot.

The drip entry runs on the **odd** hours, aligned to the triage entries at 08:30 and 18:00:
the first tick a freshly composed block can use is 09:00 and 19:00. On even hours a block
composed at 08:40 would wait until 10:00 with a wasted 08:00 tick behind it. Unlike triage,
this entry does carry `expires` — a dropped tick delays one post by two hours, while a
dropped triage message costs the whole edition.

`compose_and_publish` also fires `publish_next_item.delay(digest.id)` once, so item #1 lands
when the block is ready rather than waiting for the next tick.

Two rules that are easy to break by accident:

- **`FAILED` is terminal, not retryable.** `publish.TERMINAL_DELIVERY_STATES` and
  `tasks.UNFINISHED_DELIVERY_STATES` are complements and must stay that way. Counting
  `FAILED` as unfinished makes the drip re-send a permanently failed item at every tick
  forever, and the block never completes, so its roundup never fires.
- **`digest.publish_roundup` has no crontab entry and must not be given one.**
  `publish_next_item` dispatches it when a digest has nothing left pending. A scheduled
  roundup would index a block whose last post was still queued.

Digest status is derived, never stamped: `refresh_digest_status(digest)` reads the items. All
terminal with at least one `SENT` is `PUBLISHED`; all terminal with none sent is `FAILED`;
anything else stays `COMPOSED`. A block that lands five of six is `PUBLISHED` — the failure is
already visible on the item and in the admin alert, and failing the digest invites a re-run
that reposts the five that worked.

## The post

```
<b>headline_uz</b>      label, <= 8 words, not a sentence
lead_uz                 1 sentence, exactly one <a> on the tail, <= 18 English words
body_1_uz               1 sentence, the most specific verifiable fact, <= 20 English words
kicker_uz               1 sentence, <= 8 words
#tag                    one approved topic hashtag
```

`POST_MAX_SENTENCES` is 3 and counts only the three sentence fields; the headline and hashtag
lines are labels. `POST_MAX_CHARS` is 500 and is a runaway guard only — structure bounds the
length, and the binding limit is Telegram's 1024-character cap on a photo caption.

**`body_2` was removed on 2026-08-24.** It was always the first thing trimmed, and the model
could not tell it apart from `body_1` — in a live test its "cause or context" sentence turned up
in `body_1` instead. Generating a field in order to discard it costs output tokens on two calls.

**Length is capped in English words, never in characters against another channel.** The word
caps sit on `lead_en` and `body_1_en`, where an English word count means something. Uzbek
agglutinates — it folds prepositions into suffixes — so a character or word budget calibrated on
the Russian reference channel measures nothing here. That mistake was made once and reversed.

**Bold is positional.** The renderer wraps the headline line and nothing else; the model
returns plain text for every field and `strip_markdown_formatting` removes any markup it emits
anyway. Bold was removed once because the model applied it to the wrong words — the same
failure the link anchor had, and the same fix: take the choice away from the model.

The headline and the kicker are **never trimmed**, and `body_1` is the only trimmable field.
They are the two elements the reference channel (`@naebnet`, measured 2026-08-24: a closing
sentence in 100% of posts) always carries and this channel had lost. Take from that channel its
*structure*, not its lengths.

## django_celery_beat does not prune

`CELERY_BEAT_SCHEDULER` is `DatabaseScheduler`, so the live schedule is rows in `PeriodicTask`,
not the dict in `config/celery.py`. Beat copies that dict into the database with
`update_or_create`: it adds and updates, and **never deletes rows for entries removed from the
code**. Any change that drops a schedule entry needs the row deleted by hand and beat restarted,
or the old entry keeps firing from the database.

`PeriodicTask.total_run_count` means beat *dispatched* the task. Whether a worker executed it is a
separate question, answered by `django_celery_results.TaskResult` (`CELERY_RESULT_BACKEND` is
`django-db`). A row stuck at `STARTED` is a task that began and never finished.

That table only answers the question because `CELERY_RESULT_EXTENDED` is on. Without it a row
carries an id, a status and a return value, and `task_name`, `task_args`, `task_kwargs` and
`worker` all read `None` — you can see that something ran and not what. Measured 2026-08-26,
while an unexplained `compose_and_publish` on `worker-publish` could not be attributed.

## LLM providers

Everything runs on the internal gateway. The direct Ollama path was **removed on 2026-08-25**:
the gateway fronts the same local GPU models — `fast` is the 8B, `smart` the 31B — so the second
client bought nothing, and its model tags had quietly become the tier vocabulary for providers
that never spoke to it.

Stages route independently, each accepting `gateway | mimo`:

| Setting | Stage | Default |
|---|---|---|
| `LLM_PROVIDER` | global fallback | `gateway` |
| `EDITORIAL_UZ_PROVIDER` | single-stage Uzbek editorial | inherits `LLM_PROVIDER` |
| `CLASSIFIER_PROVIDER` | triage + classification | `gateway` |

- **The tier is said out loud.** `llm.TIER_FAST` / `llm.TIER_DEEP` are the only way a caller
  names a speed tier; `_model_for(provider, tier)` turns that into `fast`/`smart` for the gateway
  and `MIMO_FAST_MODEL`/`MIMO_DEEP_MODEL` for MiMo. Before this, passing the string
  `gemma4:latest` was how every provider was told "fast" — which is why the Ollama settings had
  to stay set even when nothing used them.
- `CLASSIFIER_PROVIDER` deliberately does **not** inherit `LLM_PROVIDER`. These two stages make
  several hundred calls a day; inheriting would move that volume silently when the editorial
  provider changes. Any new provider setting must default to preserving current behaviour.
- **Uzbek editorial runs on the deep tier.** One call writes reader-facing Uzbek and extracts the
  technical block directly.
- The gateway addresses models by tier alias only; sending a real model name is a 404.
- **`Analysis.model_digest` is now always empty, and `model_tag` records the tier alias, not the
  model.** Only Ollama exposed `/api/tags`. The gateway can repoint an alias silently — that is
  its purpose — and nothing in the database will show it happened. Historical rows still carry
  real digests.
- **The gateway has a capacity ceiling.** Measured 2026-08-25: `smart` returned
  `503 overloaded — "Model 'smart' is at capacity and the queue wait timed out"`, and the tenacity
  retry burned ~128s over four attempts before giving up, losing the article's editorial. MiMo is
  kept configured as a second provider precisely because it is a different machine; a stage can
  be moved there by changing one setting when the gateway is saturated.
- MiMo's Token Plan forbids automated/backend use, so it is not the default anywhere. See
  ADR-004 §5.

Two behaviours measured against the live gateway on 2026-08-21, neither documented in its API
guide:

- It accepts `json_schema` with `strict: true` and then wraps the answer in a markdown fence
  anyway. `_strip_code_fence` in `_openai_chat` handles that. Without it the stages that retry
  burned two calls per article and the stages that do not dropped the article outright.
- The `smart` alias is a reasoning model, and its reasoning is charged to `max_tokens` before it
  writes any answer. Too small a budget returns `finish_reason: "length"` with empty content,
  and `_openai_chat` raises a message naming the cause rather than letting an empty string reach
  `json.loads`.

## Triage reads the headline, not the article

Changed 2026-08-25. Triage used to request the full `CLASSIFICATION_SCHEMA` — topic, maturity
and three 1-10 scores — from 8000 characters of article body, then decide on
`primary_topic == irrelevant or all three scores < 3`. It now sends the title and source only
and asks one question, `TRIAGE_SCHEMA`: `{relevant: bool, reason: str}`.

Measured on the 26-row gold set, both runs interleaved against the same gateway so load is
not the variable:

| | Old (8000 chars) | New (title only) |
|---|---|---|
| **Recall** | **1.00** | **1.00** |
| Precision | 0.43 | 0.42 |
| Rejected of 16 drops | 3 | 2 |
| Input tokens per article | ~2 134 | ~200 |
| Latency per article | 18 163 ms | **8 356 ms** |

Recall is the number that governs here, and it did not move. **Triage is a recall-first
gate**: a false positive costs one classification call, a false negative loses the article
entirely. The prompt says so to the model in as many words, and it is why the instruction on
an ambiguous headline is "answer relevant=true".

Two things that follow:

- The scores are gone from triage on purpose. Novelty, evidence and production_readiness
  cannot be derived from a headline; asking for them produced numbers the old rule then acted
  on. They are still computed at classification, over the full text.
- `num_predict` for triage is **1000**, not the ~40 tokens the answer needs. Measured
  2026-08-25: at 200 the `fast` alias returned `finish_reason: "length"` with empty content on
  all 26 rows. It charges its own reasoning to `max_tokens` before writing anything, exactly
  as `smart` does — the trap CLAUDE.md already documented for the editorial stage. The budget
  is a cap, not a cost; the saving is entirely on the input side.

Token budgets, all verified live against the gateway:

| Stage | Budget | Tier | Verified |
|---|---|---|---|
| Triage | 1000 | fast | passes; 200 fails — see the triage section above |
| Classification | 2000 | smart | passes, ~15-18s |
| Uzbek Editorial | `EDITORIAL_NUM_PREDICT`, default 5000 | smart | 4000 fails on long articles, 5000 passes, ~50-80s |

The editorial budget was raised from 4000 to 5000 on 2026-08-26 to accommodate the single-stage
prompt and reasoning tokens on the gateway. An unused cap costs nothing because the model stops
when it is done.

## Token accounting

Added 2026-08-25. Until then nothing recorded what a call cost: `Analysis` stored
`latency_ms` and the provider's `usage` block was read by nothing, so "what does a post
cost" and "which stage spends the budget" were unanswerable.

`Analysis.input_tokens` / `output_tokens` hold what the provider reported. Both are
**nullable, not `default=0`** — NULL means the row predates the column or the provider sent
no usage block, and zero-filling would make an unmeasured call look free and understate every
total above it. `pipeline_stats` reports the unmeasured count and says its totals are a floor.

Every chat call returns a `ChatResult` (payload, latency_ms, model_tag, input_tokens,
output_tokens), and every `Analysis` row is written through `_record_analysis`, so a new stage
cannot forget to record its cost. **Retries are folded in by `_combine`**: a stage that
validated, failed, and retried made two calls, and recording only the second one hides the
retry from the accounting entirely.

Two commands read this:

```bash
docker compose exec -T web python manage.py pipeline_stats --days 7
docker compose exec -T web python manage.py eval_triage_replay --days 30 --limit 200
```

`pipeline_stats` gives the funnel, spend per stage, tokens per published post, and per-source
yield — the last of which answers which sources are spending triage calls and never reaching
the channel. Disable those on `Source.enabled` rather than filtering them later.

`eval_triage_replay` measures the triage gate against a stronger label than the 26-row gold
set: every article that reached classification carries the deep tier's verdict, produced after
reading the whole article. The replay runs today's triage on the title alone and compares.
It costs one fast call per article, so bound it with `--limit`.

Its one structural limit is printed in its own output and is worth repeating: articles the
*old* triage dropped were never classified, carry no label, and cannot appear. The replay
measures the gate on the population that reaches classification today.

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
