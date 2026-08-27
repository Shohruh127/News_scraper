"""Old two-call post beside new one-call post, for one article per class.

Post quality has no automatic label, so the owner is the judge. This prints both versions
and, with --post, sends them to a test channel so they can be read as real Telegram posts
rather than as terminal text.

No Digest and no DigestItem is created, so this command cannot claim a publishing slot
or put a post into a block. The new path writes nothing at all. The old path does write the
two Analysis rows it always writes, because it runs the real two-stage flow — they are the
same rows the pipeline would write for that article, so a re-run costs those two calls
again rather than corrupting anything.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.digest import llm, translation_gates
from apps.digest.models import Analysis, Article

UZ_LABELS = {
    "general": "UMUMIY",
    "release": "RELIZ",
    "agent": "AGENT",
    "risk": "XAVF",
    "research": "TADQIQOT",
    "product": "MAHSULOT",
    "robotics": "ROBOTOTEXNIKA",
}


class Command(BaseCommand):
    help = "Compare the two-call and one-call editorial paths, one article per class."

    def add_arguments(self, parser):
        parser.add_argument(
            "--post",
            action="store_true",
            help="Also send both versions to TELEGRAM_EVAL_CHANNEL_ID.",
        )
        parser.add_argument(
            "--days", type=int, default=7, help="Window of classified articles (default 7)."
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Number of article comparisons to run; 0 means one per class (default).",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        if options["limit"] < 0:
            raise CommandError("--limit must be 0 or a positive integer.")

        channel = getattr(settings, "TELEGRAM_EVAL_CHANNEL_ID", "")
        if options["post"] and not channel:
            raise CommandError(
                "--post needs TELEGRAM_EVAL_CHANNEL_ID set. It is deliberately unset on the "
                "server so an eval run cannot reach the live channel."
            )

        picked = self._one_article_per_class(options["days"], options["limit"])
        missing = [k for k in UZ_LABELS if k not in picked]
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    "No classified article for: " + ", ".join(UZ_LABELS[k] for k in missing)
                )
            )

        for n, (block_key, article) in enumerate(picked.items(), start=1):
            self._compare(n, block_key, article, post_to=channel if options["post"] else None)

    def _one_article_per_class(self, days: int, limit: int = 0) -> dict[str, Article]:
        from datetime import timedelta

        from django.utils import timezone

        since = timezone.now() - timedelta(days=days)
        picked: dict[str, Article] = {}
        target_count = min(limit, len(UZ_LABELS)) if limit else len(UZ_LABELS)
        candidates = (
            Article.objects.filter(
                analyses__stage=Analysis.Stage.CLASSIFICATION, fetched_at__gte=since
            )
            .distinct()
            .select_related("source")
            .prefetch_related("analyses")
            .order_by("-fetched_at")
        )
        for article in candidates:
            key = llm.shape_for(llm._classified_topic(article))
            picked.setdefault(key, article)
            if len(picked) == target_count:
                break
        return picked

    def _compare(self, n, block_key, article, post_to):
        label = UZ_LABELS[block_key]
        self.stdout.write("\n" + "=" * 72)
        self.stdout.write(f"{n}. {label}  —  {article.title[:60]}")
        self.stdout.write("=" * 72)

        old = self._old_path(article)
        new = self._new_path(article)

        for name, text, cost in (("ESKI", old[0], old[1]), ("YANGI", new[0], new[1])):
            self.stdout.write(f"\n  {name}   ({cost})")
            for line in (text or "(failed)").splitlines():
                self.stdout.write(f"    {line}")

        if post_to:
            self._send(post_to, f"── {n} · {label} · eski ──", old[0])
            self._send(post_to, f"── {n} · {label} · yangi ──", new[0])

    def _old_path(self, article):
        """Run the existing two-stage flow. It writes Analysis rows; that is acceptable
        here because they are the same rows the pipeline would write anyway."""
        try:
            analyses = llm.analyse_for_digest_logic([article.id])
        except Exception as exc:
            return None, f"FAILED — {exc}"
        if not analyses:
            return None, "no usable translation"
        payload = analyses[0].payload
        return self._render(payload, "uz"), "2 calls"

    def _new_path(self, article):
        try:
            result = llm.editorial_uz_for_article(article)
        except Exception as exc:
            return None, f"FAILED — {exc}"
        violations = translation_gates.validate_against_source(
            article_title=article.title,
            article_text=article.extracted_text or "",
            uz_fields=result.payload,
            technical=result.payload.get("technical"),
        )
        cost = f"1 call, {result.input_tokens} in / {result.output_tokens} out"
        if violations:
            cost += "  GATES: " + "; ".join(violations)
        return self._render(result.payload, "uz"), cost

    def _render(self, payload, _lang):
        return "\n".join(
            str(payload.get(k, ""))
            for k in ("headline_uz", "lead_uz", "body_1_uz", "kicker_uz")
            if payload.get(k)
        )

    def _send(self, chat_id, label, text):
        from apps.digest import publish

        publish.send_message(chat_id, label)
        if text:
            publish.send_message(chat_id, text)
