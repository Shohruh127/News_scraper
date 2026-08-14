"""Celery app configuration with schedule beat definitions in Asia/Tashkent timezone."""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("news_radar")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "fetch-morning": {
        "task": "digest.fetch_all_sources",
        "schedule": crontab(hour=8, minute=0),
        "options": {"expires": 3600},
    },
    "fetch-evening": {
        "task": "digest.fetch_all_sources",
        "schedule": crontab(hour=17, minute=0),
        "options": {"expires": 3600},
    },
    "triage-and-classify": {
        "task": "digest.triage_and_classify",
        "schedule": crontab(hour=18, minute=0),
        "options": {"expires": 3600},
    },
    "compose-and-publish": {
        "task": "digest.compose_and_publish",
        "schedule": crontab(hour=19, minute=0),
        "options": {"expires": 3600},
    },
}
