"""Management command to execute the full pipeline end-to-end for a target date."""

from datetime import date as dt_date

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.digest import connectors, tasks
from apps.digest.models import Digest, Source


class Command(BaseCommand):
    help = "Run the full news radar pipeline: fetch, triage/classify, compose, and publish"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default="today",
            help="Date in YYYY-MM-DD format or 'today' (default: today)",
        )
        parser.add_argument(
            "--skip-fetch",
            action="store_true",
            help="Skip the fetch stage and run triage/classify/publish on existing articles",
        )

    def handle(self, *args, **options):
        date_str = options.get("date", "today")
        if date_str == "today":
            target_date = timezone.localdate()
        else:
            target_date = dt_date.fromisoformat(date_str)

        self.stdout.write(f"\n[>>] Running full pipeline for date: {target_date}\n")

        # Step 1: Fetch sources synchronously if not skipped
        if not options.get("skip_fetch"):
            sources = list(Source.objects.filter(enabled=True))
            self.stdout.write(f"1. Fetching {len(sources)} enabled sources...")
            client = connectors.http_client()
            try:
                for src in sources:
                    try:
                        items = connectors.fetch(src, client=client)
                        created, skipped, failed = tasks._store(src, items)
                        tasks._record_success(src)
                        self.stdout.write(
                            f"   * {src.name}: {len(items)} fetched, {created} new, "
                            f"{skipped} skipped, {failed} unusable"
                        )
                    except Exception as exc:
                        tasks._record_failure(src, exc)
                        self.stdout.write(self.style.WARNING(f"   * {src.name} failed: {exc}"))
            finally:
                client.close()
        else:
            self.stdout.write("1. Skipping fetch step as requested.")

        # Step 2: Triage and Classify
        self.stdout.write("\n2. Running triage and classification...")
        llm_result = tasks.triage_and_classify(trigger_publish_chain=False)
        triaged_s = llm_result["triage_survivors"]
        classified_s = llm_result["classify_survivors"]
        self.stdout.write(
            f"   * Triaged: {llm_result['triaged']} (Survivors: {triaged_s})\n"
            f"   * Classified: {llm_result['classified']} (Survivors: {classified_s})"
        )

        # Step 3: Compose and Publish
        self.stdout.write(f"\n3. Composing and publishing digest for {target_date}...")
        existing_digest = Digest.objects.filter(digest_date=target_date).first()
        if existing_digest and existing_digest.status == Digest.Status.PUBLISHED:
            self.stdout.write(
                self.style.WARNING(
                    f"   * Digest for {target_date} already published. Duplicate run refused."
                )
            )
            return

        res = tasks.compose_and_publish(str(target_date))
        self.stdout.write(self.style.SUCCESS(f"   * Pipeline finished: {res}"))
