# Runtime Config and Served Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver configuration to containers at run time instead of baking it into the image, and serve the Django admin so every pipeline stage can be inspected from a browser.

**Architecture:** `.dockerignore` keeps `.env` out of the image; `env_file:` in compose injects the same values into each container's environment, where `django-environ` already falls back to `os.environ`. A `web` service runs gunicorn with WhiteNoise serving the admin's static files.

**Tech Stack:** Docker Compose, Django 6.0, gunicorn, WhiteNoise, `uv`.

**Spec:** none — this is a bounded change agreed in chat on 2026-08-18. The defect it fixes is recorded in `docs/REMAINING_WORK.md` §4 Phase 1.

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- The full suite must stay green: `uv run pytest -q` → currently **134 passed**
- Never commit `.env`, a token, or the Ollama LAN address
- `DATABASE_URL` and `REDIS_URL` stay in each service's `environment:` block — inside the compose network the hosts are `postgres` and `redis`, which is not what the host `.env` says
- Bind published ports to `127.0.0.1`, matching the existing postgres and redis entries
- One Django app, functions over classes, no abstraction before the second case

---

## Why this change exists

`Dockerfile` ends with `COPY . .` and no `.dockerignore` exists, so `.env` is copied into the image. `docker-compose.yml` passes only `DATABASE_URL` and `REDIS_URL` through `environment:`, so every other setting is read from the baked copy at `/app/.env`.

Measured on 2026-08-18:

```
host .env  token fingerprint 36f118160c69   PUBLISHING_ENABLED=true
container  token fingerprint 128389856442   PUBLISHING_ENABLED=False
/app/.env  797 bytes, dated 04:35 UTC       (the image build time)
```

Three consequences, in order of severity:

1. A rotated bot token never reached the workers. They kept using a revoked one.
2. The kill switch cannot be flipped. The running image carries `PUBLISHING_ENABLED=false`, so a scheduled run would complete and publish nothing while `.env` reads `true`.
3. The token sits in an image layer and travels with the image to any host or registry.

`docker compose up -d --force-recreate` does **not** fix this. The values live in the image, so only a rebuild changes them — which is the defect, not the workaround.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `.dockerignore` | keep secrets and local state out of the build context | create |
| `docker-compose.yml` | deliver config at run time; run the admin | modify |
| `pyproject.toml` | gunicorn and whitenoise | modify |
| `config/settings.py` | WhiteNoise middleware | modify |
| `apps/digest/admin.py` | make the pipeline stage visible and filterable | modify |

### Context an engineer new to this repo needs

`config/settings.py` calls `environ.Env.read_env(BASE_DIR / ".env")` and then reads every setting through `env(...)`. Verified on 2026-08-18: when that file is absent `read_env` logs and returns without raising, and `env(...)` falls back to `os.environ`. So removing `.env` from the image is safe as long as compose supplies the same variables — which is exactly what `env_file:` does.

The admin already registers `Source`, `Article`, `Analysis`, `Digest` and `Feedback`, a superuser already exists, and `django_celery_results` and `django_celery_beat` bring their own admin pages for task history and the schedule. Very little admin work is needed; the gap is that nothing serves HTTP.

`STATIC_ROOT` is already `BASE_DIR / "staticfiles"` and `DEBUG` defaults to `False`, which is why the admin needs WhiteNoise or its CSS will not load.

---

## Task 1: Deliver configuration at run time

**Files:**
- Create: `.dockerignore`
- Modify: `docker-compose.yml` — the four `build: .` services

**Interfaces:**
- Consumes: nothing
- Produces: containers whose settings come from the host `.env` at start time. Task 2's `web` service relies on this and adds `env_file:` the same way.

- [ ] **Step 1: Create `.dockerignore`**

```
.env
.env.*
!.env.example
.git
.gitignore
.venv
__pycache__/
*.pyc
.pytest_cache
.ruff_cache
data/
staticfiles/
graphify-out/
docs/
spikes/
```

`docs/` and `spikes/` are excluded because they are large and the running application never reads them. `data/` holds the gold set and local artefacts.

- [ ] **Step 2: Verify the secret is now out of the build context**

```bash
docker compose build worker-publish
docker compose run --rm --no-deps worker-publish ls -la /app/.env
```

Expected: `ls: cannot access '/app/.env': No such file or directory`

If `.env` is still there, the `.dockerignore` is not being picked up — it must sit next to `docker-compose.yml` in the build-context root.

- [ ] **Step 3: Add `env_file:` to the four build services**

In `docker-compose.yml`, each of `worker-fetch`, `worker-llm`, `worker-publish` and `beat` currently reads:

