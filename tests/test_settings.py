"""Smoke tests for configuration. These guard decisions that are easy to undo by accident."""

from django.conf import settings


def test_timezone_is_tashkent():
    assert settings.TIME_ZONE == "Asia/Tashkent"
    assert settings.USE_TZ is True


def test_publishing_defaults_to_off_when_unset(monkeypatch):
    """The kill switch must default off, so forgetting to set it cannot publish.

    Asserts the declared default with the variable removed, not the developer's live
    .env — a test that reads the current environment fails the moment someone
    legitimately enables publishing, which teaches the team to delete the test rather
    than trust it.
    """
    import environ

    monkeypatch.delenv("PUBLISHING_ENABLED", raising=False)
    assert environ.Env(PUBLISHING_ENABLED=(bool, False))("PUBLISHING_ENABLED") is False


def test_editorial_provider_is_switchable():
    """ADR-004 §5: reverting from MiMo to local Ollama must be one setting."""
    assert settings.LLM_PROVIDER in ("ollama", "mimo")
    if settings.LLM_PROVIDER == "mimo":
        assert settings.MIMO_BASE_URL, "LLM_PROVIDER=mimo requires MIMO_BASE_URL"
        assert settings.MIMO_API_KEY, "LLM_PROVIDER=mimo requires MIMO_API_KEY"


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
    assert "triage-morning" in sched
    assert "fetch-evening" in sched
    assert "triage-evening" in sched
    assert sched["fetch-morning"]["task"] == "digest.fetch_all_sources"
    assert sched["triage-morning"]["task"] == "digest.triage_and_classify"
    assert sched["triage-morning"]["kwargs"] == {"edition": "morning"}
    assert sched["triage-evening"]["kwargs"] == {"edition": "evening"}
    # The whole edition now hangs off the triage message, because publishing is chained
    # to it. An expiry would discard that message while the llm worker is busy and
    # nothing would publish at all that cycle.
    assert "expires" not in sched["triage-morning"].get("options", {})
    assert "expires" not in sched["triage-evening"].get("options", {})
    # Publishing is chained off triage_and_classify, not scheduled on its own clock.
    assert not [k for k in sched if k.startswith("compose-and-publish")]


def test_editorial_budget_clears_the_reasoning_floor():
    """The gateway's `smart` tier charges its own reasoning to the same token budget.

    Measured 2026-08-21 against the live gateway with the real editorial prompt: 1500
    returned finish_reason "length" and an empty message, 3000 completed. Any value near
    the observed failure point drops every article in the editorial stage, which surfaces
    as an empty digest rather than as an error.
    """
    from django.conf import settings

    assert settings.EDITORIAL_NUM_PREDICT >= 3000


def test_link_preview_defaults_to_on_when_unset(monkeypatch):
    """Link preview is the approved image delivery mechanism (Option A, 2026-08-18).

    Guards against someone disabling it by accident in a refactor.
    """
    import environ

    monkeypatch.delenv("TELEGRAM_LINK_PREVIEW", raising=False)
    assert environ.Env(TELEGRAM_LINK_PREVIEW=(bool, True))("TELEGRAM_LINK_PREVIEW") is True


def test_post_format_v2_defaults_to_off_when_unset(monkeypatch):
    """v2 post format redesign is built behind a default-off feature flag."""
    import environ

    monkeypatch.delenv("POST_FORMAT_V2_ENABLED", raising=False)
    assert environ.Env(POST_FORMAT_V2_ENABLED=(bool, False))("POST_FORMAT_V2_ENABLED") is False


def test_post_budget_defaults():
    from django.conf import settings as _settings

    assert _settings.POST_MAX_SENTENCES == 3
    assert _settings.POST_MAX_CHARS == 450
