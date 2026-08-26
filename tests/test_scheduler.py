"""Tests for T1.15 carried defects: lock handling, heartbeat, model digest cache."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.digest import llm
from apps.digest.management.commands.run_pipeline import Command


def test_run_pipeline_handles_lock_held_without_keyerror():
    """T1.15 defect 1: a held lock produces a clean skip rather than KeyError."""
    cmd = Command(stdout=StringIO(), stderr=StringIO())

    # Mock triage_and_classify to return the lock_held shape
    with patch("apps.digest.tasks.triage_and_classify") as mock_tc:
        mock_tc.return_value = {"status": "skipped", "reason": "lock_held"}

        # This should NOT raise KeyError
        cmd.handle(date="today", skip_fetch=True)

        output = cmd.stdout.getvalue()
        assert "Skipped" in output or "skipped" in output.lower()


def test_model_digest_is_no_longer_collected():
    """T1.15 cached /api/tags so a repointed Ollama tag showed up in the data.

    The direct Ollama path was removed on 2026-08-25 and no other provider exposes a
    digest, so drift detection is gone rather than merely unused. This pins that the
    machinery went with it instead of lingering as a function nothing can call.
    """
    assert not hasattr(llm, "fetch_model_digest")
    assert not hasattr(llm, "ollama_chat")
    assert not hasattr(llm, "_model_digest_cache")


def test_drip_entry_names_a_task_that_is_actually_registered():
    """Comparing the entry's string to the same literal proves nothing.

    Beat dispatches by task name. A name no task registered under is accepted by beat and
    rejected by the worker as NotRegistered, so the drip would simply never run. Anchor the
    assertion to the registered name instead.
    """
    from apps.digest import tasks
    from config.celery import app

    entry = app.conf.beat_schedule["drip-publish"]
    assert entry["task"] == tasks.publish_next_item.name
    assert entry["task"] in app.tasks


def test_drip_runs_on_the_odd_hours_and_expires():
    """Ticks are aligned to triage (08:30 and 18:00), so the usable hours are the odd ones.

    `expires` keeps a tick that queued behind a busy worker from firing late and putting two
    posts out back to back.
    """
    from config.celery import app

    entry = app.conf.beat_schedule["drip-publish"]
    assert entry["schedule"].hour == {1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23}
    assert entry["schedule"].minute == {0}
    assert entry["options"]["expires"] == 3600


def test_roundup_has_no_beat_entry():
    """The roundup is causal. A scheduled one would index a block whose last post was still
    queued — the 2026-08-21 failure, one level up."""
    from config.celery import app

    scheduled = {entry["task"] for entry in app.conf.beat_schedule.values()}
    assert "digest.publish_roundup" not in scheduled


@pytest.mark.django_db
def test_pruning_deletes_a_beat_entry_the_code_no_longer_has():
    """DatabaseScheduler adds and updates rows; it never deletes one.

    An entry removed from config/celery.py keeps firing from the database. This was a
    manual `shell -c "...delete()"` step in the deploy sequence until deploy.sh grew a
    call to prune_schedule.
    """
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    cron = CrontabSchedule.objects.create(minute="0", hour="9")
    PeriodicTask.objects.create(
        name="publish-morning", task="digest.compose_and_publish", crontab=cron
    )

    call_command("prune_schedule", stdout=StringIO())

    assert not PeriodicTask.objects.filter(name="publish-morning").exists()


@pytest.mark.django_db
def test_pruning_never_touches_celerys_own_entry():
    """`celery.backend_cleanup` is not in app.conf.beat_schedule — Celery installs it.

    "Delete every row the schedule does not name" would take it, and django_celery_results
    rows would then grow with nothing to trim them.
    """
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    cron = CrontabSchedule.objects.create(minute="0", hour="4")
    PeriodicTask.objects.create(
        name="celery.backend_cleanup", task="celery.backend_cleanup", crontab=cron
    )

    call_command("prune_schedule", stdout=StringIO())

    assert PeriodicTask.objects.filter(name="celery.backend_cleanup").exists()


@pytest.mark.django_db
def test_pruning_keeps_the_entries_the_code_still_declares():
    """A prune that emptied the schedule would pass the two tests above and stop the radar."""
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    from config.celery import app

    cron = CrontabSchedule.objects.create(minute="30", hour="8")
    for name in app.conf.beat_schedule:
        PeriodicTask.objects.create(
            name=name, task=app.conf.beat_schedule[name]["task"], crontab=cron
        )

    call_command("prune_schedule", stdout=StringIO())

    assert PeriodicTask.objects.count() == len(app.conf.beat_schedule)


@pytest.mark.django_db
def test_dry_run_reports_without_deleting():
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    cron = CrontabSchedule.objects.create(minute="0", hour="9")
    PeriodicTask.objects.create(
        name="publish-morning", task="digest.compose_and_publish", crontab=cron
    )

    out = StringIO()
    call_command("prune_schedule", "--dry-run", stdout=out)

    assert PeriodicTask.objects.filter(name="publish-morning").exists()
    assert "would delete" in out.getvalue()
