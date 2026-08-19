# News Radar: Windows Always-On Operations Runbook

This runbook defines the operational procedures for running News Radar 24/7 on a local Windows host with Docker Desktop, PostgreSQL, Redis, Celery, and Ollama.

---

## 1. System Architecture & Topology

```
+-------------------------------------------------------------------------------+
| Windows 11 Host (Ollama on host: http://localhost:11434)                      |
|                                                                               |
|  +--------------------------- Docker Compose -------------------------------+ |
|  | [web] (Gunicorn/Django) 127.0.0.1:8000                                   | |
|  | [worker-fetch]   (Celery queue: fetch, c=10)                              | |
|  | [worker-llm]     (Celery queue: llm, c=2)                                 | |
|  | [worker-publish] (Celery queue: publish, c=1)                             | |
|  | [beat]           (Celery Beat Scheduler)                                  | |
|  | [bot]            (Telegram Polling Bot)                                   | |
|  | [postgres]       (PostgreSQL 17 on 127.0.0.1:5433)                         | |
|  | [redis]          (Redis 7 on 127.0.0.1:6380)                              | |
|  +--------------------------------------------------------------------------+ |
|                                                                               |
|  +-------------------- Host Automation (ops/windows/) ----------------------+ |
|  | - NewsRadar-Watchdog           (health-check.ps1 every 5 min)             | |
|  | - NewsRadar-DailyBackup        (backup.ps1 daily at 02:00)                | |
|  | - NewsRadar-MonthlyRestoreDrill(restore-drill.ps1 1st of month at 03:00)  | |
|  | - NewsRadar-Startup            (startup.ps1 at user logon)                | |
|  +--------------------------------------------------------------------------+ |
+-------------------------------------------------------------------------------+
```

---

## 2. Cold-Boot and Startup Procedure

> [!IMPORTANT]
> **Windows Cold-Boot Constraint:** Docker Desktop on Windows requires an interactive user login to initialize the WSL2 VM. If the PC reboots due to a Windows Update or power cycle, log in to the Windows user account. The `NewsRadar-Startup` task will launch Docker Desktop and start all containers automatically.

To start manually:
```powershell
powershell -ExecutionPolicy Bypass -File .\ops\windows\startup.ps1
```

Or directly via Docker Compose:
```bash
docker compose up -d
```

---

## 3. Health & Liveness Endpoints

News Radar exposes three monitoring endpoints on `http://127.0.0.1:8000`:

| Endpoint | Purpose | Healthy Response | Unhealthy Status |
| :--- | :--- | :--- | :--- |
| `/healthz/` | Process liveness | `200 {"status": "ok"}` | 500 / unreachable |
| `/readyz/` | DB, Redis, & Migration schema readiness | `200 {"status": "ok", ...}` | `503 {"status": "unhealthy", ...}` |
| `/runtime-health/` | Full cluster heartbeats & pipeline freshness | `200` (or `503` if `--strict`) | `503` if degraded |

### Command-line Health Check
```bash
# General cluster health
uv run python manage.py runtime_health

# Strict check (exits with code 1 if any worker is missing or stale)
uv run python manage.py runtime_health --strict --json
```

---

## 4. Backup & Disaster Recovery

### Daily Backups
- Backups are stored in `backups/db/` as compressed `.dump` files.
- SHA256 hashes are recorded in `.sha256` sidecars.
- 14-day automated retention policy.

Manual backup trigger:
```powershell
powershell -ExecutionPolicy Bypass -File .\ops\windows\backup.ps1
```

### Restore Drill (Monthly)
The restore drill creates a disposable database (`news_radar_restore_drill`), restores the latest dump, validates row counts on `digest_article`, `digest_digest`, `digest_digestitem`, and `digest_source`, and drops the temporary DB.

Manual drill trigger:
```powershell
powershell -ExecutionPolicy Bypass -File .\ops\windows\restore-drill.ps1
```

### Full Disaster Recovery
To restore the live database from a backup:
```powershell
$dump = Get-ChildItem backups/db/backup_*.dump | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content $dump.FullName -Raw -Encoding Byte | docker compose exec -T postgres pg_restore -U news_radar -d news_radar --clean --if-exists --no-owner
```

---

## 5. Deployment and Upgrades

To deploy new code changes safely with automatic rollback:
```powershell
powershell -ExecutionPolicy Bypass -File .\ops\windows\deploy.ps1
```

The script:
1. Takes an immediate database backup.
2. Builds Docker images.
3. Applies database migrations.
4. Restarts containers with zero duplicate delivery risk.
5. Verifies `/healthz/` and `/readyz/`.
6. Automatically executes `rollback.ps1` if health verification fails.

---

## 6. Incident Troubleshooting

### Issue 1: Digest Publishing Failed / Telegram Timeout
- **Symptom:** `DigestItem` is in `unknown` state; admin received an alert.
- **Cause:** Network timeout or Telegram 5xx during `sendPhoto`. News Radar deliberately refused to auto-retry to prevent duplicates.
- **Action:**
  1. Inspect the channel to see if the message actually appeared.
  2. If it did appear with message ID `123`:
     ```bash
     uv run python manage.py reconcile_delivery --digest-id <ID> --item-id <ITEM_ID> --message-id 123
     ```
  3. If it did not appear and should be resent:
     ```bash
     uv run python manage.py reconcile_delivery --digest-id <ID> --item-id <ITEM_ID> --reset-pending --i-checked-telegram
     ```

### Issue 2: Worker Queue Degraded / Stale Heartbeat
- **Symptom:** `runtime_health` shows `worker-llm: stale` or `missing`.
- **Action:**
  ```bash
  docker compose restart worker-llm
  ```

### Issue 3: Ollama Unreachable / Local Model OOM
- **Symptom:** `worker-llm` logs `ConnectionRefusedError` or timeout on `http://localhost:11434`.
- **Action:**
  1. Verify Ollama is running on the Windows host: `ollama list`.
  2. Verify GPU memory: `nvidia-smi` (or Task Manager -> Performance -> GPU).
  3. Restart Ollama from the Windows system tray.