```yaml
  worker-fetch:
    build: .
    command: uv run celery -A config worker -Q fetch -c 10 --loglevel=info
    environment:
      DATABASE_URL: postgresql://news_radar:news_radar@postgres:5432/news_radar
      REDIS_URL: redis://redis:6379/0
```

Add one line to each, directly above `environment:`:

```yaml
    env_file:
      - .env
```

so the block becomes:

```yaml
  worker-fetch:
    build: .
    command: uv run celery -A config worker -Q fetch -c 10 --loglevel=info
    env_file:
      - .env
    environment:
      DATABASE_URL: postgresql://news_radar:news_radar@postgres:5432/news_radar
      REDIS_URL: redis://redis:6379/0
```

Apply the identical two lines to `worker-llm`, `worker-publish` and `beat`. Keep `environment:` after `env_file:` — compose gives `environment:` the higher precedence, which is what keeps the in-network `postgres` and `redis` hostnames from being overwritten by the host values in `.env`.

- [ ] **Step 4: Recreate and confirm the containers now match the host**

```bash
docker compose up -d --force-recreate worker-fetch worker-llm worker-publish beat
docker compose exec -T worker-publish uv run python -c "
import os, django, hashlib
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from django.conf import settings
print('token fingerprint:', hashlib.sha256(settings.TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:12])
print('PUBLISHING_ENABLED:', settings.PUBLISHING_ENABLED)
print('DATABASE_URL host :', settings.DATABASES['default']['HOST'])
"
```

Expected: the token fingerprint equals the one computed from the host `.env`, `PUBLISHING_ENABLED` equals the host value, and the database host is `postgres` — proving `environment:` still wins over `env_file:`.

To compute the host fingerprint for comparison:

```bash
uv run python -c "
import hashlib
for line in open('.env', encoding='utf-8'):
    if line.startswith('TELEGRAM_BOT_TOKEN='):
        print(hashlib.sha256(line.split('=',1)[1].strip().strip('\"').encode()).hexdigest()[:12])
"
```

Never print the token itself.

- [ ] **Step 5: Prove a config change lands without a rebuild**

This is the acceptance check for the whole task. It is the test the current setup fails.

```bash
uv run python - <<'PY'
import io
p = ".env"; s = io.open(p, encoding="utf-8").read()
io.open(p, "w", encoding="utf-8").write(s + "\nDIGEST_MAX_ITEMS=99\n")
PY
docker compose restart worker-publish
docker compose exec -T worker-publish uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from django.conf import settings; print('DIGEST_MAX_ITEMS:', settings.DIGEST_MAX_ITEMS)
"
```

Expected: `DIGEST_MAX_ITEMS: 99` — with **no `docker compose build` anywhere in this step.**

Then restore:

```bash
uv run python - <<'PY'
import io
p = ".env"; s = io.open(p, encoding="utf-8").read()
io.open(p, "w", encoding="utf-8").write(s.replace("\nDIGEST_MAX_ITEMS=99\n", "\n"))
PY
docker compose restart worker-publish
docker compose exec -T worker-publish uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from django.conf import settings; print('DIGEST_MAX_ITEMS restored to:', settings.DIGEST_MAX_ITEMS)
"
```

Expected: `DIGEST_MAX_ITEMS restored to: 15`

- [ ] **Step 6: Commit**

```bash
git add .dockerignore docker-compose.yml
git commit -m "Deliver configuration at run time instead of baking it into the image"
```

---

## Task 2: Serve the Django admin

**Files:**
- Modify: `pyproject.toml` — dependencies
- Modify: `config/settings.py` — one middleware line
- Modify: `docker-compose.yml` — a `web` service
- Modify: `apps/digest/admin.py:74-86` — `AnalysisAdmin`

**Interfaces:**
- Consumes: the `env_file:` pattern from Task 1
- Produces: the admin at `http://127.0.0.1:8000/admin/`. No Python callable that other code imports.

- [ ] **Step 1: Add the two dependencies**

In `pyproject.toml`, inside `dependencies = [ ... ]`, keeping the list alphabetical:

```toml
    "gunicorn>=26.0.0",
```

goes after `"feedparser>=6.0.14",` and

```toml
    "whitenoise>=6.12.0",
```

goes after `"trafilatura>=2.2.0",`.

Then:

```bash
uv sync
```

- [ ] **Step 2: Add the WhiteNoise middleware**

In `config/settings.py`, `MIDDLEWARE` currently begins:

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
```

Insert one line so it becomes:

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves the admin's own CSS and JS straight from gunicorn. DEBUG is False here, so
    # without this the admin renders as unstyled HTML and nothing explains why.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
```

The position is required: WhiteNoise must come directly after `SecurityMiddleware` and before everything else.

