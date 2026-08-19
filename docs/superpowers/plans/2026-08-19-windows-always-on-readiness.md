# Windows PC always-on production readiness — implementation plan

**Date:** 2026-08-19  
**Status:** DRAFT — implementation has not started  
**Target:** one Windows 11 PC running Docker Desktop continuously  
**Owner gates:** production Telegram writes, Windows Task Scheduler registration, power-policy
changes, restore against real data, and final cutover always require explicit owner approval.

## Goal

Make the project safe to leave running on the target PC without daily supervision. “Ready” means
more than a green unit-test suite: the application must publish without silent duplication, recover
from ordinary process failures, expose useful health state, retain restorable database backups, keep
secrets out of images/logs, and have a tested operator runbook.

This plan is implementation-grade. The executing agent must make the code and documentation changes,
run every listed check, record evidence, and stop at owner gates. It must not merely mark tasks done
because files exist or tests pass.

## Fixed decisions

1. **Clustering remains exact character 5-gram Jaccard at `0.80`.** No second clustering tier is
   added. Remove the abandoned semantic-clustering subsystem and remove approximate-Jaccard work from
   active plans and runtime comments while preserving unrelated editorial terminology.2. **Post format v2 is not accepted yet.** Repository defaults return to safe-off until the real
   measurement and test-channel gates pass. The current `POST_FORMAT_V2_ENABLED=True` cutover is not
   evidence of acceptance.
