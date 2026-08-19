"""Runtime health checks and heartbeats for News Radar.

Provides:
- /healthz (process liveness)
- /readyz (readiness: DB, Redis, migrations)
- check_runtime_health(strict=...) for container/host watchdog
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import redis
from django.conf import settings
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

log = logging.getLogger(__name__)

HEARTBEAT_SERVICES = ("worker-fetch", "worker-llm", "worker-publish", "beat", "bot")
HEARTBEAT_TTL_SECONDS = 120


def check_liveness() -> dict[str, Any]:
    """Process liveness: returns immediately if web process is alive."""
    return {
        "status": "ok",
        "timestamp": timezone.now().isoformat(),
    }


def check_database() -> tuple[bool, str]:
    """Check database connection and responsiveness."""
    try:
        connection = connections["default"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, "connected"
    except Exception as exc:
        return False, f"Database error: {exc}"


def check_redis() -> tuple[bool, str]:
    """Check Redis connectivity."""
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        r = redis.from_url(broker_url, socket_timeout=3.0)
        r.ping()
        return True, "connected"
    except Exception as exc:
        return False, f"Redis error: {exc}"


def check_pending_migrations() -> tuple[bool, list[str]]:
    """Check if any unapplied migrations exist."""
    try:
        connection = connections["default"]
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
        if plan:
            unapplied = [f"{m[0].app_label}.{m[0].name}" for m in plan]
            return False, unapplied
        return True, []
    except Exception as exc:
        return False, [f"Migration check error: {exc}"]


def check_readiness() -> tuple[bool, dict[str, Any]]:
    """Readiness probe: verifies DB, Redis, and schema are ready."""
    db_ok, db_msg = check_database()
    redis_ok, redis_msg = check_redis()
    mig_ok, unapplied = check_pending_migrations()

    is_ready = db_ok and redis_ok and mig_ok
    status = "ok" if is_ready else "unhealthy"

    details = {
        "status": status,
        "timestamp": timezone.now().isoformat(),
        "database": {"ok": db_ok, "message": db_msg},
        "redis": {"ok": redis_ok, "message": redis_msg},
        "migrations": {"ok": mig_ok, "unapplied": unapplied},
    }
    return is_ready, details


def get_heartbeats() -> dict[str, dict[str, Any]]:
    """Read worker and bot heartbeats from Redis."""
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    heartbeats = {}
    now = datetime.now(UTC)

    try:
        r = redis.from_url(broker_url, socket_timeout=3.0)
        for svc in HEARTBEAT_SERVICES:
            key = f"news_radar:heartbeat:{svc}"
            val = r.get(key)
            if not val:
                heartbeats[svc] = {"status": "missing", "last_seen": None, "age_seconds": None}
                continue

            try:
                dt_val = datetime.fromisoformat(val.decode("utf-8"))
                age = (now - dt_val).total_seconds()
                if age <= HEARTBEAT_TTL_SECONDS:
                    heartbeats[svc] = {
                        "status": "healthy",
                        "last_seen": dt_val.isoformat(),
                        "age_seconds": round(age, 1),
                    }
                else:
                    heartbeats[svc] = {
                        "status": "stale",
                        "last_seen": dt_val.isoformat(),
                        "age_seconds": round(age, 1),
                    }
            except Exception:
                heartbeats[svc] = {
                    "status": "unknown",
                    "raw_value": str(val),
                    "age_seconds": None,
                }
    except Exception as exc:
        for svc in HEARTBEAT_SERVICES:
            heartbeats[svc] = {"status": "unreachable", "error": str(exc)}

    return heartbeats


def get_pipeline_freshness() -> dict[str, Any]:
    """Read last pipeline run metadata from Redis."""
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    try:
        r = redis.from_url(broker_url, socket_timeout=3.0)
        val = r.get("news_radar:last_pipeline_run")
        if val:
            return json.loads(val.decode("utf-8"))
        return {"status": "none_recorded"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def check_runtime_health(strict: bool = False) -> tuple[bool, dict[str, Any]]:
    """Comprehensive runtime health check for watchdog and monitoring."""
    ready_ok, readiness = check_readiness()
    heartbeats = get_heartbeats()
    pipeline_info = get_pipeline_freshness()

    # Determine overall status
    stale_or_missing_services = [
        svc for svc, h in heartbeats.items() if h.get("status") not in ("healthy",)
    ]

    if not ready_ok:
        overall_status = "unhealthy"
        is_healthy = False
    elif stale_or_missing_services:
        if strict:
            overall_status = "degraded"
            is_healthy = False
        else:
            overall_status = "degraded"
            is_healthy = True  # core DB/Redis is up
    else:
        overall_status = "healthy"
        is_healthy = True

    details = {
        "status": overall_status,
        "strict": strict,
        "timestamp": timezone.now().isoformat(),
        "readiness": readiness,
        "heartbeats": heartbeats,
        "degraded_services": stale_or_missing_services,
        "pipeline_last_run": pipeline_info,
    }

    return is_healthy, details
