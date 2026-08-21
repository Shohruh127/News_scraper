"""Celery app configuration with schedule beat definitions in Asia/Tashkent timezone."""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("news_radar")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # --- Morning Cycle (Asia/Tashkent) ---
    "fetch-morning": {
        "task": "digest.fetch_all_sources",
        "schedule": crontab(hour=7, minute=30),
        "options": {"expires": 3600},
    },
    # These two carry no `expires`: publishing hangs off this message now, so dropping it
    # because the llm worker was busy would mean nothing publishes at all that cycle.
    # Publishing is not scheduled. triage_and_classify chains into compose_and_publish
    # when it actually finishes; a fixed publish time raced the LLM stage, published an
    # empty digest and locked the slot for the day — measured 2026-08-21.
    "triage-morning": {
        "task": "digest.triage_and_classify",
        "schedule": crontab(hour=8, minute=30),
        "kwargs": {"edition": "morning"},
    },
    # --- Evening Cycle (Asia/Tashkent) ---
    "fetch-evening": {
        "task": "digest.fetch_all_sources",
        "schedule": crontab(hour=16, minute=30),
        "options": {"expires": 3600},
    },
    "triage-evening": {
        "task": "digest.triage_and_classify",
        "schedule": crontab(hour=18, minute=0),
        "kwargs": {"edition": "evening"},
    },
    # --- Heartbeats ---
    "dispatch-worker-heartbeats": {
        "task": "digest.dispatch_worker_heartbeats",
        "schedule": 30.0,
    },
}