3. **The PC will not download and decode arbitrary article images.** Keep metadata extraction, validate
   the URL syntactically, and pass the public HTTP(S) URL directly to Telegram `sendPhoto`. Telegram’s
   [Bot API supports fetching a photo from a URL](https://core.telegram.org/bots/api#sendphoto). This
   removes the local SSRF/DNS-rebinding and MIME mismatch surface as well as the Pillow dependency.
4. **Ambiguous Telegram delivery is never retried automatically.** A timeout or 5xx after a send may
   mean that Telegram accepted the post. Mark it `unknown`, alert the owner, and require reconciliation.
   Only a deterministic photo rejection may fall back to a text post in the same attempt.
5. **No host resource limits are guessed.** Measure one complete pipeline run, then set limits from the
   observed peak with documented headroom.
6. **Docker Desktop’s cold-boot limitation is explicit.** The supported readiness level is unattended
   operation while the Windows user session can start Docker Desktop. Do not claim autonomous recovery
   from a reboot left at the Windows login screen unless that exact scenario is demonstrated. Do not
   enable insecure Windows auto-login as part of this plan.

## Current audited baseline

The executor must re-check these facts; they explain why the project is not ready today.

- `uv run pytest -q` passed **300 tests** on 2026-08-19, but that does not cover the host lifecycle.
- Ruff, Django system checks, and migration drift checks passed.
- `git diff --check` currently fails on trailing blank lines in seven files.
- The worktree contains many modified and untracked implementation files after commit `84b1e8f`.
- Post-format measurement has median length `749`, above the plan’s `<600` target; all ten image rows
  are `none`; there is no owner approval; at least one link anchor is not a verb.
- `POST_FORMAT_V2_ENABLED` currently defaults to true despite the original safe-off gate.
- Legacy templates, `ARCHETYPE_TEMPLATES`, feature-flag branches, and preview configuration remain.
- Image download code accepts several formats but always uploads bytes as `image.jpg` / `image/jpeg`.
- Application containers have restart policies but no meaningful app health signal, host watchdog,
  log rotation, backup/restore automation, or startup runbook.
- Compose hardcodes the PostgreSQL credential, and the Dockerfile runs as root and copies `uv:latest`.

## Definition of done

The project may be labelled **READY_FOR_ALWAYS_ON_SESSION** only when all of these are true:

- code, docs, and active plans describe one actual architecture;
- all quality commands pass, including `git diff --check`;
- post v2 passes automated constraints and signed human review on real articles;
- one real photo post and one text fallback are verified in a test channel;
- a Telegram timeout cannot trigger an automatic duplicate;
- every application process has a fresh heartbeat or health endpoint;
- Docker log rotation and measured resource limits are active;
- a scheduled backup exists and a restore into a disposable database has succeeded;
- watchdog, process-crash, Redis/PostgreSQL recovery, network outage, and reboot/login scenarios are
  tested and recorded;
- the operations runbook is executable by a person who did not write the code;
- the owner explicitly enables production publishing after reviewing the evidence.

`READY_AFTER_COLD_BOOT` is a separate, stricter label. It requires a real power-cycle test proving
Docker and the stack return without an interactive login. If Docker Desktop cannot satisfy it, record
`MANUAL_WINDOWS_LOGIN_REQUIRED`; do not hide the limitation.

## Execution rules

- Work on a dedicated `codex/always-on-readiness` branch. Preserve all current user changes; never
  use `git reset --hard`, blanket checkout, or an unreviewed stash.
- Before editing, assign every modified/untracked file to a known feature, generated artifact, or
  throwaway. `telegram_preview.html` may be removed only after confirming it is disposable.
- Use test-first changes for failure semantics, state transitions, URL validation, health checks, and
  backup script argument validation.
- Production writes remain disabled during implementation: `PUBLISHING_ENABLED=false` and
  `POST_FORMAT_V2_ENABLED=false` in the operator environment.
- Do not run a live recheck, publish, Windows registration, power change, or destructive restore under
  the name of “verification.” Stop for the named owner gate.
- Commit by coherent phase. Do not create one giant commit mixing runtime, content, and operations.
- After each task, run its focused tests. After each phase, run the complete quality gate.

## Execution order and dependencies

Tasks 0–6 are sequential. After the Task 6 owner gate, start Task 7’s observation window and continue
Tasks 8–12 while that window runs; do not leave the agent idle for 72 hours. The deletion part of Task 7
waits for its observation gate, even if the operations work is already complete. Task 13 starts only after
Tasks 7–12 have passed. Tasks that require live Telegram, Windows, reboot, or external backup writes still
pause at their individual owner gates.

## Task 0 — Freeze, inventory, and create a recoverable baseline

**Files:** no source changes except the evidence file created below.

1. Create `docs/operations/READINESS_EVIDENCE.md` with date, branch, current commit, OS/Docker versions,
   container list, enabled feature flags (values only; never tokens), and all baseline command results.
2. Record `git status --short`, `git diff --stat`, and the untracked-file list. Classify every path.
3. Confirm the local `.env` has both kill switches off. Do not print `.env`.
4. Take a database backup using a one-off safe command before migrations. Record file size and SHA-256;
   do not commit the backup.
5. Run and record:

   ```powershell
   uv sync --frozen
   uv run pytest -q
   uv run ruff check .
   uv run python manage.py check
   uv run python manage.py makemigrations --check --dry-run
   git diff --check
   docker compose config --quiet
   docker compose ps
   ```

6. Fix only whitespace errors needed for `git diff --check`; do not reformat unrelated code.

**Acceptance:** all current changes are accounted for, backup evidence exists, no secret was printed,
and the baseline failures are recorded rather than silently edited out.

## Task 1 — Reconcile old plans and remove abandoned clustering debt

**Delete:**

- the abandoned clustering implementation, probes, measurements, and proposal
**Modify:**

- `config/settings.py`, `.env.example`, `tests/test_settings.py`
- `apps/digest/clustering.py`, `tests/test_clustering.py`
- `docs/spike/DEDUP_MEASUREMENT.md`
- `docs/spike/BENCHMARK_EVIDENCE_MEASUREMENT.md`
- `docs/superpowers/plans/2026-08-19-benchmark-verification.md`
- `docs/decisions/003-m1-scope-correction.md`
- `docs/decisions/004-architecture-and-product-corrections.md`
- `docs/IMPLEMENTATION_PLAN.md`, `docs/REMAINING_WORK.md`
- any active design/plan that still makes semantic or approximate clustering a dependency

Implementation:

1. Remove the abandoned clustering settings plus their environment examples and tests.2. Remove cosine helper tests and all runtime imports/callers. Do not leave a disabled flag, placeholder
   threshold, TODO, probe, optional dependency, or “future tier” in active work.
3. Remove approximate-Jaccard recommendations from active plans, measurement conclusions, and runtime
   comments. Keep the measured exact char-5 Jaccard implementation and `0.80` threshold unchanged.
4. Rewrite the benchmark plan so corroboration uses only secondaries already produced by exact Jaccard.
   It must not wait for any deleted subsystem.
5. Update ADR-004 to record the final decision: exact Jaccard is the complete clustering architecture
   for this project’s measured volume. ADR history may say a rejected design was superseded, but must not
   present it as backlog.
6. Create/update `docs/STATUS.md` as the single current-work index:
   - artifact verification: implemented, targeted verification still required;
   - feedback bot: implemented, live test-channel smoke still required;
   - benchmark verification: implemented behind safe-off flag, corpus acceptance still required;
   - alert delivery: implemented, live admin-chat smoke still required;
   - post v2: implemented but not accepted;
   - deleted clustering proposal: rejected, not deferred.

Verification:

```powershell
uv run pytest tests/test_clustering.py tests/test_settings.py tests/test_verification.py -q
uv run ruff check apps/digest/clustering.py config/settings.py tests/test_clustering.py
rg -n "CLUSTER_JACCARD_THRESHOLD" apps config tests
```
The final `rg` must return no matches. Separately review documentation matches manually so unrelated
model architecture and natural-language glossary examples are not damaged.

**Acceptance:** there is one clustering implementation and no abandoned clustering code/config/probe.

## Task 2 — Restore the v2 safety gate and enforce the actual one-word link rule

**Modify:** `config/settings.py`, `.env.example`, local operator `.env`, `apps/digest/post_format.py`,
`apps/digest/llm.py`, `apps/digest/translation_gates.py`, `apps/digest/ranking.py`, and their tests.

1. Set repository and example defaults to `POST_FORMAT_V2_ENABLED=false`. Keep `POST_MAX_CHARS=900`.
2. Define `link_anchor` as exactly one Unicode word token from the first sentence and require it to be
   an approved Uzbek action verb. No compounds, phrases, source names, domains, `U.S`, punctuation-only
   values, or whole-lead fallback.
3. Replace substring matching with boundary-aware token matching. A request for `etdi` must not link the
   `etdi` characters inside `ketdi`.
4. Require exactly one anchor in the rendered post, located in sentence one. Keep source URL only on that
   word; do not add “Nextgov”, “GitHub”, or a separate link line.
5. If the requested anchor is invalid, select a valid action verb from sentence one deterministically.
   If none exists, reject the render and leave the item unpublished; do not silently link arbitrary text.
6. Align English editorial schema, Uzbek translation schema, prompts, validators, renderer, and tests.

Required tests include:

- `etdi` versus `ketdi` boundary regression;
- punctuation adjacent to a valid token;
- repeated same token links only the intended first occurrence;
- a multiword anchor is rejected;
- source/domain names are rejected;
- no verb produces a controlled render failure;
- exactly one `<a>` and no `<b>`, `<i>`, bullet, heading, or naked URL;
- plain-text and HTML-visible lengths both respect the 900-character limit.

```powershell
uv run pytest tests/test_post_format.py tests/test_translation_gates.py tests/test_editorial.py tests/test_ranking.py -q
```

**Acceptance:** the rule is structural and deterministic, not prompt-only.

## Task 3 — Simplify image delivery and remove local image-fetch debt

**Modify:** `apps/digest/media.py`, `apps/digest/publish.py`, `tests/test_media.py`,
`tests/test_publish.py`, `pyproject.toml`, `uv.lock`, post-format spec/plan, and settings.

1. Keep extraction of `og:image`, `twitter:image`, and their secure URL variants from already-fetched
   article HTML. Resolve relative URLs against the canonical article URL.
2. Replace `fetch_and_validate_image()` with a pure URL policy function. It performs no DNS lookup and
   no HTTP request. Accept only `http`/`https`, reject credentials, empty/invalid host, `localhost`,
   `.local`, and literal loopback/private/link-local/reserved IPs; cap URL length.
3. Change `send_photo` to send JSON/form data with the URL string as `photo`. Preserve caption, HTML parse
   mode, and feedback keyboard.
4. Remove byte upload, fake `image.jpg`/`image/jpeg`, Pillow use, image byte/format limits, SSRF downloader,
   redirect code, and corresponding settings/tests. Run `uv lock` after removing Pillow if no other
   production code needs it.
5. Never log the full image URL query string. Log item ID, host, and a bounded reason.
6. A missing/invalid image goes directly to `sendMessage` with link preview disabled.

Required tests:

- relative metadata URL resolution;
- HTTPS and HTTP public URL acceptance;
- credential/private/localhost/bad-scheme rejection without any network call;
- `sendPhoto` payload contains URL, caption, parse mode, and keyboard;
- no-image path calls only `sendMessage`;
- image URL query parameters do not appear in logs.

```powershell
uv run pytest tests/test_media.py tests/test_publish.py -q
uv run ruff check apps/digest/media.py apps/digest/publish.py tests/test_media.py tests/test_publish.py
```

**Acceptance:** the local PC never downloads or decodes article images during publishing.

## Task 4 — Make Telegram delivery state explicit and duplicate-safe

**Modify:** `apps/digest/models.py`, new migration `0007_*`, `apps/digest/publish.py`, Django admin,
management commands, and publish tests.

1. Add `DigestItem.channel_delivery_state` with `pending`, `sending`, `sent`, `unknown`, and `failed`;
   add bounded `channel_delivery_error` and `channel_delivery_attempted_at`. Backfill rows with a message
   ID to `sent`; other historical rows remain `pending` unless evidence says otherwise.
2. Before sending, lock the item row with `select_for_update()` and transition `pending -> sending`.
   Do not hold the DB transaction over the HTTP request; use a short compare-and-set transition and retain
   the existing Redis digest lock. A stale `sending` state is promoted to `unknown`, never to `pending`.
3. On a successful Telegram response, atomically store message ID, media type, and `sent`.
4. On a deterministic Bot API 400 caused by photo retrieval/format, record the rejection and send one
   text fallback. Do not fallback on authentication, chat, markup, or permission failures.
5. On timeout, connection reset, or Telegram 5xx, set `unknown`, alert admin, and skip that item on every
   automatic retry. Never send the alternate text form after an ambiguous result.
6. On 429, respect `retry_after` through Celery retry without changing media type. Ensure retry cannot
   run concurrently with the original attempt.
7. Add `manage.py reconcile_delivery ITEM_ID --message-id N --sent-as-photo yes|no` and
   `--reset-pending`. The latter requires an explicit `--i-checked-telegram` acknowledgement.
8. Admin shows state, last error, attempt time, and IDs. Editing/deleting uses the stored media type and
   refreshes state from the database.

Tests must cover success, deterministic photo fallback, 403, 429, 500, timeout, retry of `unknown`,
parallel attempts, partial digest resume, and reconciliation command validation.

**Acceptance:** every channel item is either definitely sent, definitely retryable, or explicitly
unknown; an unknown result can never create an automatic duplicate.

## Task 5 — Close A–E feature work by evidence, not file presence

No old plan may be re-executed blindly. Compare each plan’s promised behavior to current source and
tests, then add only missing verification/fixes.

1. **Artifact verification:** run targeted tests and `recheck_artifacts --dry-run`; verify 403/timeout
   leaves `artifact_verified=None`, cited repositories are not mistaken for author artifacts, and
   stranded verified papers can re-enter the pipeline. Live non-dry-run is an owner gate.
2. **Feedback bot:** test callback validation/idempotency and auto-forward handling; perform one callback
   in a test channel/group. Confirm bot polling survives a temporary Telegram/network outage.
3. **Benchmark corroboration:** remove the deleted-plan dependency, run its corpus probe on exact-Jaccard
   secondaries, manually review every promotion, and keep `BENCHMARK_VERIFICATION_ENABLED=false` until
   the report is approved. A year/version alone must never promote evidence.
4. **Alert delivery:** retain “record only after Telegram accepted it”; test 200, 400/403, timeout, missing
   token/chat, and retry-on-next-failure behavior. Send one test alert to the configured admin chat at an
   owner-approved time.
5. Mark each old plan `IMPLEMENTED + VERIFIED`, `SUPERSEDED`, or `BLOCKED` with evidence links and date.
   “Tests exist” is not sufficient evidence for live Telegram behavior.

**Acceptance:** `docs/STATUS.md` and reality agree; no plan claims work that has not passed its gate.

## Task 6 — Replace the post-format probe with an enforceable acceptance gate

**Modify:** `spikes/probe_post_format.py`, `docs/spike/POST_FORMAT_MEASUREMENT.md`; optionally add a
management command if DB access is cleaner than a standalone script.

1. Select 10 real publishable articles intentionally across at least four archetypes/topics, including:
   one valid image URL, one missing image, one very long source article, one non-English source, and one
   article whose first generated lead initially lacks a usable action verb.
2. Record per row: article ID, archetype/topic, source, requested anchor, resolved anchor, anchor token
   count, first-sentence position, visible length, HTML length, image host/status, render fallback, and
   reviewer verdict/comment.
3. Compute and enforce:
   - 100% exactly one one-word action-verb link in sentence one;
   - 100% no source label or naked URL;
   - maximum visible length `<=900`;
   - median visible length `<600`;
   - render fallback rate `<20%`;
   - at least one real test-channel `sendPhoto` and one text-only send;
   - zero duplicate messages during rerun.
4. Exit non-zero if an automated criterion fails. Never write “verified” when all images are absent.
5. Add `APPROVED_BY`, `APPROVED_AT`, test channel ID/name, Telegram message IDs, and human anchor-quality
   verdict. The script cannot self-approve.

**Owner gate:** review the ten outputs and test-channel messages. Only then may the local environment set
`POST_FORMAT_V2_ENABLED=true`.

## Task 7 — Observe v2, then remove the legacy branch

1. Enable v2 only in the test environment first. Run one full dry pipeline and one live test-channel
   publish.
2. After owner approval, enable it in production while keeping `PUBLISHING_ENABLED` under operator
   control. Observe either three successful scheduled digests or 72 hours, whichever is longer.
3. During observation, track render failures, image fallbacks, Telegram errors, unknown deliveries,
   feedback callbacks, and appendix delivery.
4. If the observation gate passes, remove:
   - six archetype item templates plus obsolete base/channel/group templates that have no caller;
   - `ARCHETYPE_TEMPLATES`, `ARCHETYPE_REQUIRED`, legacy render functions and v1-only tests;
   - `POST_FORMAT_V2_ENABLED` and its dead branch (v2 becomes the only implementation);
   - `TELEGRAM_LINK_PREVIEW` if no intentional consumer remains;
   - throwaway preview HTML and stale v1 documentation.
5. Update `docs/CONTENT_SCHEMA.md` with the actual lead/body/kicker/link-anchor schema and compatibility
   rules for historical database rows.

**Acceptance:** one post renderer and one publishing path remain. Do not delete fallback code before the
observation gate.

## Task 8 — Add runtime heartbeats and health checks

**Create/modify:** `apps/digest/health.py`, `apps/digest/views.py`, `config/urls.py`, Celery tasks/schedule,
bot lifecycle, `manage.py runtime_health`, tests.

1. Add `/healthz` for liveness and `/readyz` for DB + Redis readiness. Return compact JSON, no config or
   secret values. Liveness must not depend on external LLM/Telegram services.
2. Every minute, Celery Beat dispatches a tiny heartbeat task separately to `fetch`, `llm`, and `publish`.
   Each worker writes a queue-specific Redis key with a short TTL. This proves Beat, broker, routing, and
   each worker are alive.
3. The bot runs a lightweight background heartbeat while long polling is active.
4. `runtime_health --json --strict` checks DB, Redis, three queue heartbeats, bot heartbeat, and business
   freshness (last source fetch and expected evening pipeline result according to Asia/Tashkent schedule).
   Before the day’s scheduled time, freshness checks must be `not_due`, not failed.
5. Distinguish `healthy`, `degraded`, and `critical`; exit non-zero only for actionable unhealthy state.
6. Unit tests use frozen times around 08:00, 17:00, 18:00, and 19:00, stale/missing heartbeat cases, DB/
   Redis failure, and empty first-run database.

**Acceptance:** a stopped queue worker, stopped Beat, stopped bot, or stale daily pipeline is visible by
name within three minutes.

## Task 9 — Harden Docker for continuous local operation

**Modify/create:** `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, optionally
`compose.production.yml`, and `docs/operations/RESOURCE_BASELINE.md`.

1. Use a multi-stage build. Pin the Python base and `uv` image to explicit versions/digests; never use
   `uv:latest`. Keep compilers/headers out of the runtime image.
2. Create an unprivileged application user, use `init: true`, set graceful stop periods, and ensure only
   required writable directories exist. Build static files once instead of collecting on every web start.
3. Remove hardcoded PostgreSQL credentials and URLs. Require `POSTGRES_DB`, `POSTGRES_USER`,
   `POSTGRES_PASSWORD`, `DATABASE_URL`, and `REDIS_URL` from the operator environment using Compose
   required-variable syntax. `.env.example` contains placeholders, never working secrets.
4. Require a non-default `SECRET_KEY`, `DEBUG=false`, and explicit loopback `ALLOWED_HOSTS` in the
   always-on environment. Keep Django admin bound to loopback; do not expose port 8000 to the LAN or
   Internet. Document any TLS-only `check --deploy` warning that is intentionally inapplicable to this
   loopback-only HTTP deployment.
5. Add health checks for PostgreSQL, Redis, web readiness, and queue/bot heartbeats. Make web depend on
   both DB and Redis readiness.
6. Apply Docker log rotation to every service (`json-file`, bounded size and file count). Confirm bot
   tokens and signed image-query values cannot enter logs.
7. Keep database/Redis ports loopback-only or remove them from the production override. Expose web only
   on loopback. Use a private backend network.
8. Measure `docker stats` idle and through one complete fetch/triage/compose run. Record peaks. Set service
   memory/CPU limits with at least 50% headroom and sensible floors; rerun the pipeline and prove no OOM or
   starvation. Do not invent values before measurement.
9. Add `stop_grace_period` long enough for Celery warm shutdown and test one controlled worker restart.

Verification:

```powershell
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
docker compose exec web python manage.py runtime_health --json --strict
```

**Acceptance:** containers run non-root, logs are bounded, secrets are not hardcoded, and limits are backed
by a recorded measurement.

## Task 10 — Implement backup, restore drill, deploy, and rollback scripts

**Create:**

- `ops/windows/backup.ps1`
- `ops/windows/restore-check.ps1`
- `ops/windows/health-check.ps1`
- `ops/windows/start-stack.ps1`
- `ops/windows/deploy.ps1`
- `ops/windows/register-scheduled-tasks.ps1`
- `ops/windows/unregister-scheduled-tasks.ps1`
- Pester tests if Pester is available; otherwise deterministic `-WhatIf`/argument tests

Backup requirements:

1. Accept an explicit absolute backup root outside the repository; never derive a destructive target from
   `$HOME`, `~`, a wildcard, or an unvalidated environment variable.
2. Run PostgreSQL `pg_dump -Fc` inside the pinned database container, write to a temporary file, verify
   non-zero size and `pg_restore --list`, compute SHA-256, then atomically rename.
3. Retain 14 daily backups and 8 weekly backups. Deletion is limited to validated files inside the exact
   configured backup root and supports `-WhatIf`.
4. Never include `.env` in repository or database backups. Document separate secret recovery in a password
   manager/BitLocker-protected location with restricted ACL.
5. `restore-check.ps1` restores the newest dump into a disposable isolated database/container, runs basic
   row-count and Django checks, records duration/result, then removes only the verified disposable target.
   It must never point at the production database.

Deploy requirements:

1. Refuse a dirty or unverified tree; record the previous image/commit for rollback.
2. Keep publishing off, take a backup, build, run migrations, start/recreate services, and run strict
   health checks.
3. Never turn publishing on automatically. Print the exact owner-gated command.
4. Rollback restores the previous application image/commit first. Database rollback is allowed only with
   a migration-specific reviewed procedure; never automatically restore yesterday’s DB over live data.

**Acceptance:** a backup and disposable restore succeed twice, including once from the scheduled context.

## Task 11 — Add a bounded Windows watchdog and startup automation

1. `health-check.ps1` runs strict runtime health, writes a timestamped local operations log, and sends an
   admin Telegram alert with a cooldown. It also checks Windows free disk space, newest-backup age, and
   Docker Desktop availability. It must never print the bot token.
2. For stateless app containers only, the watchdog may restart an unhealthy service at most three times
   per hour with backoff. It must not automatically recreate/delete PostgreSQL or Redis volumes. Stateful
   failures alert and stop.
3. Register, only after owner approval:
   - stack startup at user logon, waiting for Docker Desktop readiness;
   - health check every five minutes;
   - database backup daily at 02:30 Asia/Tashkent;
   - disposable restore check weekly;
   - optional weekly status summary.
4. Registration is idempotent and has a matching unregister script. Store task names under one prefix.
5. Document and owner-verify Windows settings: Docker Desktop starts at sign-in, sleep/hibernate are off
   while plugged in, display may turn off, disk has reserved free space, and Windows updates are not
   disabled. Record whether BIOS/UEFI is configured to restore power after AC loss and whether a UPS is
   present; these are operator decisions, not assumptions. Power-policy changes are an owner gate.
6. Test a real Windows reboot. If the stack waits at login, record `MANUAL_WINDOWS_LOGIN_REQUIRED` and do
   not claim cold-boot autonomy.

**Acceptance:** ordinary app crashes recover automatically; stateful failures are visible and not handled
destructively; startup limitation is truthfully documented.

## Task 12 — Operations documentation, security pass, and codebase cleanup

**Create/update:** `README.md`, `docs/operations/WINDOWS_ALWAYS_ON_RUNBOOK.md`,
`docs/operations/INCIDENTS.md`, `docs/operations/READINESS_EVIDENCE.md`, `.gitignore`, `.dockerignore`.

The runbook must contain exact commands for first install, secret setup, preflight, build, migrations,
start/stop, logs, health, manual pipeline dry-run, test-channel publish, enabling/disabling publishing,
backup, restore drill, rollback, delivery reconciliation, source recovery, disk cleanup, token rotation,
and Windows scheduled-task removal.

Security/cleanup checklist:

- rotate any token that may have appeared in terminal history or logs;
- verify `.env`, backups, reports, previews, and generated graph artifacts are ignored;
- set restrictive Windows ACL on `.env` and backup directory;
- search tracked files and Git diff for credential-shaped values;
- remove dead templates, dead flags, obsolete probes, throwaway preview files, stale comments, and duplicate
  helpers only after their gates;
- confirm all outbound HTTP clients have explicit connect/read/write/pool timeouts and bounded retries;
- confirm admin alerts do not include secrets or full signed URLs;
- update architecture/data-flow docs and run `graphify --update` if the local graph tool is available.

**Acceptance:** a second person can operate and recover the service from the runbook without reading the
source code.

## Task 13 — Final fault-injection and cutover gate

Run these in the test channel with production publishing disabled unless the step explicitly has owner
approval. Record commands, timestamps, observed alerts, recovery time, and resulting database state.

1. Stop each app container separately; prove Docker/watchdog recovery and heartbeat freshness.
2. Stop Beat; prove all queue heartbeats become stale and an alert appears.
3. Pause Redis, then PostgreSQL; prove no data volume is deleted and recovery is clean.
4. Make the LLM endpoint unavailable; prove bounded failure, no false maturity/evidence, and no publish.
5. Make Telegram unavailable during send; prove `unknown` state and no automatic duplicate.
6. Trigger deterministic image rejection; prove one text fallback and correct `sent_as_photo=false`.
7. Publish one valid photo and one no-image item; verify one-word anchors, feedback, auto-forward appendix,
   edit, delete/reconcile behavior, and rerun idempotency.
8. Fill disk in a disposable bounded volume until warning threshold; prove alert before real host exhaustion.
9. Restore the latest scheduled backup into a disposable database and run Django checks.
10. Restart Docker Desktop and reboot Windows; record whether login is required.
11. Leave the stack for at least 72 hours and three scheduled digest cycles. Require zero unexplained
    restarts, zero duplicate posts, zero stale heartbeats, bounded logs, and successful backups.
12. Run the final quality gate:

    ```powershell
    uv sync --frozen
    uv run pytest -q
    uv run ruff check .
    uv run python manage.py check --deploy
    uv run python manage.py makemigrations --check --dry-run
    git diff --check
    docker compose config --quiet
    docker compose exec web python manage.py runtime_health --json --strict
    ```

13. Owner reviews `READINESS_EVIDENCE.md`, post-format measurement, restore report, fault-injection results,
    and current `docs/STATUS.md`.
14. **Owner gate:** set `PUBLISHING_ENABLED=true`. If v2 observation has passed and legacy cleanup is done,
    there is no v2 flag left; otherwise set it only according to Task 6 approval.

**Acceptance:** owner signs the readiness label and date. Without this final signature the status remains
`IMPLEMENTED_NOT_ACCEPTED`.

## Recommended commit sequence

1. `chore: reconcile plans and remove abandoned clustering debt`
2. `fix: enforce one-word post anchors and restore safe cutover`
3. `refactor: delegate photo fetching to Telegram`
4. `fix: make Telegram delivery ambiguity duplicate-safe`
5. `test: turn real post measurements into an acceptance gate`
6. `feat: add runtime health and queue heartbeats`
7. `chore: harden the always-on Docker stack`
8. `ops: add Windows backup restore watchdog and runbook`
9. `chore: remove accepted legacy post-format code`
10. `docs: record always-on readiness evidence`

Each commit must pass focused tests and `git diff --check`. The complete suite is mandatory before any
Docker rebuild, migration, test-channel publish, or owner gate.

## Mandatory stop conditions for the executing agent

Stop and report evidence instead of improvising when:

- a current user change cannot be confidently assigned to this work;
- a migration would lose or reinterpret existing data;
- a command would write outside the repository/explicit backup root;
- a live Telegram action, Windows setting, scheduled task, reboot, or production flag needs approval;
- a send result is ambiguous;
- backup validation or disposable restore fails;
- the post-format corpus does not meet the stated metrics;
- Docker Desktop cannot recover without login.

The agent may fix implementation defects revealed by these gates, but it may not lower the gates or mark
the project ready to make the report green.
