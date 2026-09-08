"""The chain carries the slot date, and a stale slot is refused at its head (2026-09-08).

Reproduced from the local stack on 2026-09-08, Asia/Tashkent: the stack was down at
2026-09-07 18:00 and came up at 14:57 the next day; beat replayed the missed evening entry
at once; compose_and_publish stamped it with today's date and composed the 2026-09-08
evening slot at 14:58 with one item; the genuine 18:00 run found the slot published and
composed nothing. The triage entries carry no `expires` on purpose (CLAUDE.md), and the
edition must never be re-derived from the clock, so the guard works on the *date*.
"""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.digest import llm, publish, ranking, tasks
from apps.digest.models import Article, Digest, Source

pytestmark = pytest.mark.django_db

TASHKENT = ZoneInfo("Asia/Tashkent")


def at(monkeypatch, *wall_time):
    """Freeze Django's clock at a Tashkent wall time (year, month, day, hour, minute[, s])."""
    fixed = datetime(*wall_time, tzinfo=TASHKENT).astimezone(UTC)
    monkeypatch.setattr(timezone, "now", lambda: fixed)


def _quiet_pipeline(monkeypatch):
    """No Redis, no LLM, no Telegram; the calls that do happen are recorded."""
    fake_redis = MagicMock()
    fake_redis.set.return_value = True
    monkeypatch.setattr("redis.Redis.from_url", lambda url: fake_redis)
    llm_calls, chained, alerts = [], [], []
    monkeypatch.setattr(llm, "triage_article_logic", lambda art: llm_calls.append(art.id) or False)
    monkeypatch.setattr(tasks.compose_and_publish, "delay", lambda **kw: chained.append(kw))
    monkeypatch.setattr(
        publish, "send_admin_alert", lambda text, client=None: alerts.append(text) or True
    )
    return llm_calls, chained, alerts


@pytest.fixture
def fetched_article(db):
    source = Source.objects.create(
        name="slot_src", connector=Source.Connector.RSS, url="https://s.test/rss", priority=50
    )
    return Article.objects.create(
        source=source,
        canonical_url="https://s.test/a",
        content_hash="slot_h1",
        title="Waiting for triage",
        extracted_text="x " * 50,
        status=Article.Status.FETCHED,
    )


def test_the_slot_is_the_latest_occurrence_of_the_editions_beat_entry(monkeypatch):
    at(monkeypatch, 2026, 9, 8, 14, 57)
    assert tasks.slot_for("evening") == datetime(2026, 9, 7, 18, 0, tzinfo=TASHKENT)
    assert tasks.slot_for("morning") == datetime(2026, 9, 8, 8, 30, tzinfo=TASHKENT)

    at(monkeypatch, 2026, 9, 8, 18, 0, 5)
    assert tasks.slot_for("evening") == datetime(2026, 9, 8, 18, 0, tzinfo=TASHKENT)

    # A worker that picks the message up after midnight is still working on that slot.
    at(monkeypatch, 2026, 9, 9, 0, 10)
    assert tasks.slot_for("evening") == datetime(2026, 9, 8, 18, 0, tzinfo=TASHKENT)

    # Beat never dispatches early, but a clock a few seconds apart must not push a genuine
    # message onto yesterday's slot.
    at(monkeypatch, 2026, 9, 8, 8, 29, 50)
    assert tasks.slot_for("morning") == datetime(2026, 9, 8, 8, 30, tzinfo=TASHKENT)

    assert tasks.slot_for("weekly") is None, "no entry, no slot: the chain composes for today"


def test_a_replayed_entry_from_yesterday_is_refused_before_any_llm_call(
    monkeypatch, fetched_article
):
    llm_calls, chained, alerts = _quiet_pipeline(monkeypatch)
    at(monkeypatch, 2026, 9, 8, 14, 57, 39)

    result = tasks.triage_and_classify(edition="evening")

    assert result["status"] == "stale_slot"
    assert result["slot"].startswith("2026-09-07T18:00") and result["age_hours"] == 21.0
    assert llm_calls == [], "refused before triage spent a call"
    assert chained == [] and Digest.objects.count() == 0
    assert len(alerts) == 1 and "2026-09-07 18:00" in alerts[0]


def test_the_2026_09_08_sequence_ends_with_the_genuine_run_composing(
    monkeypatch, settings, fetched_article
):
    """Replay at 14:57 refused; the genuine 18:00 run chains with its date and composes."""
    settings.PUBLISHING_ENABLED = True
    llm_calls, chained, _ = _quiet_pipeline(monkeypatch)

    at(monkeypatch, 2026, 9, 8, 14, 57, 39)
    tasks.triage_and_classify(edition="evening")
    assert chained == [] and Digest.objects.count() == 0

    at(monkeypatch, 2026, 9, 8, 18, 0, 0)
    result = tasks.triage_and_classify(edition="evening")
    assert result["digest_date"] == "2026-09-08" and llm_calls == [fetched_article.id]
    assert chained == [{"digest_date_str": "2026-09-08", "edition": "evening"}]

    monkeypatch.setattr(ranking, "select_digest_candidates", lambda target: [])
    monkeypatch.setattr(tasks.publish_next_item, "delay", lambda digest_id: None)
    assert tasks.compose_and_publish(**chained[0])["status"] == "drip_started"
    digest = Digest.objects.get()
    assert (digest.digest_date, digest.edition) == (date(2026, 9, 8), "evening")


def test_a_genuine_message_picked_up_after_midnight_keeps_its_slot_date(monkeypatch):
    """Without the carried date this composed the *next* day's evening slot: the same burn
    as the replay, with no outage involved, just an LLM stage that ran past midnight."""
    _, chained, alerts = _quiet_pipeline(monkeypatch)
    at(monkeypatch, 2026, 9, 9, 0, 10)

    result = tasks.triage_and_classify(edition="evening")

    assert result["digest_date"] == "2026-09-08" and alerts == []
    assert chained == [{"digest_date_str": "2026-09-08", "edition": "evening"}]


def test_a_run_without_an_edition_is_untouched(monkeypatch):
    """run_pipeline calls the task with no edition; compose then derives it as before."""
    _, chained, alerts = _quiet_pipeline(monkeypatch)
    at(monkeypatch, 2026, 9, 8, 14, 57)

    result = tasks.triage_and_classify()

    assert result["digest_date"] is None and alerts == []
    assert chained == [{"digest_date_str": None, "edition": None}]


def test_the_triage_entries_are_simple_daily_crontabs():
    """slot_for reads one hour and one minute per entry. A schedule with several, or with
    a day-of-week, needs the general crontab arithmetic, and this test says so first."""
    from celery import current_app

    entries = [e for e in current_app.conf.beat_schedule.values() if e["task"] == tasks.TRIAGE_TASK]
    assert {e["kwargs"]["edition"] for e in entries} == {"morning", "evening"}
    for entry in entries:
        s = entry["schedule"]
        assert len(s.hour) == 1 and len(s.minute) == 1, entry
        assert len(s.day_of_week) == 7 and len(s.day_of_month) == 31, entry
        assert len(s.month_of_year) == 12, entry
        assert "expires" not in entry.get("options", {}), (
            "a dropped triage message loses the edition"
        )
