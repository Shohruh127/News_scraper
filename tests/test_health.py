"""Tests for runtime health, liveness, readiness, heartbeats, and watchdog."""

import json
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.digest import health, tasks


@pytest.fixture
def client():
    return Client()


def test_healthz_endpoint(client):
    """healthz returns 200 OK with liveness status."""
    response = client.get("/healthz/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


@pytest.mark.django_db
def test_readyz_endpoint_healthy(client, monkeypatch):
    """readyz returns 200 OK when DB, Redis, and schema are ready."""
    monkeypatch.setattr(health, "check_redis", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_pending_migrations", lambda: (True, []))

    response = client.get("/readyz/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["database"]["ok"] is True
    assert data["redis"]["ok"] is True
    assert data["migrations"]["ok"] is True


@pytest.mark.django_db
def test_readyz_endpoint_db_failure(client, monkeypatch):
    """readyz returns 503 when database is down."""
    monkeypatch.setattr(health, "check_database", lambda: (False, "connection refused"))
    monkeypatch.setattr(health, "check_redis", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_pending_migrations", lambda: (True, []))

    response = client.get("/readyz/")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["database"]["ok"] is False


@pytest.mark.django_db
def test_readyz_endpoint_redis_failure(client, monkeypatch):
    """readyz returns 503 when Redis is down."""
    monkeypatch.setattr(health, "check_database", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_redis", lambda: (False, "redis timeout"))
    monkeypatch.setattr(health, "check_pending_migrations", lambda: (True, []))

    response = client.get("/readyz/")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["redis"]["ok"] is False


@pytest.mark.django_db
def test_readyz_endpoint_unapplied_migrations(client, monkeypatch):
    """readyz returns 503 when migrations are pending."""
    monkeypatch.setattr(health, "check_database", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_redis", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_pending_migrations", lambda: (False, ["digest.0008_fake"]))

    response = client.get("/readyz/")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["migrations"]["ok"] is False
    assert "digest.0008_fake" in data["migrations"]["unapplied"]


def test_record_heartbeat_task(monkeypatch):
    """record_heartbeat writes timestamp and ex=120 to Redis."""
    redis_mock = MagicMock()
    monkeypatch.setattr("redis.from_url", lambda *args, **kw: redis_mock)

    res = tasks.record_heartbeat("worker-fetch")
    assert res["service"] == "worker-fetch"
    assert "heartbeat_at" in res
    assert redis_mock.set.called
    call_args = redis_mock.set.call_args
    assert call_args[0][0] == "news_radar:heartbeat:worker-fetch"
    assert call_args[1]["ex"] == 120


def test_dispatch_worker_heartbeats_task(monkeypatch):
    """dispatch_worker_heartbeats dispatches to all queues and records beat."""
    redis_mock = MagicMock()
    monkeypatch.setattr("redis.from_url", lambda *args, **kw: redis_mock)

    mock_apply = MagicMock()
    monkeypatch.setattr(tasks.record_heartbeat, "apply_async", mock_apply)

    res = tasks.dispatch_worker_heartbeats()
    assert "beat" in res["dispatched"]
    assert mock_apply.call_count == 3


def test_get_heartbeats_stale_and_healthy(monkeypatch):
    """get_heartbeats detects healthy vs stale services."""
    now_iso = timezone.now().isoformat()
    old_iso = "2020-01-01T00:00:00+00:00"

    class FakeRedis:
        def get(self, key):
            if "worker-fetch" in key:
                return now_iso.encode("utf-8")
            if "worker-llm" in key:
                return old_iso.encode("utf-8")
            return None

    monkeypatch.setattr("redis.from_url", lambda *args, **kw: FakeRedis())

    hb = health.get_heartbeats()
    assert hb["worker-fetch"]["status"] == "healthy"
    assert hb["worker-llm"]["status"] == "stale"
    assert hb["worker-publish"]["status"] == "missing"


@pytest.mark.django_db
def test_runtime_health_strict_mode(monkeypatch):
    """check_runtime_health in strict mode fails if any worker is missing or stale."""
    monkeypatch.setattr(health, "check_database", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_redis", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_pending_migrations", lambda: (True, []))

    # All workers healthy
    monkeypatch.setattr(
        health,
        "get_heartbeats",
        lambda: {svc: {"status": "healthy"} for svc in health.HEARTBEAT_SERVICES},
    )

    is_healthy, data = health.check_runtime_health(strict=True)
    assert is_healthy is True
    assert data["status"] == "healthy"

    # One worker missing -> strict mode fails
    monkeypatch.setattr(
        health,
        "get_heartbeats",
        lambda: {
            "worker-fetch": {"status": "healthy"},
            "worker-llm": {"status": "missing"},
            "worker-publish": {"status": "healthy"},
            "beat": {"status": "healthy"},
            "bot": {"status": "healthy"},
        },
    )

    is_healthy, data = health.check_runtime_health(strict=True)
    assert is_healthy is False
    assert data["status"] == "degraded"
    assert "worker-llm" in data["degraded_services"]


@pytest.mark.django_db
def test_runtime_health_command_output(monkeypatch, capsys):
    """runtime_health command outputs structured key=value and JSON."""
    monkeypatch.setattr(health, "check_database", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_redis", lambda: (True, "connected"))
    monkeypatch.setattr(health, "check_pending_migrations", lambda: (True, []))
    monkeypatch.setattr(
        health,
        "get_heartbeats",
        lambda: {
            svc: {"status": "healthy", "age_seconds": 5.2} for svc in health.HEARTBEAT_SERVICES
        },
    )
    monkeypatch.setattr(
        health,
        "get_pipeline_freshness",
        lambda: {"status": "published", "completed_at": "2026-08-19T18:00:00Z"},
    )

    # Key=value output
    call_command("runtime_health")
    out = capsys.readouterr().out
    assert "status=healthy" in out
    assert "database=True" in out
    assert "redis=True" in out
    assert "service:worker-fetch=healthy" in out

    # JSON output
    call_command("runtime_health", "--json")
    out_json = capsys.readouterr().out
    parsed = json.loads(out_json)
    assert parsed["status"] == "healthy"
    assert parsed["heartbeats"]["worker-fetch"]["status"] == "healthy"
