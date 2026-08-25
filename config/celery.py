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
    # --- Drip publishing ---
    # Odd hours, not even ones, because the ticks are aligned to the triage entries above:
    # triage runs at 08:30 and 18:00, so the first tick a freshly composed block can use is
    # 09:00 and 19:00. On even hours a morning block composed at 08:40 would wait until
    # 10:00 with an empty 08:00 tick behind it.
    #
    # `expires` is safe here and deliberate, unlike on the triage entries: a dropped tick
    # delays one post by two hours, where a dropped triage message costs the whole edition.
    # Without it, a tick queued behind a busy worker fires late and puts two posts out back
    # to back.
    "drip-publish": {
        "task": "digest.publish_next_item",
        "schedule": crontab(minute=0, hour="1,3,5,7,9,11,13,15,17,19,21,23"),
        "options": {"expires": 3600},
    },
}
