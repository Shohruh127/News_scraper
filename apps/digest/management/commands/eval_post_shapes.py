"""Management command: the same article written both ways, side by side.

Post quality has no automatic label. Triage could be scored because classification supplied
a verdict; nothing supplies a verdict on prose, and mechanical proxies — does body_1 carry a
number, does the kicker restate the lead — measure form, which a bad post passes. So this
prints both versions and the owner is the judge.

Nothing is stored. Two editorial calls per article, so --limit is not optional in practice.
"""

from django.core.management.base import BaseCommand

from apps.digest import llm
from apps.digest.models import Analysis, Article


class Command(BaseCommand):
    help = "Generate each article's post under the general and the per-topic instruction."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=3,
            help="How many comparisons to print. Each costs two editorial calls.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        # `limit` counts comparisons produced, not articles examined. An article whose topic
        # maps to the general instruction yields no comparison — both halves would be the
        # same prompt — so slicing the queryset to `limit` answers a request for five with
        # one and reads like a broken command.
        candidates = (
            Article.objects.filter(analyses__stage=Analysis.Stage.CLASSIFICATION)
            .distinct()
            .select_related("source")
            .prefetch_related("analyses")
            .order_by("-fetched_at")
        )

        compared = 0
        skipped = 0
        for article in candidates:
            if compared >= limit:
                break

            topic = llm._classified_topic(article)
            shape = llm.shape_for(topic)
            if shape == llm.SHAPE_GENERAL:
                skipped += 1
                continue

            compared += 1
            self.stdout.write("\n" + "=" * 72)
            self.stdout.write(f"{article.title[:68]}")
            self.stdout.write(f"topic={topic}  shape={shape}")
            self.stdout.write("=" * 72)

            for label, block_key in (("GENERAL", llm.SHAPE_GENERAL), (shape.upper(), shape)):
                try:
                    result = llm._editorial_call(
                        prompt=llm.EDITORIAL_EN_PROMPT.format(
                            shape=llm.SHAPE_BLOCKS[block_key],
                            title=article.title,
                            source=article.source.name if article.source else "",
                            text=(article.extracted_text or "")[:8000],
                        ),
                        schema=llm.EDITORIAL_EN_SCHEMA,
                        model_cls=llm.EditorialEn,
                        num_predict=llm.settings.EDITORIAL_NUM_PREDICT,
                        provider=llm.settings.EDITORIAL_EN_PROVIDER,
                    )
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f"\n  {label}: FAILED — {exc}"))
                    continue

                p = result.payload
                self.stdout.write(
                    f"\n  {label}   ({result.input_tokens} in / {result.output_tokens} out)"
                )
                self.stdout.write(f"    headline  {p.get('headline_en', '')}")
                self.stdout.write(f"    lead      {p.get('lead_en', '')}")
                self.stdout.write(f"    body_1    {p.get('body_1_en', '')}")
                self.stdout.write(f"    kicker    {p.get('kicker_en', '')}")

        if not compared:
            self.stdout.write(
                f"No comparisons to print. {skipped} classified article(s) were examined and"
                " every one maps to the general instruction, so both halves would be"
                " identical."
            )
            return

        self.stdout.write(
            f"\n  {compared} comparison(s); {skipped} article(s) skipped as general-shape."
            "\n  Read the pairs. The question is which lead answers what a reader of this"
            "\n  kind of story actually wants first, not which reads more smoothly."
        )
