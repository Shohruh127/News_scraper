# Source Yield and Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each source's contribution measurable, put the live registry back in the seed file, and only then add twelve more sources.

**Architecture:** A management command reports the per-source funnel from data already in the database — articles fetched, classified, selected into a digest, published. No new model field and no "shadow mode": a source that earns nothing shows a zero, which is the measurement.

**Tech Stack:** Django management commands, PostgreSQL, pytest.

**Spec:** none — bounded change agreed in chat on 2026-08-18. Continues T1.20 in `docs/REMAINING_WORK.md` §4 Phase 5.

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- The suite must stay green: `uv run pytest -q` → **170 passed** before this plan
- **Task 3 was gated and is now open**, 2026-08-19. The measurement it waited for arrived: the eleven sources added on 2026-08-18 produced 99 articles and 7 classified, against `hn` at 164 and 40. The owner's call is to add all twelve rather than pre-select, and to remove what does not earn its place after a week of `source_yield`
- Adding a source is a data change. It must go through `seed_sources.py`, never an ad-hoc script
- `seed_sources.py` is idempotent and must stay so
- One Django app, functions over classes, no abstraction before the second case

---

## Why this change exists

T1.20 says to keep the six to eight sources that "earn their place on usable items, extraction
failures, and share of items reaching a digest". That decision has a criterion and no instrument.
Every time the question has come up, the numbers were produced by an ad-hoc script.

Measured 2026-08-18, hours after eleven sources were added:

```
source            arts   triaged   classified   in digest
nextgov      *      25         0            0           0
sifted       *      25         0            0           0
techcrunch_ai*      18         0            0           0
crunchbase   *      10         0            0           0
fedscoop     *      10         0            0           0
hn                 164        90           24          15
gh_ollama            6         6            5           5
```

The eleven new sources have produced 99 articles and zero evidence: their articles sit in a
186-article backlog waiting for the 18:00 triage. Adding twelve more before that run would stack
an unmeasured change on an unmeasured change, and tomorrow nobody could say which expansion did
what.

A second gap surfaced while planning this. `seed_sources.py` holds **12** sources; the database
holds **23**. The eleven added on 2026-08-18 went in through a one-off script and never reached
the seed file, so a fresh environment comes up with eleven sources missing.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/digest/management/commands/source_yield.py` | the per-source funnel report | create |
| `apps/digest/management/commands/seed_sources.py` | the reproducible source registry | modify twice |
| `tests/test_connectors.py` | command output and seed coverage | modify |

### Context an engineer new to this repo needs

`Article.status` moves `fetched → triaged → classified | skipped`. Four states, not three.
An earlier version of this plan said three, and the command built from it could not tell a
rejected article from one still in the queue — see
`docs/superpowers/plans/2026-08-18-source-yield-in-flight.md`.

`skipped` covers both the rule prefilter (paper domains, text too short) and an LLM rejection,
so "not classified" is not the same as "rejected by the model", and `triaged` is neither.

A `DigestItem` row means the article was *selected* into a digest. A non-null
`channel_message_id` on that row means it was actually *published*. The gap between the two is
the kill switch and publishing failures, so both are worth reporting separately.

`seed_sources.py` is idempotent by design — it uses `get_or_create` keyed on `name`. Running it
against a populated database must add the missing rows and leave the existing ones alone.

---

## Task 1: The `source_yield` command

**Files:**
- Create: `apps/digest/management/commands/source_yield.py`
- Test: `tests/test_connectors.py`

**Interfaces:**
- Consumes: nothing
- Produces: `source_yield(days: int = 0)` as a management command only. Tasks 2 and 3 do not import it

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_connectors.py`:

```python
@pytest.mark.django_db
def test_source_yield_counts_the_funnel():
    """Each stage is counted separately, because they answer different questions."""
    from io import StringIO

    from django.core.management import call_command

    from apps.digest.models import Article, Digest, DigestItem, Source

    src = Source.objects.create(
        name="yield_src", connector=Source.Connector.RSS, url="https://example.com/rss"
    )
    made = []
    for i, status in enumerate(
        [Article.Status.CLASSIFIED, Article.Status.CLASSIFIED, Article.Status.SKIPPED]
    ):
        made.append(
            Article.objects.create(
                source=src,
                canonical_url=f"https://example.com/y{i}",
                content_hash=f"y{i}",
                title=f"Article {i}",
                extracted_text="Body " * 60,
                status=status,
            )
        )
    digest = Digest.objects.create(digest_date=timezone.localdate())
    DigestItem.objects.create(
        digest=digest, article=made[0], position=1, score=0.9, channel_message_id=42
    )
    DigestItem.objects.create(digest=digest, article=made[1], position=2, score=0.8)

    out = StringIO()
    call_command("source_yield", stdout=out)
    line = next(li for li in out.getvalue().splitlines() if "yield_src" in li)

    assert "3" in line       # articles
    assert "2" in line       # classified
    assert "1" in line       # published, the one with a channel_message_id


@pytest.mark.django_db
def test_source_yield_lists_a_source_that_produced_nothing():
    """A zero is the measurement. A source missing from the report cannot be judged."""
    from io import StringIO

    from django.core.management import call_command

    from apps.digest.models import Source

    Source.objects.create(
        name="silent_src", connector=Source.Connector.RSS, url="https://example.com/quiet"
    )

    out = StringIO()
    call_command("source_yield", stdout=out)

    assert "silent_src" in out.getvalue()


@pytest.mark.django_db
def test_source_yield_days_window_excludes_older_articles():
    """`--days` answers "what has this source done lately", not "ever"."""
    from datetime import timedelta
    from io import StringIO

    from django.core.management import call_command

    from apps.digest.models import Article, Source

    src = Source.objects.create(
        name="window_src", connector=Source.Connector.RSS, url="https://example.com/w"
    )
    old = Article.objects.create(
        source=src,
        canonical_url="https://example.com/old",
        content_hash="old",
        title="Old",
        extracted_text="Body " * 60,
        status=Article.Status.CLASSIFIED,
    )
    Article.objects.filter(pk=old.pk).update(fetched_at=timezone.now() - timedelta(days=30))

    out = StringIO()
    call_command("source_yield", "--days", "7", stdout=out)
    line = next(li for li in out.getvalue().splitlines() if "window_src" in li)

    assert line.split()[1] == "0"
```

No import changes are needed: `tests/test_connectors.py` already imports `pytest`, `timezone`
and `Source`.

