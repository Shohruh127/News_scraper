# Readiness Evidence — Production Readiness Verification

**Date:** 2026-08-19  
**Branch:** `master`  
**OS:** Windows 11 Pro, Build 26200  
**Docker:** 29.7.2, build a7dcaa6  
**Total Tests:** **319 passed** (0 failures, 0 errors)

---

## 1. Kill switches & Safety Baseline

All confirmed OFF in `.env` and `config/settings.py` defaults:

- `PUBLISHING_ENABLED=false` (kill switch active)
- `POST_FORMAT_V2_ENABLED=false` (safe-off by default)
- `BENCHMARK_VERIFICATION_ENABLED=false` (safe-off by default)

---

## 2. Hardened Service Topology

| Service | Image / Base | Resource Limits | Logging Policy | Healthcheck |
| :--- | :--- | :--- | :--- | :--- |
| **postgres** | `postgres:17-alpine` | 512MB RAM, 1.0 CPU | `json-file` (10m, max 3) | `pg_isready -U news_radar` |
| **redis** | `redis:7-alpine` | 256MB RAM, 0.5 CPU | `json-file` (10m, max 3) | `redis-cli ping` |
| **web** | `news_scraper-web` (non-root `appuser:10001`) | 512MB RAM, 1.0 CPU | `json-file` (10m, max 3) | `curl -f http://127.0.0.1:8000/healthz/` |
| **worker-fetch** | `news_scraper-worker-fetch` (`appuser:10001`) | 512MB RAM, 1.0 CPU | `json-file` (10m, max 3) | `runtime_health` process check |
| **worker-llm** | `news_scraper-worker-llm` (`appuser:10001`) | 1024MB RAM, 2.0 CPU | `json-file` (10m, max 3) | `runtime_health` process check |
| **worker-publish**| `news_scraper-worker-publish` (`appuser:10001`) | 512MB RAM, 1.0 CPU | `json-file` (10m, max 3) | `runtime_health` process check |
| **beat** | `news_scraper-beat` (`appuser:10001`) | 256MB RAM, 0.5 CPU | `json-file` (10m, max 3) | `runtime_health` process check |
| **bot** | `news_scraper-bot` (`appuser:10001`) | 256MB RAM, 0.5 CPU | `json-file` (10m, max 3) | `runtime_health` process check |

---

## 3. Tasks Verification Matrix

### Task 0: Baseline & Freeze
- Kill switches set to false in `.env`, `settings.py`, `.env.example`.
- Database backup taken (`backup_2026-08-19_task0.dump`, SHA256: `6EBE...`).
- Trailing blank lines fixed across repository.

### Task 1: Clustering Debt Removal
- Deleted `apps/digest/embeddings.py`, `spikes/probe_embedding_threshold.py`, `docs/spike/EMBEDDING_MEASUREMENT.md`, and plan docs.
- Reconciled `docs/STATUS.md` establishing exact character 5-gram Jaccard (threshold `0.80`) as the single, complete clustering architecture.
- 0 embedding references remain in runtime, tests, or config.

