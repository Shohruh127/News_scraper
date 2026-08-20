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
    "triage-morning": {
        "task": "digest.triage_and_classify",
        "schedule": crontab(hour=8, minute=30),
        "options": {"expires": 3600},
    },
    "compose-and-publish-morning": {
        "task": "digest.compose_and_publish",
        "schedule": crontab(hour=9, minute=0),
        "kwargs": {"edition": "morning"},
        "options": {"expires": 3600},
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
        "options": {"expires": 3600},
    },
    "compose-and-publish-evening": {
        "task": "digest.compose_and_publish",
        "schedule": crontab(hour=19, minute=0),
        "kwargs": {"edition": "evening"},
        "options": {"expires": 3600},
    },
    # --- Heartbeats ---
    "dispatch-worker-heartbeats": {
        "task": "digest.dispatch_worker_heartbeats",
        "schedule": 30.0,
    },
}
