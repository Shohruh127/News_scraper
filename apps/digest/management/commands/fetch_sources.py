"""Run ingestion in-process. Same code path as the Celery task, without a worker."""

from django.core.management.base import BaseCommand

from apps.digest.models import Source
from apps.digest.tasks import fetch_source


class Command(BaseCommand):
    help = "Fetch all enabled sources synchronously."

    def add_arguments(self, parser):
        parser.add_argument("--source", help="fetch only this source by name")

    def handle(self, *args, **options):
        qs = Source.objects.filter(enabled=True)
        if options["source"]:
            qs = qs.filter(name=options["source"])

        rows, degraded = [], []
        for source in qs:
            result = fetch_source(source.pk)
            rows.append(result)
            if result.get("error"):
                degraded.append(source.name)

        self.stdout.write(f"\n  {'SOURCE':<16}{'FETCHED':>9}{'NEW':>7}{'DUP':>7}{'UNUSABLE':>10}")
        self.stdout.write("  " + "-" * 49)
        for r in rows:
            if r.get("error"):
                self.stdout.write(f"  {r['source']:<16}{'FAILED':>9}   {r['error'][:40]}")
            else:
                self.stdout.write(
                    f"  {r['source']:<16}{r['fetched']:>9}{r['created']:>7}"
                    f"{r['duplicate']:>7}{r['unusable']:>10}"
                )

        total_new = sum(r.get("created", 0) for r in rows)
        self.stdout.write(f"\n  {total_new} new articles")
        if degraded:
            # Not an error exit: a failing source must not fail the run (ADR-002).
            self.stdout.write(self.style.WARNING(f"  failed sources: {', '.join(degraded)}"))
