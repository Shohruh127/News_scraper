"""Management command to edit a published Telegram message for a DigestItem."""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.digest import publish
from apps.digest.models import DigestItem


class Command(BaseCommand):
    help = "Edit message text for a published DigestItem on Telegram"

    def add_arguments(self, parser):
        parser.add_argument("--item-id", type=int, required=True, help="DigestItem ID")
        parser.add_argument("--text", type=str, required=True, help="New message text")

    def handle(self, *args, **options):
        item_id = options["item_id"]
        new_text = options["text"]

        try:
            item = DigestItem.objects.get(pk=item_id)
        except DigestItem.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"DigestItem {item_id} not found."))
            return

        if not item.channel_message_id:
            self.stderr.write(self.style.ERROR(f"DigestItem {item_id} has no channel_message_id."))
            return

        channel_id = getattr(settings, "TELEGRAM_CHANNEL_ID", "")
        res = publish.edit_message(
            chat_id=channel_id,
            message_id=item.channel_message_id,
            new_text=new_text,
            sent_as_photo=item.sent_as_photo,
        )
        self.stdout.write(self.style.SUCCESS(f"Edited message for item {item_id}: {res}"))
