# ADR-001 — Django + Celery + Celery Beat instead of a minimal Python stack

Date: 2026-08-14
Status: **Accepted**
Supersedes: the stack table in `PROJECT_PLAN.md` §7 (v1)

## Context

The original plan chose the smallest stack that could produce a daily digest:
SQLAlchemy 2 + Alembic + APScheduler + Typer, with `config/sources.yaml` as the source
registry and no web layer at all. Celery, Redis and Django were listed under
"deliberately not used", on the grounds that four batch jobs per day do not justify a
distributed task queue.

That reasoning was correct about throughput and wrong about what actually determines
whether this project ships.

## Decision

Use **Django + Celery + Celery Beat + Redis**.

Django is used strictly as: ORM, migrations, settings, admin, and management commands.
**No DRF, no views, no templates beyond admin, no frontend.** Telegram remains the
user-facing interface.

## Reasons

### 1. Developer familiarity is the dominant risk factor

`PROJECT_PLAN.md` §10 lists "project stalls halfway" with probability **high** and
impact **high** — the single worst entry in the risk table.

The developer already runs Django + Celery + Redis in production in
`D:\IMV_IB_Support`, with working Docker Compose, healthchecks, settings management
and deployment patterns to copy. A stack of four unfamiliar libraries
(SQLAlchemy 2, Alembic, APScheduler, Typer) increases the dominant risk in order to
reduce a cost — process count — that was never the constraint.

### 2. Celery queues express the two concurrency budgets natively

The design requires two independent concurrency limits: network fetching (bounded by
politeness and I/O) and Ollama inference (bounded by the server, measured in M0.1).
The minimal stack would implement this with two hand-built `asyncio.Semaphore`
instances. Celery expresses it as configuration:

```python
task_routes = {
    "ingest.fetch_source": {"queue": "fetch"},   # worker -c 10
    "llm.classify":        {"queue": "llm"},     # worker -c <measured>
}
```

Per-task `rate_limit` additionally gives per-host politeness without custom code.

### 3. Django admin is the operator interface the plan was missing

The original plan said "Telegram is the frontend, no web UI needed". That is correct
for **users** and wrong for **operators**.

The source-failure policy (see ADR-002) is *alert, never auto-disable*, which means a
human reviews degraded sources by hand. That review needs somewhere to happen.

| Task | `sources.yaml` | Django admin |
|---|---|---|
| Add a source | edit file, redeploy | fill a form |
| Disable a source temporarily | edit file, redeploy | untick a checkbox |
| See `consecutive_failures` | write SQL | column in list view |
| Find degraded sources | write SQL | filter |
| Inspect a stored LLM analysis | write SQL | detail view |

The admin arrives free with the ORM that is being adopted anyway.

### 4. Celery Beat survives process death better than APScheduler

APScheduler runs inside the application process. If that process dies before 19:00,
no digest is published and the failure is discovered the next day. With Beat, the
schedule lives in the database (`django-celery-beat`), the queue lives in Redis, and a
worker restart resumes rather than loses work.

## Costs accepted

1. **Process count rises from 2 to 5**: django, celery-worker, celery-beat, redis,
   postgres. Debugging moves from "call the function in a terminal" to "read the worker
   log" — a real cost, mitigated by the developer already working this way daily.
2. **No asyncio.** Celery tasks are synchronous. At 8–40 sources this costs nothing
   measurable; synchronous `httpx` with Celery concurrency 10 is sufficient. Async is
   removed from the design rather than bridged, to avoid mixing execution models.
3. **Redis becomes a hard dependency.** It is already running on the workstation and is
   a known quantity for this developer.

## Unchanged

- **Pydantic stays** for LLM structured-output validation. This is orthogonal to the ORM.
- `httpx`, `trafilatura`, `feedparser`, `tenacity`, `aiogram`, `jinja2` — all retained.
- The Ollama pipeline design and model routing.
- The three-milestone structure and all gates.
- Every M0 measurement remains valid; M0 does not depend on the application stack.

## Explicitly still excluded

Django REST Framework · Django templates and views beyond admin · React/Vue ·
Kubernetes · vector database · LangChain · model fine-tuning · microservices.

Adopting Django is not permission to adopt the rest of a typical Django project.
