"""Per-source funnel. T1.20 says to keep the sources that earn their place; this measures it.

Every stage is counted separately because they answer different questions. ARTS says the feed
works. TRIAGED says the content is still moving. CLASSIFIED says it survived the model. DIGEST
says ranking chose it. PUBLISHED says it reached a reader.

TRIAGED exists because without it a rejection and a queue look identical. Measured 2026-08-18:
eleven newly added sources showed 0 CLASSIFIED and read as a failed expansion, while 71 of their
99 articles were sitting in `triaged`, waiting for a classification pass that had not run yet.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.digest.models import Article, DigestItem, Source


class Command(BaseCommand):
    help = "Show how many articles each source produced and how many reached a reader."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=0,
            help="only count articles fetched in the last N days (0 = all time)",
        )

    def handle(self, *args, **options):
        articles = Article.objects.all()
        if options["days"]:
            cutoff = timezone.now() - timedelta(days=options["days"])
            articles = articles.filter(fetched_at__gte=cutoff)

        self.stdout.write(
            f"\n  {'SOURCE':<18}{'ARTS':>6}{'TRIAGED':>9}{'CLASSIF':>9}{'DIGEST':>8}"
            f"{'PUBLISHED':>11}{'YIELD':>8}  LAST FETCH"
        )
        self.stdout.write("  " + "-" * 77)

        for source in Source.objects.order_by("name"):
            mine = articles.filter(source=source)
            total = mine.count()
            triaged = mine.filter(status=Article.Status.TRIAGED).count()
            classified = mine.filter(status=Article.Status.CLASSIFIED).count()
            in_digest = DigestItem.objects.filter(article__in=mine).count()
            published = DigestItem.objects.filter(
                article__in=mine, channel_message_id__isnull=False
            ).count()
            rate = f"{100 * published / total:.1f}%" if total else "-"
            last = source.last_fetched_at.date() if source.last_fetched_at else "never"
            flag = "" if source.enabled else "  (disabled)"
            self.stdout.write(
                f"  {source.name:<18}{total:>6}{triaged:>9}{classified:>9}{in_digest:>8}"
                f"{published:>11}{rate:>8}  {last}{flag}"
            )

        self.stdout.write(
            "\n  YIELD is published articles over articles fetched. A source with a healthy "
            "feed\n  and a zero yield is producing content that ranking never chooses — but "
            "check\n  TRIAGED first: those articles have not been judged yet.\n"
        )