- [ ] **Step 3: Add the `web` service**

In `docker-compose.yml`, add this service after `beat` and before the `volumes:` block at the bottom:

```yaml
  web:
    build: .
    command: >
      uv run sh -c "python manage.py collectstatic --noinput &&
      gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --access-logfile -"
    ports:
      - "127.0.0.1:8000:8000"
    env_file:
      - .env
    environment:
      DATABASE_URL: postgresql://news_radar:news_radar@postgres:5432/news_radar
      REDIS_URL: redis://redis:6379/0
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped
```

It depends only on postgres — the admin reads the database and does not need Redis to start.

- [ ] **Step 4: Show the pipeline stage in the admin**

`apps/digest/admin.py` currently has:

```python
@admin.register(Analysis)
class AnalysisAdmin(admin.ModelAdmin):
    list_display = ("article", "model_tag", "topic", "maturity", "latency_ms", "created_at")
    list_filter = ("model_tag",)
```

Replace those two lines with:

```python
@admin.register(Analysis)
class AnalysisAdmin(admin.ModelAdmin):
    #: `stage` is the column that makes this table readable as a pipeline: one article
    #: carries a triage row, a classification row and two editorial rows, and without it
    #: they are four near-identical lines.
    list_display = ("article", "stage", "model_tag", "topic", "maturity", "latency_ms",
                    "created_at")
    list_filter = ("stage", "model_tag")
```

Leave the rest of the class untouched.

- [ ] **Step 5: Build and start**

```bash
docker compose up -d --build web
docker compose logs web | tail -20
```

Expected: `collectstatic` reports copied files, then gunicorn lines showing `Listening at: http://0.0.0.0:8000`.

- [ ] **Step 6: Verify the admin answers and is styled**

```bash
curl -s -o /dev/null -w "login page: %{http_code}\n" http://127.0.0.1:8000/admin/login/
curl -s -o /dev/null -w "admin css : %{http_code}\n" http://127.0.0.1:8000/static/admin/css/base.css
```

Expected:

```
login page: 200
admin css : 200
```

A `404` on the CSS means either `collectstatic` did not run or the WhiteNoise middleware is in the wrong position. A `500` on the login page usually means `ALLOWED_HOSTS` — it defaults to `127.0.0.1` and `localhost`, so use one of those in the URL.

- [ ] **Step 7: Confirm the stages are visible**

Open `http://127.0.0.1:8000/admin/digest/analysis/` in a browser and confirm the `stage` column appears. The filter lists five values: `triage`, `classification`, `editorial_en`, `editorial_uz`, and the legacy `editorial` kept so old rows still validate.

Then confirm the two pages that make a failure diagnosable:

- `/admin/digest/digest/` — a `failed` digest is visible with its item count
- `/admin/django_celery_results/taskresult/` — every Celery run with its status and traceback

- [ ] **Step 8: Run the suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `134 passed` and `All checks passed!`. This task adds no tests: it changes deployment wiring and one admin display, neither of which the suite covers. If a test fails here, the middleware line is in the wrong place — report it rather than adjusting the test.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock config/settings.py docker-compose.yml apps/digest/admin.py
git commit -m "Serve the Django admin so each pipeline stage can be inspected"
```

---

## Not in this plan

**Migrations are still run by hand.** No service runs `migrate`, and this plan does not add one. Putting it in the `web` service would mean a schema change lands whenever the admin restarts, which is a deployment decision the owner has not made.

**No authentication in front of the admin.** The port binds to `127.0.0.1`, so it is reachable only from the host. When this moves to the shared server that hosts Ollama, exposure becomes a real question and needs its own decision.

**Secrets remain in a file.** Keeping `.env` out of the image is the fix for this phase. A secrets manager is a different scale of change and this project does not need it yet.

---

## Self-review

**Coverage of the agreed design**

| Agreed in chat | Task |
|---|---|
| `.dockerignore` so the secret leaves the image | Task 1 Steps 1–2 |
| `env_file:` so config arrives at run time | Task 1 Step 3 |
| `web` service with gunicorn and collectstatic | Task 2 Steps 1–3, 5 |
| Admin columns making each stage legible | Task 2 Step 4 — smaller than proposed, because `model_tag`, `topic`, `maturity` and `latency_ms` were already there. Only `stage` was missing |
| Acceptance: a config change without a rebuild | Task 1 Step 5 |

**Placeholder scan:** none. Every step carries its command or its code.

**Type consistency:** no new Python callables are introduced. `stage` is an existing field on `Analysis`, already used in queries elsewhere in the codebase, so naming it in `list_display` and `list_filter` cannot drift.
