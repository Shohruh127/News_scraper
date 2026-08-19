"""Return paper articles that artifact verification stranded in `skipped`.

Two groups qualify, and only two:

* `artifact_verified=True` — the repository was verified and the article was skipped anyway,
  because the backfill filled the fields without moving the status. Measured 2026-08-19: six
  articles, none of which had ever re-entered the pipeline.
* `artifact_verified IS NULL` with a repository link in the text — GitHub never answered, so
  nothing was stored. These retry.

`artifact_verified=False` is a settled answer and is never reset, which is what stops this
command from looping. Everything moves to `fetched`, and the next evening run triages it.
"""

from django.core.management.base import BaseCommand

from apps.digest import artifacts
from apps.digest.llm import PAPER_DOMAINS
from apps.digest.models import Article


def _is_paper(article: Article) -> bool:
    url = (article.canonical_url or "").lower()
    return any(domain in url for domain in PAPER_DOMAINS) or (
        article.source is not None and article.source.connector == "hf"
    )


class Command(BaseCommand):
    help = "Move stranded, artifact-bearing paper articles back to fetched for re-triage"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would move without changing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        skipped = (
            Article.objects.filter(status=Article.Status.SKIPPED)
            .exclude(artifact_verified=False)
            .select_related("source")
        )

        selected = []
        for article in skipped:
            if not _is_paper(article):
                continue
            if article.artifact_verified is True:
                selected.append((article, "verified"))
            elif artifacts.find_repo_url(article.extracted_text or "", article.title or ""):
                selected.append((article, "unanswered"))

        for article, reason in selected:
            self.stdout.write(f"  [{reason}] {article.canonical_url}  {article.title[:60]}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"\n  Would return {len(selected)} article(s) to fetched.")
            )
            return

        for article, _reason in selected:
            article.status = Article.Status.FETCHED
            article.save(update_fields=["status"])

        self.stdout.write(
            self.style.SUCCESS(
                f"\n  Returned {len(selected)} article(s) to fetched. "
                "The next evening run will triage them."
            )
        )
