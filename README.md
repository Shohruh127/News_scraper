# News Radar

**News Radar** is an automated, AI-driven tech news aggregation, verification, and digest generation system that curates, scores, translates, and publishes high-signal engineering and AI news directly to a Telegram channel.

---

## Architecture Overview

```
+-----------------------------------------------------------------------------------+
| SOURCES                                                                           |
| [Official Blogs] [Tech Media] [Research Papers] [GitHub Releases]                 |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| INGESTION & PIPELINE (Celery Workers)                                             |
|                                                                                   |
|  1. FETCH (worker-fetch): Canonical deduplication, HTML extraction                |
|  2. CLUSTER: Exact character 5-gram Jaccard deduplication (threshold 0.80)        |
|  3. TRIAGE & CLASSIFY (worker-llm): Fast triage & deep classification (Ollama)    |
|  4. EDITORIAL & TRANSLATION (worker-llm): Prose synthesis with Uzbek glossary     |
|  5. PUBLISH (worker-publish): Idempotent Telegram Bot API delivery               |
|  6. GROUP FORWARD BOT (bot): Tracks channel-to-group message forwards          |
+-----------------------------------------+-----------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
| STORAGE & SERVICES                                                                |
| [PostgreSQL 17]  [Redis 7]  [Django Web & Health Endpoints]  [Celery Beat]        |
+-----------------------------------------------------------------------------------+
```

---

## Key Features

- **Multi-Source Aggregation:** Pulls from RSS, Atom, Substack, and HTML listings with feed-level deduplication and rate-limited error backoff.
- **Story Identity Clustering:** Exact character 5-gram Jaccard matching ($\ge 0.80$) groups corroborating articles across different sources without heavy semantic embeddings.
- **Two-Stage LLM Pipeline:**
  - **Triage & Classification:** Evaluates technical depth, novelty, and production-readiness on local Ollama models.
  - **Editorial Stage:** Synthesizes structured Uzbek summaries with strict anchor-verb link verification (`is_valid_action_verb`).
- **Idempotent Telegram Publishing:**
  - Strict delivery state machine (`pending` $\rightarrow$ `sending` $\rightarrow$ `sent` / `unknown` / `failed`).
  - Network timeouts and 5xx errors transition to `unknown` and notify the admin without duplicate retries.
  - Direct public image URL delegation to Telegram's `sendPhoto` API without local SSRF risk or Pillow dependency.
- **Group Forward Bot:** Native polling bot tracking channel-to-group message forwards for technical appendix delivery.
- **Production Observability & Monitoring:**
  - `/healthz/` (process liveness).
  - `/readyz/` (DB, Redis, and schema migration readiness).
  - `/runtime-health/` (worker heartbeats with 120s TTL and cluster status).

---

## Tech Stack

- **Backend:** Python 3.13, Django 5.x, Gunicorn
- **Task Queue:** Celery, Redis 7
- **Database:** PostgreSQL 17
- **Bot & API:** Aiogram 3.x, Telegram Bot API
- **Package Management:** `uv`
- **Containerization:** Multi-stage Docker (`appuser:10001` non-root), Docker Compose

---

## Quickstart

### Prerequisites
- Python 3.13+ and [uv](https://github.com/astral-sh/uv)
- Docker and Docker Compose
- Running [Ollama](https://ollama.com/) instance (with models e.g., `gemma4:latest`, `gemma4:31b`)

### 1. Clone and Environment Setup
```bash
git clone https://github.com/Shohruh127/News_scraper.git
cd News_scraper

cp .env.example .env
# Edit .env with your PostgreSQL credentials, Telegram bot tokens, and Ollama URL
```

### 2. Local Python Environment
```bash
uv sync
uv run python manage.py migrate
uv run python manage.py check
```

### 3. Run with Docker Compose
```bash
docker compose up -d
```

Check cluster health:
```bash
curl http://127.0.0.1:8000/healthz/
curl http://127.0.0.1:8000/readyz/
docker compose exec web uv run python manage.py runtime_health
```

---

## Operational Commands

### Manual Delivery Reconciliation
When an ambiguous Telegram network error occurs (`unknown` state):
```bash
# Record message ID if message appeared on Telegram:
uv run python manage.py reconcile_delivery --digest-id <ID> --item-id <ITEM_ID> --message-id <MSG_ID>

# Reset to pending for retry if message did not appear:
uv run python manage.py reconcile_delivery --digest-id <ID> --item-id <ITEM_ID> --reset-pending --i-checked-telegram
```

### Database Backup & Restore Drills
```powershell
# Windows Host
powershell -ExecutionPolicy Bypass -File .\ops\windows\backup.ps1
powershell -ExecutionPolicy Bypass -File .\ops\windows\restore-drill.ps1
```

---

## Testing & Quality Gates

Run the full automated test suite:
```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

---

## License

Private repository. All rights reserved.