That file also carries `pytestmark = pytest.mark.django_db` at module level, so every test in it
already has database access. The `@pytest.mark.django_db` decorators above are therefore
redundant — harmless, and kept so each test states its own requirement rather than depending on
a line at the top of a long file.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_connectors.py -q -k source_yield
```

Expected: FAIL, `CommandError: Unknown command: 'source_yield'`

- [ ] **Step 3: Write the command**

Create `apps/digest/management/commands/source_yield.py`:

```python
"""Per-source funnel. T1.20 says to keep the sources that earn their place; this measures it.

Every stage is counted separately because they answer different questions. ARTS says the feed
works. CLASSIFIED says the content survives triage. DIGEST says ranking chose it. PUBLISHED says
it reached a reader. A source can pass the first and fail the last, and only the last one counts.
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
            f"\n  {'SOURCE':<18}{'ARTS':>6}{'CLASSIF':>9}{'DIGEST':>8}"
            f"{'PUBLISHED':>11}{'YIELD':>8}  LAST FETCH"
        )
        self.stdout.write("  " + "-" * 68)

        for source in Source.objects.order_by("name"):
            mine = articles.filter(source=source)
            total = mine.count()
            classified = mine.filter(status=Article.Status.CLASSIFIED).count()
            in_digest = DigestItem.objects.filter(article__in=mine).count()
            published = DigestItem.objects.filter(
                article__in=mine, channel_message_id__isnull=False
            ).count()
            rate = f"{100 * published / total:.1f}%" if total else "-"
            last = source.last_fetched_at.date() if source.last_fetched_at else "never"
            flag = "" if source.enabled else "  (disabled)"
            self.stdout.write(
                f"  {source.name:<18}{total:>6}{classified:>9}{in_digest:>8}"
                f"{published:>11}{rate:>8}  {last}{flag}"
            )

        self.stdout.write(
            "\n  YIELD is published articles over articles fetched. A source with a healthy "
            "feed\n  and a zero yield is producing content that ranking never chooses.\n"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_connectors.py -q -k source_yield
```

Expected: PASS, 3 passed

- [ ] **Step 5: Run it against the real database**

```bash
uv run python manage.py source_yield
```

Paste the output into your report. This is the number T1.20's decision rests on.

- [ ] **Step 6: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `173 passed` and `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add apps/digest/management/commands/source_yield.py tests/test_connectors.py
git commit -m "Measure what each source actually contributes"
```

---

## Task 2: Put the live registry back in the seed file

**Files:**
- Modify: `apps/digest/management/commands/seed_sources.py` — the `SOURCES` list
- Test: `tests/test_connectors.py`

**Interfaces:**
- Consumes: nothing
- Produces: a `SOURCES` list covering every source currently live. Task 3 appends to the same list

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connectors.py`:

```python
def test_seed_covers_every_source_added_since_the_file_was_written():
    """A source added by a one-off script is invisible to a fresh environment.

    Measured 2026-08-18: the seed held 12 sources and the database held 23. Eleven had been
    inserted directly and never reached the file.
    """
    from apps.digest.management.commands.seed_sources import SOURCES

    names = {entry["name"] for entry in SOURCES}
    added_2026_08_18 = {
        "nextgov",
        "fedscoop",
        "statescoop",
        "ec_digital",
        "gds_uk",
        "gh_sherpa_onnx",
        "gh_pyannote",
        "gh_whisperx",
        "techcrunch_ai",
        "crunchbase_news",
        "sifted",
    }

    assert added_2026_08_18 <= names, f"missing from the seed: {added_2026_08_18 - names}"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_connectors.py -q -k seed_covers
```

Expected: FAIL, listing all eleven names

- [ ] **Step 3: Add the eleven entries**

Append these to the `SOURCES` list in `apps/digest/management/commands/seed_sources.py`, before
the closing `]`. All eleven were verified live on 2026-08-18.

```python
    # --- Added 2026-08-18. Feeds verified live the same day. -------------------
    # govtech had no source at all before this; these five are its whole supply.
    {
        "name": "nextgov",
        "connector": "rss",
        "priority": 50,
        "url": "https://www.nextgov.com/rss/all/",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "fedscoop",
        "connector": "rss",
        "priority": 50,
        "url": "https://fedscoop.com/feed/",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "statescoop",
        "connector": "rss",
        "priority": 60,
        "url": "https://statescoop.com/feed/",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "ec_digital",
        "connector": "rss",
        "priority": 50,
        "url": "https://digital-strategy.ec.europa.eu/en/rss.xml",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "gds_uk",
        "connector": "rss",
        "priority": 60,
        "url": "https://gds.blog.gov.uk/feed/",
        "stream": Topic.GOVTECH,
    },
    # speech_voice: the three large vendors publish no feed, so release feeds are the supply.
    {
        "name": "gh_sherpa_onnx",
        "connector": "github",
        "priority": 50,
        "url": "https://github.com/k2-fsa/sherpa-onnx",
        "stream": Topic.SPEECH_VOICE,
        "config": {"repo": "k2-fsa/sherpa-onnx"},
    },
    {
        "name": "gh_pyannote",
        "connector": "github",
        "priority": 50,
        "url": "https://github.com/pyannote/pyannote-audio",
        "stream": Topic.SPEECH_VOICE,
        "config": {"repo": "pyannote/pyannote-audio"},
    },
    {
        "name": "gh_whisperx",
        "connector": "github",
        "priority": 60,
        "url": "https://github.com/m-bain/whisperX",
        "stream": Topic.SPEECH_VOICE,
        "config": {"repo": "m-bain/whisperX"},
    },
    # startups
    {
        "name": "techcrunch_ai",
        "connector": "rss",
        "priority": 50,
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "stream": Topic.STARTUPS,
    },
    {
        "name": "crunchbase_news",
        "connector": "rss",
        "priority": 60,
        "url": "https://news.crunchbase.com/feed/",
        "stream": Topic.STARTUPS,
    },
    {
        "name": "sifted",
        "connector": "rss",
        "priority": 60,
        "url": "https://sifted.eu/feed",
        "stream": Topic.STARTUPS,
    },
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_connectors.py -q -k seed_covers
```

Expected: PASS

- [ ] **Step 5: Confirm the seed is still idempotent against the live database**

```bash
uv run python manage.py seed_sources
uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.models import Source; print('sources:', Source.objects.count())
"
```

Expected: `sources: 23` — unchanged, because every entry already exists. If the count rises, an
entry's `name` does not match the live row and a duplicate was created. Stop and report.

- [ ] **Step 6: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `174 passed` and `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add apps/digest/management/commands/seed_sources.py tests/test_connectors.py
git commit -m "Return the eleven sources added by hand to the seed file"
```

- [ ] **Step 8: Report and continue**

Report the output of `uv run python manage.py source_yield` from Task 1 Step 5, then go straight
on to Task 3. The gate that used to sit here was lifted on 2026-08-19.

---

## Task 3: Add the twelve new sources — OPEN

**Opened 2026-08-19 by the project owner.** Add all twelve. The split measured on 2026-08-18 —
GitHub release feeds converting at 100% and RSS news feeds at 5% — argued for adding only the
eight release feeds, and the owner chose the wider set deliberately: a week of `source_yield`
answers the question better than a pre-selection does, and removing a source is one line.

Run the whole task. Do not stop after step 4 this time.

**Files:**
- Modify: `apps/digest/management/commands/seed_sources.py`
- Test: `tests/test_connectors.py`

**Interfaces:**
- Consumes: the `SOURCES` list from Task 2
- Produces: nothing new

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connectors.py`:

```python
def test_seed_contains_the_second_expansion():
    """All twelve feeds were verified live on 2026-08-18 before being written down."""
    from apps.digest.management.commands.seed_sources import SOURCES

    names = {entry["name"] for entry in SOURCES}
    expansion = {
        "mistral_news",
        "gh_openai_agents",
        "gh_a2a",
        "gh_vllm",
        "gh_transformers",
        "gh_llamacpp",
        "gh_lerobot",
        "gh_fineract",
        "bis_fsi",
        "uk_gov_tech",
        "gh_garak",
        "modal_blog",
    }

    assert expansion <= names, f"missing: {expansion - names}"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_connectors.py -q -k second_expansion
```

Expected: FAIL, listing all twelve

- [ ] **Step 3: Add the twelve entries**

Append to `SOURCES`, before the closing `]`:

```python
    # --- Second expansion, 2026-08-18. Feeds verified live the same day. -------
    # Six of these publish less often than ARTICLE_MAX_AGE_DAYS, so they contribute
    # nothing on most days without being broken: gh_a2a last released 82 days ago,
    # uk_gov_tech 42, gh_fineract 34, gh_lerobot 15, gh_garak 14, gh_transformers 8.
    {
        "name": "mistral_news",
        "connector": "rss",
        "priority": 90,
        "url": "https://mistral.ai/news/rss",
        "stream": Topic.FRONTIER_MODELS,
    },
    {
        "name": "gh_openai_agents",
        "connector": "github",
        "priority": 80,
        "url": "https://github.com/openai/openai-agents-python",
        "stream": Topic.AI_AGENTS,
        "config": {"repo": "openai/openai-agents-python"},
    },
    {
        "name": "gh_a2a",
        "connector": "github",
        "priority": 75,
        "url": "https://github.com/a2aproject/A2A",
        "stream": Topic.AI_AGENTS,
        "config": {"repo": "a2aproject/A2A"},
    },
    {
        "name": "gh_vllm",
        "connector": "github",
        "priority": 80,
        "url": "https://github.com/vllm-project/vllm",
        "stream": Topic.PRODUCTION_ENGINEERING,
        "config": {"repo": "vllm-project/vllm"},
    },
    {
        "name": "gh_transformers",
        "connector": "github",
        "priority": 75,
        "url": "https://github.com/huggingface/transformers",
        "stream": Topic.PRODUCTION_ENGINEERING,
        "config": {"repo": "huggingface/transformers"},
    },
    {
        "name": "gh_llamacpp",
        "connector": "github",
        "priority": 75,
        "url": "https://github.com/ggml-org/llama.cpp",
        "stream": Topic.PRODUCTION_ENGINEERING,
        "config": {"repo": "ggml-org/llama.cpp"},
    },
    {
        "name": "gh_lerobot",
        "connector": "github",
        "priority": 70,
        "url": "https://github.com/huggingface/lerobot",
        "stream": Topic.ROBOTICS,
        "config": {"repo": "huggingface/lerobot"},
    },
    {
        "name": "gh_fineract",
        "connector": "github",
        "priority": 70,
        "url": "https://github.com/apache/fineract",
        "stream": Topic.FINTECH,
        "config": {"repo": "apache/fineract"},
    },
    {
        "name": "bis_fsi",
        "connector": "rss",
        "priority": 65,
        "url": "https://www.bis.org/doclist/bis_fsi_publs.rss",
        "stream": Topic.FINTECH,
    },
    {
        "name": "uk_gov_tech",
        "connector": "rss",
        "priority": 70,
        "url": "https://technology.blog.gov.uk/feed/",
        "stream": Topic.GOVTECH,
    },
    {
        "name": "gh_garak",
        "connector": "github",
        "priority": 75,
        "url": "https://github.com/NVIDIA/garak",
        "stream": Topic.SAFETY_SECURITY,
        "config": {"repo": "NVIDIA/garak"},
    },
    {
        "name": "modal_blog",
        "connector": "rss",
        "priority": 65,
        "url": "https://modal.com/blog/atom.xml",
        "stream": Topic.STARTUPS,
    },
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_connectors.py -q -k second_expansion
```

Expected: PASS

- [ ] **Step 5: Seed them and fetch once**

```bash
uv run python manage.py seed_sources
uv run python manage.py fetch_sources
```

Paste the fetch table. A source reporting `FAILED` needs reporting, not retrying.

- [ ] **Step 6: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `175 passed` and `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add apps/digest/management/commands/seed_sources.py tests/test_connectors.py
git commit -m "Add the second source expansion, twelve feeds verified live"
```

---

## After the plan

Nothing here changes application code, so **no rebuild is needed** for Tasks 1 and 2 — the
commands run on the host. Task 3 writes rows to the shared database, which every container reads,
so no rebuild there either.

A week after Task 3, run `source_yield --days 7` and cut the sources that earn nothing. That is a
decision, not a task, and it belongs to the project owner.

---

## Self-review

**Coverage of the agreed design**

| Agreed in chat | Task |
|---|---|
| Build the instrument before making the decision | Task 1 |
| Per-source funnel: articles, classified, digest, published | Task 1 Step 3 |
| Measure the eleven sources added this morning before adding more | Global Constraints, Task 2 Step 8 |
| Add the twelve, through `seed_sources.py` | Task 3 |
| Sources are a data change, never an ad-hoc script | Task 2 exists because that rule was broken |

**Placeholder scan:** none. Every step carries its code or command and its expected output.

**Type consistency:** `SOURCES` entries use the keys the existing file uses — `name`,
`connector`, `priority`, `url`, `stream`, and `config` where a connector needs it. `Topic` values
referenced (`GOVTECH`, `SPEECH_VOICE`, `STARTUPS`, `FRONTIER_MODELS`, `AI_AGENTS`,
`PRODUCTION_ENGINEERING`, `ROBOTICS`, `FINTECH`, `SAFETY_SECURITY`) all exist in the `Topic`
choices listed in `apps/digest/models.py`.

**One thing worth flagging to the reviewer:** Task 3 is written but must not run yet. A plan that
contains a task nobody is allowed to start is unusual; it is written now so the twelve verified
URLs are recorded while the verification is fresh, rather than re-verified later from memory.
