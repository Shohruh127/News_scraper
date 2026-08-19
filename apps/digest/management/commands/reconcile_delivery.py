"""Management command to reconcile Telegram channel delivery state for a DigestItem."""

from django.core.management.base import BaseCommand, CommandError

from apps.digest.models import DeliveryState, DigestItem


class Command(BaseCommand):
    help = "Reconcile delivery state for an ambiguous or failed DigestItem."

    def add_arguments(self, parser):
        parser.add_argument("item_id", type=int, help="ID of the DigestItem")
        parser.add_argument(
            "--message-id",
            type=int,
            help="Telegram channel message ID if post succeeded",
        )
        parser.add_argument(
            "--sent-as-photo",
            choices=["yes", "no"],
            help="Whether the post was sent as photo ('yes' or 'no')",
        )
        parser.add_argument(
            "--reset-pending",
            action="store_true",
            help="Reset delivery state back to pending for retry",
        )
        parser.add_argument(
            "--i-checked-telegram",
            action="store_true",
            help="Explicit acknowledgement that Telegram was manually inspected",
        )

    def handle(self, *args, **options):
        item_id = options["item_id"]
        msg_id = options.get("message_id")
        sent_as_photo_str = options.get("sent_as_photo")
        reset_pending = options.get("reset_pending")
        checked_telegram = options.get("i_checked_telegram")

        try:
            item = DigestItem.objects.get(id=item_id)
        except DigestItem.DoesNotExist as err:
            raise CommandError(f"DigestItem #{item_id} does not exist.") from err

        if reset_pending:
            if not checked_telegram:
                raise CommandError(
                    "--reset-pending requires --i-checked-telegram to confirm "
                    "no orphan post exists on the channel."
                )
            item.channel_delivery_state = DeliveryState.PENDING
            item.channel_delivery_error = "Manually reset to pending after operator inspection"
            item.channel_message_id = None
            item.save(
                update_fields=[
                    "channel_delivery_state",
                    "channel_delivery_error",
                    "channel_message_id",
                ]
            )
            self.stdout.write(self.style.SUCCESS(f"DigestItem #{item_id} reset to PENDING."))
            return

        if msg_id is not None:
            if sent_as_photo_str is None:
                raise CommandError("--message-id requires --sent-as-photo yes|no to be specified.")
            item.channel_message_id = msg_id
            item.sent_as_photo = sent_as_photo_str == "yes"
            item.channel_delivery_state = DeliveryState.SENT
            item.channel_delivery_error = ""
            item.save(
                update_fields=[
                    "channel_message_id",
                    "sent_as_photo",
                    "channel_delivery_state",
                    "channel_delivery_error",
                ]
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"DigestItem #{item_id} reconciled as SENT "
                    f"(msg_id={msg_id}, photo={item.sent_as_photo})."
                )
            )
            return

        raise CommandError("Provide either --message-id (with --sent-as-photo) or --reset-pending.")
