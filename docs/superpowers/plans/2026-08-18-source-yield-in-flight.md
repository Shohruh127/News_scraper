# Source Yield: In-Flight Articles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `source_yield` distinguish an article that was rejected from one that is still moving through the pipeline, so the gated Task 3 decision is made on what the numbers mean rather than on what they look like.

**Architecture:** One extra column. `Article.Status` has four states, not three, and the report currently reports three — so an article sitting in `triaged`, waiting for classification, is invisible and reads exactly like a rejection.

**Tech Stack:** Django management command, pytest.

**Spec:** none — this closes a defect found while verifying `docs/superpowers/plans/2026-08-18-source-yield-and-expansion.md`, whose Task 3 remains gated.

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- The suite must stay green: `uv run pytest -q` → **174 passed** before this plan
- Task 3 of the previous plan stays gated. This plan does not unblock it; it makes the decision possible
- One Django app, functions over classes, no abstraction before the second case

---

## Why this change exists

The architect's plan told you `Article.status` moves `fetched → skipped | classified`. That is
wrong. The enum in `apps/digest/models.py` has four states:

```python
class Status(models.TextChoices):
    FETCHED = "fetched"
    TRIAGED = "triaged"
    CLASSIFIED = "classified"
    SKIPPED = "skipped"
```

`source_yield` was built from the wrong description, so it reports `ARTS`, `CLASSIF`, `DIGEST`
and `PUBLISHED` with nothing between the first two. An article that passed triage and is waiting
for classification counts in `ARTS` and in nothing else — identical to one the model rejected.

Measured 2026-08-18, on the eleven sources added that morning:

```
source_yield said              an independent query said
nextgov   25 ARTS   0 CLASSIF   99 articles across the eleven sources:
sifted    25 ARTS   0 CLASSIF     71 triaged   (72% passed triage)
techcrunch 18 ARTS  0 CLASSIF     28 skipped   (irrelevant)

                                 topics assigned by triage:
                                 ai_agents 46 · govtech 9 · robotics 4
                                 speech_voice 3 · startups 3 · safety_security 2
```

Read from the report alone, the first expansion produced nothing and the second should be
cancelled. Read from the data, 72% of it passed triage and the expansion is working.

The report was not lying. It answered the question it was given, and the question was wrong.

**Why the tests did not catch this.** The fixture in
`test_source_yield_counts_the_funnel` creates articles in `CLASSIFIED` and `SKIPPED` only.
No test ever produced a `TRIAGED` article, so no test could notice the state was unreported.
Step 1 below fixes that first, because a column added without a test that fails without it is
the same mistake in a new place.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/digest/management/commands/source_yield.py` | the per-source funnel report | modify |
| `tests/test_connectors.py` | the funnel test fixture and assertions | modify |
| `docs/superpowers/plans/2026-08-18-source-yield-and-expansion.md` | the wrong description an executor will re-read for Task 3 | modify |
| `docs/REMAINING_WORK.md` | record the status machine where the project looks things up | modify |

---

## Task 1: Report in-flight articles

**Files:**
- Modify: `apps/digest/management/commands/source_yield.py`
- Modify: `tests/test_connectors.py` — `test_source_yield_counts_the_funnel`
- Modify: `docs/superpowers/plans/2026-08-18-source-yield-and-expansion.md`
- Modify: `docs/REMAINING_WORK.md`

**Interfaces:**
- Consumes: `Article.Status` from `apps/digest/models.py`
- Produces: no callable. The command gains a `TRIAGED` column between `ARTS` and `CLASSIF`

- [ ] **Step 1: Make the existing test cover the missing state**

In `tests/test_connectors.py`, `test_source_yield_counts_the_funnel` currently builds three
articles:

```python
    for i, status in enumerate(
        [Article.Status.CLASSIFIED, Article.Status.CLASSIFIED, Article.Status.SKIPPED]
    ):
```

Add a fourth, in the state the report cannot currently see:

```python
    for i, status in enumerate(
        [
            Article.Status.CLASSIFIED,
            Article.Status.CLASSIFIED,
            Article.Status.SKIPPED,
            Article.Status.TRIAGED,
        ]
    ):
```

Then replace the assertions at the end of that test. They currently read:

```python
    assert "3" in line       # articles
    assert "2" in line       # classified
    assert "1" in line       # published, the one with a channel_message_id
```

Those pass on any line containing the digits, which is why they never noticed a missing column.
Replace them with positional assertions:

```python
    # Positional, not "is this digit somewhere in the line". The previous assertions passed
    # while a whole column was missing, because the digits they looked for appeared anyway.
    cols = line.split()
    assert cols[0] == "yield_src"
    assert cols[1] == "4", "ARTS counts every article the source produced"
    assert cols[2] == "1", "TRIAGED counts articles still moving, not rejected ones"
    assert cols[3] == "2", "CLASSIF counts articles that finished classification"
    assert cols[4] == "2", "DIGEST counts articles ranking selected"
    assert cols[5] == "1", "PUBLISHED counts articles that reached a reader"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_connectors.py -q -k source_yield_counts
