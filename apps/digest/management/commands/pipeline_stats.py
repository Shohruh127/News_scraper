"""Management command: what the pipeline ingested, what it spent, and where.

Answers three questions that were previously unanswerable because nothing recorded token
usage: what does one published post cost, which stage spends the budget, and which sources
are worth fetching at all.
"""

from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Avg, Count, Sum
from django.utils import timezone

from apps.digest.models import Analysis, Article, DigestItem

#: Reading order of the pipeline, so the report follows the funnel rather than the alphabet.
STAGE_ORDER = [
    Analysis.Stage.TRIAGE,
    Analysis.Stage.CLASSIFICATION,
    Analysis.Stage.EDITORIAL_EN,
    Analysis.Stage.EDITORIAL_UZ,
]


class Command(BaseCommand):
    help = "Report ingestion counts, token spend per stage, and per-source yield."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Window in days, counted back from now (default 7).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        since = timezone.now() - timedelta(days=days)

        self.stdout.write(f"\nWindow: last {days} days (since {since:%Y-%m-%d %H:%M})")

        self._funnel(since)
        totals = self._stages(since)
        self._unit_cost(since, totals)
        self._sources(since)

    # --- sections -----------------------------------------------------------

    def _funnel(self, since):
        counts = dict(
            Article.objects.filter(fetched_at__gte=since)
            .values_list("status")
            .annotate(n=Count("id"))
        )
        fetched = sum(counts.values())
        published = DigestItem.objects.filter(
            article__fetched_at__gte=since, channel_message_id__isnull=False
        ).count()

        self.stdout.write("\n" + "=" * 64)
        self.stdout.write("FUNNEL")
        self.stdout.write("=" * 64)
        self.stdout.write(f"  stored          {fetched:>6}")
        for status in ("fetched", "triaged", "classified", "skipped"):
            self.stdout.write(f"  {status:<15} {counts.get(status, 0):>6}")
        self.stdout.write(f"  published       {published:>6}")

    def _stages(self, since):
        rows = (
            Analysis.objects.filter(created_at__gte=since)
            .values("stage")
            .annotate(
                calls=Count("id"),
                measured=Count("input_tokens"),
                tokens_in=Sum("input_tokens"),
                tokens_out=Sum("output_tokens"),
                mean_ms=Avg("latency_ms"),
            )
        )
        by_stage = {r["stage"]: r for r in rows}

        self.stdout.write("\n" + "=" * 64)
        self.stdout.write("SPEND BY STAGE")
        self.stdout.write("=" * 64)
        self.stdout.write(
            f"  {'stage':<16}{'calls':>7}{'measured':>10}{'input':>11}{'output':>9}{'mean ms':>10}"
        )

        total_in = total_out = 0
        for stage in STAGE_ORDER:
            r = by_stage.get(stage)
            if not r:
                continue
            t_in, t_out = r["tokens_in"] or 0, r["tokens_out"] or 0
            total_in += t_in
            total_out += t_out
            self.stdout.write(
                f"  {stage:<16}{r['calls']:>7}{r['measured']:>10}"
                f"{t_in:>11,}{t_out:>9,}{r['mean_ms'] or 0:>10,.0f}"
            )

        unmeasured = sum(r["calls"] - r["measured"] for r in by_stage.values())
        self.stdout.write("-" * 64)
        self.stdout.write(f"  {'total':<16}{'':>7}{'':>10}{total_in:>11,}{total_out:>9,}")
        if unmeasured:
            # Not zero-filled on purpose: a NULL is a call whose cost nobody recorded, and
            # averaging it in as free would understate every number above it.
            self.stdout.write(
                f"\n  {unmeasured} call(s) carry no usage data — written before token"
                " accounting landed on 2026-08-25, or the provider returned no usage block."
                "\n  They are excluded from the totals, which are therefore a floor."
            )
        return total_in, total_out

    def _unit_cost(self, since, totals):
        total_in, total_out = totals
        published = DigestItem.objects.filter(
            digest__digest_date__gte=since.date(), channel_message_id__isnull=False
        ).count()

        self.stdout.write("\n" + "=" * 64)
        self.stdout.write("UNIT COST")
        self.stdout.write("=" * 64)
        if not published:
            self.stdout.write("  No posts published in this window.")
            return
        self.stdout.write(f"  posts published        {published:>10,}")
        self.stdout.write(f"  input tokens per post  {total_in // published:>10,}")
        self.stdout.write(f"  output tokens per post {total_out // published:>10,}")

    def _sources(self, since):
        stats = defaultdict(lambda: defaultdict(int))
        for name, status in Article.objects.filter(fetched_at__gte=since).values_list(
            "source__name", "status"
        ):
            stats[name]["stored"] += 1
            stats[name][status] += 1
        for name in DigestItem.objects.filter(
            article__fetched_at__gte=since, channel_message_id__isnull=False
        ).values_list("article__source__name", flat=True):
            stats[name]["published"] += 1

        if not stats:
            return

        self.stdout.write("\n" + "=" * 64)
        self.stdout.write("SOURCE YIELD")
        self.stdout.write("=" * 64)
        self.stdout.write(
            f"  {'source':<22}{'stored':>8}{'classified':>12}{'published':>11}{'yield':>8}"
        )
        # Worst yield last: the sources worth disabling are the ones to leave the reader on.
        for name, s in sorted(stats.items(), key=lambda kv: -kv[1]["published"]):
            stored = s["stored"]
            pct = (s["published"] / stored * 100) if stored else 0
            self.stdout.write(
                f"  {(name or '?'):<22}{stored:>8}{s['classified']:>12}"
                f"{s['published']:>11}{pct:>7.0f}%"
            )
        self.stdout.write(
            "\n  A source with many stored and no published items is spending triage calls"
            "\n  for nothing. Disable it on Source.enabled rather than filtering it later."
        )
