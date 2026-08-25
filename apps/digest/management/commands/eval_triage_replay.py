"""Management command: replay the current triage gate over already-classified articles.

The gold set is 26 hand-labelled rows. The database holds every article that ever reached
classification, and that classification is a stronger label: the deep tier produced it after
reading the whole article, not a headline.

So the measurement is a replay. For each article with a stored classification, derive the
keep/drop verdict from it, run today's triage on the title alone, and compare.

One asymmetry is unavoidable and is stated in the output: articles the *old* triage dropped
were never classified, so they carry no label and cannot appear here. The replay therefore
measures the gate on the population that reaches classification today, which is the
population it will actually face.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.digest import llm
from apps.digest.models import EXCLUDED_MATURITIES, Analysis, Topic


class Command(BaseCommand):
    help = "Replay the triage gate over classified articles and report recall."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Window in days (default 30).")
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after N articles. 0 means no limit. Each article costs one fast call.",
        )

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(days=options["days"])
        rows = (
            Analysis.objects.filter(stage=Analysis.Stage.CLASSIFICATION, created_at__gte=since)
            .select_related("article", "article__source")
            .order_by("-created_at")
        )
        if options["limit"]:
            rows = rows[: options["limit"]]
        rows = list(rows)

        if not rows:
            self.stdout.write("No classified articles in this window; nothing to replay.")
            return

        self.stdout.write(
            f"\nReplaying triage over {len(rows)} classified article(s) "
            f"on {llm.settings.CLASSIFIER_PROVIDER}. One fast call each.\n"
        )

        tp = fp = tn = fn = 0
        tokens_in = tokens_out = 0
        lost: list[tuple[str, str]] = []
        errors = 0

        for idx, analysis in enumerate(rows, 1):
            article = analysis.article
            label = self._verdict(analysis)

            try:
                passed, result = llm.triage_text(
                    title=article.title,
                    source_name=article.source.name if article.source else "",
                )
            except Exception as exc:
                errors += 1
                self.stdout.write(
                    self.style.ERROR(f"[{idx}/{len(rows)}] {article.title[:40]} -> {exc}")
                )
                continue

            tokens_in += result.input_tokens or 0
            tokens_out += result.output_tokens or 0

            if label == "keep" and passed:
                tp += 1
            elif label == "drop" and passed:
                fp += 1
            elif label == "drop" and not passed:
                tn += 1
            else:
                fn += 1
                lost.append((article.title, result.payload.get("reason", "")))

            mark = "ok " if (label == "keep") == passed else "XX "
            self.stdout.write(
                f"[{idx}/{len(rows)}] {mark}{article.title[:44]:<44} "
                f"label={label:<4} triage={'keep' if passed else 'drop'}"
            )

        scored = tp + fp + tn + fn
        if not scored:
            self.stdout.write(self.style.ERROR("\nEvery row errored; no metrics to report."))
            return

        recall = tp / (tp + fn) if (tp + fn) else 0.0
        precision = tp / (tp + fp) if (tp + fp) else 0.0

        self.stdout.write("\n" + "=" * 64)
        self.stdout.write(f"TRIAGE REPLAY — {scored} article(s), {errors} error(s)")
        self.stdout.write("=" * 64)
        self.stdout.write(f"  kept and should keep   (TP) {tp:>6}")
        self.stdout.write(f"  kept but should drop   (FP) {fp:>6}   costs one classification each")
        self.stdout.write(f"  dropped and should drop(TN) {tn:>6}")
        self.stdout.write(f"  dropped but should keep(FN) {fn:>6}   THESE ARE LOST ARTICLES")
        self.stdout.write("-" * 64)
        self.stdout.write(f"  RECALL     {recall:>8.4f}   the number that governs")
        self.stdout.write(f"  precision  {precision:>8.4f}")
        self.stdout.write(f"  replay cost {tokens_in:>7,} in / {tokens_out:,} out")

        if lost:
            self.stdout.write("\n" + "-" * 64)
            self.stdout.write("ARTICLES THIS GATE WOULD HAVE LOST")
            self.stdout.write("-" * 64)
            for title, reason in lost:
                self.stdout.write(f"  {title[:52]:<52} :: {reason[:40]}")
            self.stdout.write(
                "\n  Read these before trusting the recall number. One lost article that"
                "\n  should obviously have been kept matters more than the percentage."
            )

        self.stdout.write(
            "\n  Articles the OLD triage dropped were never classified, so they carry no"
            "\n  label and are absent here. This measures the gate on the population that"
            "\n  reaches classification today, not on everything ever fetched."
        )

    @staticmethod
    def _verdict(analysis: Analysis) -> str:
        """The label: what the deep tier decided after reading the whole article."""
        if analysis.topic == Topic.IRRELEVANT or analysis.maturity in EXCLUDED_MATURITIES:
            return "drop"
        return "keep"