```

Expected: FAIL on `cols[2] == "1"`, because the column does not exist and `cols[2]` currently
holds the classified count.

- [ ] **Step 3: Add the column**

In `apps/digest/management/commands/source_yield.py`, replace the module docstring:

```python
"""Per-source funnel. T1.20 says to keep the sources that earn their place; this measures it.

Every stage is counted separately because they answer different questions. ARTS says the feed
works. TRIAGED says the content is still moving. CLASSIFIED says it survived the model. DIGEST
says ranking chose it. PUBLISHED says it reached a reader.

TRIAGED exists because without it a rejection and a queue look identical. Measured 2026-08-18:
eleven newly added sources showed 0 CLASSIFIED and read as a failed expansion, while 71 of their
99 articles were sitting in `triaged`, waiting for a classification pass that had not run yet.
"""
```

Replace the header line:

```python
        self.stdout.write(
            f"\n  {'SOURCE':<18}{'ARTS':>6}{'TRIAGED':>9}{'CLASSIF':>9}{'DIGEST':>8}"
            f"{'PUBLISHED':>11}{'YIELD':>8}  LAST FETCH"
        )
        self.stdout.write("  " + "-" * 77)
```

Add the count inside the loop, directly above the `classified` line:

```python
            triaged = mine.filter(status=Article.Status.TRIAGED).count()
```

And replace the row line:

```python
            self.stdout.write(
                f"  {source.name:<18}{total:>6}{triaged:>9}{classified:>9}{in_digest:>8}"
                f"{published:>11}{rate:>8}  {last}{flag}"
            )
```

Finally, extend the closing note:

```python
        self.stdout.write(
            "\n  YIELD is published articles over articles fetched. A source with a healthy "
            "feed\n  and a zero yield is producing content that ranking never chooses — but "
            "check\n  TRIAGED first: those articles have not been judged yet.\n"
        )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_connectors.py -q -k source_yield
```

Expected: PASS, 3 passed

- [ ] **Step 5: Run it against the real database**

```bash
uv run python manage.py source_yield
```

Paste the whole table. Then paste this independent count, which must agree with the TRIAGED
column for those sources:

```bash
uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.models import Article
from collections import Counter
new = ['nextgov','fedscoop','statescoop','ec_digital','techcrunch_ai','crunchbase_news','sifted','gh_sherpa_onnx']
print(dict(Counter(a.status for a in Article.objects.filter(source__name__in=new))))
"
```

The two must match. A report that disagrees with a direct count is the defect this plan exists
to close, so if they differ, stop and report rather than adjusting either one.

- [ ] **Step 6: Correct the description an executor will re-read**

In `docs/superpowers/plans/2026-08-18-source-yield-and-expansion.md`, the "Context an engineer
new to this repo needs" section begins:

```
`Article.status` moves `fetched → skipped | classified`. `skipped` covers both the rule
prefilter (paper domains, text too short) and an LLM rejection, so "not classified" is not the
same as "rejected by the model".
```

Replace that paragraph with:

```
`Article.status` moves `fetched → triaged → classified | skipped`. Four states, not three.
An earlier version of this plan said three, and the command built from it could not tell a
rejected article from one still in the queue — see
`docs/superpowers/plans/2026-08-18-source-yield-in-flight.md`.

`skipped` covers both the rule prefilter (paper domains, text too short) and an LLM rejection,
so "not classified" is not the same as "rejected by the model", and `triaged` is neither.
```

- [ ] **Step 7: Record the status machine in the map**

In `docs/REMAINING_WORK.md`, add this row to the table under **### Measured facts an executor
will need**, directly after the `Paper prefilter` row:

```
| Article status machine | `fetched → triaged → classified \| skipped`. Four states. Reading it as three made `source_yield` report a queue as a rejection |
```

- [ ] **Step 8: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `174 passed` and `All checks passed!` — the count does not change, because Step 1
strengthens an existing test rather than adding one.

- [ ] **Step 9: Commit**

```bash
git add apps/digest/management/commands/source_yield.py tests/test_connectors.py \
        docs/superpowers/plans/2026-08-18-source-yield-and-expansion.md \
        docs/REMAINING_WORK.md
git commit -m "Report articles still in flight, so a queue stops reading as a rejection"
```

- [ ] **Step 10: STOP and report**

Task 3 of the previous plan stays gated. Report the table from Step 5 and wait.

---

## Not in this plan, and why

**The `tests/test_llm.py` change made during the previous plan stands.** Adding a Redis mock to
`test_triage_and_classify_batch` was correct: the test reached the live Redis and failed whenever
the evening pipeline held the lock, which it did at 18:00 that day. Verified by running the
pre-change version — it reported `Evening pipeline already running (lock held by
27c18e13c6be:29)` and then `KeyError: 'triaged'`.

The problem was that the change went unreported, not that it was wrong. A swept check found the
lock is used in exactly one place, `apps/digest/tasks.py:229`, and exactly one test touches it,
so no other test needs the same treatment.

**Nothing is rebuilt.** This changes a management command run on the host, not code any container
executes on a schedule.

---

## Self-review

**Coverage**

| Gap found in verification | Step |
|---|---|
| `source_yield` cannot distinguish a queue from a rejection | Steps 1–4 |
| No test ever produced a `TRIAGED` article | Step 1 |
| Assertions passed while a column was missing | Step 1, positional assertions |
| The report was never checked against an independent count | Step 5 |
| The wrong status machine sits in a plan an executor will re-read | Step 6 |
| The status machine is recorded nowhere the project looks it up | Step 7 |

**Placeholder scan:** none. Every step carries its code or command and its expected output.

**Type consistency:** `Article.Status.TRIAGED` is the existing enum member at
`apps/digest/models.py:68`. The column order in the header, the row, and the positional
assertions in Step 1 are the same order: `SOURCE ARTS TRIAGED CLASSIF DIGEST PUBLISHED YIELD`.

**One note for the reviewer.** Step 1 changes assertions that currently pass. That is the point:
they passed while a whole column was missing, because `assert "3" in line` is satisfied by any
`3` anywhere in the row. Weak assertions do not fail — they simply stop being evidence.
