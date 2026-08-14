"""Management command to compose (if needed) and publish a digest to Telegram."""

from datetime import date as dt_date

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.digest import publish, ranking
from apps.digest.models import Digest


class Command(BaseCommand):
    help = "Compose and publish digest for a specific date or digest ID"

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            type=str,
            default="today",
            help="Date in YYYY-MM-DD format or 'today'",
        )
        parser.add_argument(
            "--digest-id",
            type=int,
            default=None,
            help="Specific Digest ID to publish",
        )

    def handle(self, *args, **options):
        digest_id = options.get("digest_id")
        if digest_id:
            try:
                digest = Digest.objects.get(pk=digest_id)
            except Digest.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"Digest with id {digest_id} does not exist."))
                return
        else:
            date_str = options.get("date", "today")
            if date_str == "today":
                target_date = timezone.localdate()
            else:
                target_date = dt_date.fromisoformat(date_str)

            digest = Digest.objects.filter(digest_date=target_date).first()
            if not digest:
                self.stdout.write(f"Composing new digest for {target_date}...")
                digest = ranking.compose_digest(target_date)

        if digest.items.count() == 0:
            self.stdout.write(self.style.WARNING(
                f"Digest {digest.digest_date} has 0 items. Nothing to publish."
            ))
            return

        self.stdout.write(
            f"Publishing digest for {digest.digest_date} ({digest.items.count()} items)..."
        )
        res = publish.publish_digest(digest)
        self.stdout.write(self.style.SUCCESS(
            f"Successfully published digest {res['digest_date']}: "
            f"channel_msg={res['channel_message_id']}, group_msg={res['group_message_id']}"
        ))