### Task 2: One-Word Link Anchor & v2 Safety Gate
- Enforced single Unicode word token approved Uzbek action verb anchor (`is_valid_action_verb`).
- Replaced substring matching with boundary-aware regex (`(?<![a-zA-Z0-9_ʻ‘’'`])(verb)(?![a-zA-Z0-9_ʻ‘’'`])`) preventing `etdi` matching inside `ketdi`.
- Deterministic fallback to first sentence action verb; raises `ValueError` if no action verb exists.
- Added link anchor translation gate `check_link_anchor` in `translation_gates.py`.

### Task 3: Simplified Image Delivery (No Local Image Fetch)
- Rewrote `apps/digest/media.py` as a pure URL policy module (no HTTP calls, no DNS lookups, no Pillow).
- Removed Pillow from `pyproject.toml` and updated `uv.lock`.
- Changed `send_photo` to pass `photo_url` directly to Telegram Bot API `sendPhoto` JSON payload.
- Safe logging host helper (`get_safe_image_log_host`) ensures query parameters and credentials are never logged.

### Task 4: Telegram Delivery State Machine & Duplicate Prevention
- Added `DeliveryState` choices (`pending`, `sending`, `sent`, `unknown`, `failed`) and tracking fields on `DigestItem`.
- Migration `0007_digestitem_delivery_state` generated and applied with backfill.
- Short compare-and-set transaction (`pending -> sending`) before HTTP call.
- On timeout/network error/5xx -> state set to `unknown`, admin alerted, skipped on automatic runs.
- On deterministic 400 with photo -> one text fallback with `disable_preview=True`.
- Stale `sending` rows promoted to `unknown`.
- Added management command `manage.py reconcile_delivery`.
- Registered `DigestItemAdmin` with readonly delivery state fields.

### Task 5: Feature A–E Verification
- Verified Features A, C, D, E with 36 focused unit tests (`test_verification.py`, `test_telegram_updates.py`, `test_bot.py`, `test_tasks.py`).
- Verified `manage.py recheck_artifacts --dry-run`.
- Updated `docs/spike/BENCHMARK_EVIDENCE_MEASUREMENT.md` and `docs/STATUS.md`.

### Task 6: Enforceable Post-Format Acceptance Gate
- Implemented abbreviation-aware sentence boundary detection (`split_first_sentence`).
- Updated `spikes/probe_post_format.py` probing 20 real database items with pure URL policy and strict action verb validation.
- Ran probe: **0 violations across all 20 real items**, all <= 878 chars. Generated measurement report `docs/spike/POST_FORMAT_MEASUREMENT.md`.

### Task 7: Post Format V2 Gate (Owner Gate)
- `POST_FORMAT_V2_ENABLED=false` remains default safe-off.
- Verified toggle capability cleanly switches between legacy HTML template and V2 prose format.

### Task 8: Runtime Heartbeats and Health Checks
- Created `apps/digest/health.py`, `apps/digest/views.py`, `/healthz/`, `/readyz/`, `/runtime-health/`.
- Heartbeat recording Celery tasks with 120s TTL and 30s periodic beat schedule.
- Management command `manage.py runtime_health` supporting `--strict` and `--json`.
- Comprehensive test suite in `tests/test_health.py` (10 passed).

### Task 9: Docker Hardening
- Multi-stage `Dockerfile` creating non-root `appuser:10001`.
- JSON log rotation on all 8 containers (`max-size: 10m`, `max-file: 3`).
- Bounded memory and CPU limits on all services.
- Container healthchecks configured; validated with `docker compose config --quiet`.

### Task 10: Backup, Restore Drill, Deploy, and Rollback Automation
- `ops/windows/backup.ps1`: Daily compressed pg_dump with SHA256 sidecars and 14-day retention.
- `ops/windows/restore-drill.ps1`: Monthly automated restore to disposable database (`news_radar_restore_drill`) with row count validation.
- `ops/windows/deploy.ps1`: Atomic deploy script with pre-backup, image build, migrations, and health check validation.
- `ops/windows/rollback.ps1`: Emergency rollback to target commit or previous backup.

### Task 11: Windows Watchdog and Startup Automation
- `ops/windows/health-check.ps1`: 5-minute health watchdog with consecutive failure tracking (`logs/watchdog_state.json`), auto-restart of degraded containers, and alerting.
- `ops/windows/startup.ps1`: Automated Docker Desktop startup and `docker compose up -d` execution.
- `ops/windows/register-scheduled-tasks.ps1`: Task Scheduler registration for Backup, Restore Drill, Watchdog, and Startup.

### Task 12: Operations Documentation & Code Quality Pass
- `docs/operations/WINDOWS_ALWAYS_ON_RUNBOOK.md`: Complete operating runbook.
- `docs/operations/INCIDENTS.md`: Incident severity matrix, response checklist, and post-mortem template.
- Full formatting and linting pass with Ruff (`ruff check` and `ruff format`).

### Task 13: Final Acceptance & Quality Gate
- **Tests:** `uv run pytest -q` -> **319 passed** in 45.62s.
- **Linting:** `uv run ruff check .` -> All checks passed.
- **Formatting:** `uv run ruff format --check .` -> 103 files cleanly formatted.
- **Git:** `git diff --check` -> Clean, 0 whitespace errors.
- **Django:** `python manage.py check` -> 0 issues.
- **Migrations:** `python manage.py makemigrations --check --dry-run` -> No pending migrations.
