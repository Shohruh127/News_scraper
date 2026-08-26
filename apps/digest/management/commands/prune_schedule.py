"""Delete PeriodicTask rows for beat entries that no longer exist in the code.

`CELERY_BEAT_SCHEDULER` is `DatabaseScheduler`, so the live schedule is rows in
`PeriodicTask`, not the dict in `config/celery.py`. Beat copies that dict into the database
with `update_or_create`: it adds and it updates, and it never deletes a row for an entry
that was removed from the code. A renamed or deleted entry keeps firing from the database
until somebody removes the row by hand — which is why the documented deploy sequence had a
`shell -c "...delete()"` step in it, and why forgetting that step is silent.

Only tasks this project owns are considered. `celery.backend_cleanup` is not in
`app.conf.beat_schedule` — Celery installs it itself — so a rule of "delete every row the
schedule does not name" would delete it, and `django_celery_results` rows would then
accumulate with nothing to trim them.
"""

from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask

from config.celery import app

#: Every task in this project is registered under this namespace (see `config/celery.py`).
#: Rows outside it belong to Celery or to something else, and are left alone.
OWNED_PREFIX = "digest."


class Command(BaseCommand):
    help = "Delete PeriodicTask rows whose beat entry no longer exists in config/celery.py."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted and exit without deleting it.",
        )

    def handle(self, *args, **options):
        live = set(app.conf.beat_schedule)
        stale = PeriodicTask.objects.filter(task__startswith=OWNED_PREFIX).exclude(name__in=live)

        if not stale.exists():
            self.stdout.write(f"Schedule is clean: {len(live)} entries, no stale rows.")
            return

        for row in stale.order_by("name"):
            verb = "would delete" if options["dry_run"] else "deleting"
            self.stdout.write(f"  {verb}  {row.name}  ({row.task})")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"{stale.count()} stale row(s), none deleted."))
            return

        count = stale.count()
        stale.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"{count} stale row(s) deleted. Restart beat so it re-reads the schedule."
            )
        )
