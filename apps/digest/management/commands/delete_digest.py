"""Management command to delete a published Telegram message for a DigestItem."""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.digest import publish
from apps.digest.models import DigestItem


class Command(BaseCommand):
    help = "Delete message for a published DigestItem on Telegram"

    def add_arguments(self, parser):
        parser.add_argument("--item-id", type=int, required=True, help="DigestItem ID")

    def handle(self, *args, **options):
        item_id = options["item_id"]

        try:
            item = DigestItem.objects.get(pk=item_id)
        except DigestItem.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"DigestItem {item_id} not found."))
            return

        if not item.channel_message_id:
            self.stderr.write(self.style.ERROR(f"DigestItem {item_id} has no channel_message_id."))
            return

        channel_id = getattr(settings, "TELEGRAM_CHANNEL_ID", "")
        res = publish.delete_message(
            chat_id=channel_id,
            message_id=item.channel_message_id,
        )
        self.stdout.write(self.style.SUCCESS(f"Deleted message for item {item_id}: {res}"))
