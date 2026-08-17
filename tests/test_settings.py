"""Smoke tests for configuration. These guard decisions that are easy to undo by accident."""

from django.conf import settings


def test_timezone_is_tashkent():
    assert settings.TIME_ZONE == "Asia/Tashkent"
    assert settings.USE_TZ is True


def test_publishing_is_off_by_default():
    """The kill switch defaults to off until GATE 1 passes."""
    assert settings.PUBLISHING_ENABLED is False


def test_llm_concurrency_matches_measurement():
    """Measured in docs/spike/OLLAMA_BENCHMARK.md: 8 parallel requests are slower
    than serial. Raising this without a new measurement is a regression."""
    assert settings.OLLAMA_MAX_CONCURRENCY == 2


def test_celery_routes_separate_fetch_from_llm():
    """Network fetching and Ollama inference have different concurrency budgets,
    so they must not share a queue."""
    routes = settings.CELERY_TASK_ROUTES
    assert routes["digest.fetch_source"]["queue"] == "fetch"
    assert routes["digest.triage_article"]["queue"] == "llm"
    assert routes["digest.classify_article"]["queue"] == "llm"


def test_ranking_weights_sum_to_one():
    assert round(sum(settings.RANKING_WEIGHTS.values()), 6) == 1.0


def test_celery_app_is_importable():
    from config import celery_app

    assert celery_app.main == "news_radar"


def test_celery_beat_schedule_configured():
    from config import celery_app

    sched = celery_app.conf.beat_schedule
    assert "fetch-morning" in sched
    assert "fetch-evening" in sched
    assert "triage-and-classify" in sched
    assert "compose-and-publish" in sched
    assert sched["fetch-morning"]["task"] == "digest.fetch_all_sources"
    assert sched["triage-and-classify"]["task"] == "digest.triage_and_classify"
    assert sched["compose-and-publish"]["task"] == "digest.compose_and_publish"
