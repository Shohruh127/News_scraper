# Subject Diversity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a single digest opening with three consecutive releases from the same project by capping how many items may share a subject.

**Architecture:** A pure function derives a subject identity from an article URL. The existing diversification loop in `select_digest_candidates` gains a second counter keyed on `(subject_key, topic)`, beside the per-topic counter it already keeps. No new pipeline stage and no new model field. Backfill is already free: the loop continues past a rejected candidate instead of breaking, so freed slots are filled from lower-ranked candidates.

**Tech Stack:** Python 3.13, Django 6.0, pytest + pytest-django, ruff. Run everything through `uv run`.

**Spec:** `docs/superpowers/specs/2026-08-18-digest-subject-diversity-design.md`

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. Every file must pass `uv run ruff check .`
- `DIGEST_MAX_PER_SUBJECT` default is `1`
- `SUBJECT_CODE_HOSTS = ("github.com", "gitlab.com", "huggingface.co")`
- The subject key uses the **network location**, not the registrable domain. `gds.blog.gov.uk` and `technology.blog.gov.uk` are unrelated blogs that both reduce to `gov.uk`
- A host counts as a code host only on **exact equality**, never a suffix match, so `raw.githubusercontent.com` keeps its own key
- The counter key is the pair `(subject_key, topic)`, never `subject_key` alone
- The rule runs **after** `clustering.cluster_candidates`, so a merged cluster counts once
- One Django app, functions over classes, no abstraction before the second case

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `config/settings.py` | the two new constants, with the measurement that produced them | append a section |
| `apps/digest/ranking.py` | `subject_key()` and the extra loop condition | add one function, edit one loop |
| `tests/test_ranking.py` | unit tests for the key, selection tests for the rule | append tests |

Nothing else is touched. `clustering.py`, the models and the templates are unaffected.

### Context an engineer new to this repo needs

`select_digest_candidates(target_date)` in `apps/digest/ranking.py` returns a list of
`(Article, Analysis, float, list[Article])` tuples — article, its classification analysis, its
score, and any secondary articles that clustering merged into it. `compose_digest` turns that
list into `Digest` and `DigestItem` rows.

`Analysis.topic` and `Analysis.maturity` are properties that read the JSON `payload`, where the
keys are `primary_topic` and `maturity`. Tests therefore build a classification by writing
`payload={"primary_topic": ..., "maturity": ...}`.

Articles whose maturity is in `EXCLUDED_MATURITIES` (`announcement_only`, `paper_only`) or whose
topic is `irrelevant` are dropped **before** the diversification loop and never reach this rule.

---

## Task 1: Subject key derivation

**Files:**
- Modify: `config/settings.py` (append at end of file)
- Modify: `apps/digest/ranking.py` (add import, add function after `calculate_score`)
- Test: `tests/test_ranking.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `subject_key(url: str) -> str` in `apps.digest.ranking`, and the settings
  `DIGEST_MAX_PER_SUBJECT: int` and `SUBJECT_CODE_HOSTS: tuple[str, ...]`. Task 2 imports the
  function by that exact name and reads both settings via `getattr(settings, ..., default)`.

- [ ] **Step 1: Add the settings**

Append to the end of `config/settings.py`:

```python
# --- Subject diversity -------------------------------------------------------
# Digest #11 opened with three consecutive Ollama releases and two DeepSeek posts: five of
# twelve items covering two stories, with every component behaving correctly. Clustering
# scored the Ollama pairs 0.093-0.149 because they really are different releases, ranking
# scored all three at 0.82 because each is a real release, and DIGEST_MAX_PER_TOPIC allowed
# exactly three. Variety belonged to no component.
#
# Unlike CLUSTER_JACCARD_THRESHOLD, this IS a knob. That threshold is settled by a 0.79
# separation gap in the measurement; the choice between one and two items per subject is a
# judgement about how the channel reads. 1 is the default because it produced the correct
# result on digest #11.
#
# See docs/superpowers/specs/2026-08-18-digest-subject-diversity-design.md
DIGEST_MAX_PER_SUBJECT = env.int("DIGEST_MAX_PER_SUBJECT", default=1)
#: Hosts that carry many unrelated projects, where the owner segment is part of the identity.
#: Matched by exact equality, so raw.githubusercontent.com is an ordinary host.
SUBJECT_CODE_HOSTS = ("github.com", "gitlab.com", "huggingface.co")
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_ranking.py`:

```python
@pytest.mark.parametrize(
    "url, expected",
    [
        # Code hosts keep the owner segment: one host carries unrelated projects.
        ("https://github.com/ollama/ollama/releases/tag/v0.32.10", "github.com/ollama"),
        ("https://github.com/k2-fsa/sherpa-onnx/releases/tag/v1.12.15", "github.com/k2-fsa"),
        # A leading www. is not part of the identity.
        ("https://www.anthropic.com/news/claude-opus-5", "anthropic.com"),
        ("https://anthropic.com/news/fable-5-safeguards", "anthropic.com"),
        # Both DeepSeek articles in digest #11 resolve to one subject.
        ("https://api-docs.deepseek.com/guides/v4-pro", "api-docs.deepseek.com"),
        ("https://api-docs.deepseek.com/news/pricing", "api-docs.deepseek.com"),
        # Two unrelated government blogs must not collapse: this is why the network
        # location is used rather than the registrable domain, which is gov.uk for both.
        ("https://gds.blog.gov.uk/2026/08/06/a-post/", "gds.blog.gov.uk"),
        ("https://technology.blog.gov.uk/2026/07/07/another/", "technology.blog.gov.uk"),
        # Equality, not a suffix test.
        (
            "https://raw.githubusercontent.com/ollama/ollama/main/README.md",
            "raw.githubusercontent.com",
        ),
        # A code host with no path segment falls back to the bare host.
        ("https://github.com", "github.com"),
        # A port is not part of the identity.
        ("https://example.com:8443/post", "example.com"),
    ],
)
def test_subject_key(url, expected):
    assert ranking.subject_key(url) == expected
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
uv run pytest tests/test_ranking.py::test_subject_key -q
```

Expected: FAIL, `AttributeError: module 'apps.digest.ranking' has no attribute 'subject_key'`

- [ ] **Step 4: Write the implementation**

Add `from urllib.parse import urlparse` to the standard-library imports at the top of
`apps/digest/ranking.py`, directly below the `from datetime import time as dt_time` line:

```python
from urllib.parse import urlparse
```

Then add this function immediately after `calculate_score`:

```python
def subject_key(url: str) -> str:
    """Identity of the thing an article is about, derived from its URL alone.

    The network location without a leading `www.`. For hosts that carry many unrelated
    projects, the owner segment is appended, so github.com/ollama and github.com/k2-fsa are
    different subjects rather than one.

    The network location is used rather than the registrable domain because gds.blog.gov.uk
    and technology.blog.gov.uk are unrelated government blogs that both reduce to gov.uk. It
    also needs no public-suffix list, so there is nothing to keep updated.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
    if host in getattr(settings, "SUBJECT_CODE_HOSTS", ()):
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments:
            return f"{host}/{segments[0]}"
    return host
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
uv run pytest tests/test_ranking.py::test_subject_key -q
```

Expected: PASS, 11 passed

- [ ] **Step 6: Run ruff**

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add config/settings.py apps/digest/ranking.py tests/test_ranking.py
git commit -m "Derive a subject key from the article URL"
```

---

## Task 2: Apply the cap in candidate selection

**Files:**
- Modify: `apps/digest/ranking.py:139-150` (the diversification loop)
- Test: `tests/test_ranking.py` (append)

**Interfaces:**
- Consumes: `ranking.subject_key(url: str) -> str`, `settings.DIGEST_MAX_PER_SUBJECT: int` from Task 1
- Produces: no new callable. `select_digest_candidates` keeps its existing signature and
  return type, `list[tuple[Article, Analysis, float, list[Article]]]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ranking.py`. The bodies are deliberately unlike each other: clustering
runs before this rule and merges articles whose character 5-gram Jaccard reaches 0.80, so
near-identical filler text would merge the fixture articles and mask what is being tested.

```python
@pytest.fixture
def repetition_articles(db, source):
    """Digest #11's opening, reproduced: three Ollama releases, two DeepSeek posts,
    two Anthropic posts.

    Scores descend down the list so selection order is predictable. Bodies share no
    vocabulary, so clustering leaves all seven as separate candidates.
    """
    spec = [
        (
            "https://github.com/ollama/ollama/releases/tag/v0.32.10",
            "production_engineering",
            9,
            "Ollama changes the default repeat penalty for local inference runs. ",
        ),
        (
            "https://github.com/ollama/ollama/releases/tag/v0.32.9",
            "production_engineering",
            8,
            "Nemotron Lightning arrives with fresh agent tooling and driver support. ",
        ),
        (
            "https://github.com/ollama/ollama/releases/tag/v0.32.8",
            "production_engineering",
            7,
            "Muse Glimmer joins the coding lineup for editor integrations everywhere. ",
        ),
        (
            "https://api-docs.deepseek.com/guides/v4-pro",
            "frontier_models",
            6,
            "A quiet publication describes the reasoning system behind version four. ",
        ),
        (
            "https://api-docs.deepseek.com/news/pricing",
            "frontier_models",
            5,
            "Peak and off peak tariffs now apply across every inference endpoint. ",
        ),
        (
            "https://www.anthropic.com/news/claude-opus-5",
            "frontier_models",
            4,
            "Introducing the most capable frontier assistant this laboratory has built. ",
        ),
        (
            "https://www.anthropic.com/news/fable-5-safeguards",
            "safety_security",
            3,
            "Biology risk evaluations were tightened substantially during this quarter. ",
        ),
    ]
    made = []
    for index, (url, topic, novelty, body) in enumerate(spec):
        article = Article.objects.create(
            source=source,
            canonical_url=url,
            content_hash=f"rep{index}",
            title=f"Repetition fixture {index}",
            extracted_text=body * 30,
            status=Article.Status.CLASSIFIED,
        )
        Analysis.objects.create(
            article=article,
            stage=Analysis.Stage.CLASSIFICATION,
            model_tag="gemma4:31b",
            payload={
                "primary_topic": topic,
                "maturity": "live_product",
                "novelty": novelty,
                "evidence": novelty,
                "production_readiness": novelty,
                "reason": "fixture",
            },
            latency_ms=1000,
        )
        make_editorial(article)
        made.append(article)
    return made


def _selected_urls(candidates):
    return [article.canonical_url for article, _, _, _ in candidates]


def test_repetitive_subjects_are_dropped(repetition_articles):
    """The failure this rule exists for: five of twelve published items were two stories."""
    urls = _selected_urls(ranking.select_digest_candidates())

    assert "https://github.com/ollama/ollama/releases/tag/v0.32.10" in urls
    assert "https://github.com/ollama/ollama/releases/tag/v0.32.9" not in urls
    assert "https://github.com/ollama/ollama/releases/tag/v0.32.8" not in urls
    assert "https://api-docs.deepseek.com/guides/v4-pro" in urls
    assert "https://api-docs.deepseek.com/news/pricing" not in urls


def test_same_subject_different_topic_both_survive(repetition_articles):
    """The over-filtering guard, and it matters more than the drop assertions.

    A filter's loud failure is dropping too little; its silent failure is dropping something
    nobody notices is missing. Both Anthropic posts are legitimate and unrelated, and a key
    of subject alone would have collapsed them.
    """
    urls = _selected_urls(ranking.select_digest_candidates())

    assert "https://www.anthropic.com/news/claude-opus-5" in urls
    assert "https://www.anthropic.com/news/fable-5-safeguards" in urls


def test_backfill_keeps_the_digest_at_its_cap(repetition_articles, settings):
    """Freed slots are filled from lower-ranked candidates; the digest does not shrink.

    Without backfill the loop would stop at the first rejection and return one item.
    """
    settings.DIGEST_MAX_ITEMS = 3

    assert len(ranking.select_digest_candidates()) == 3


def test_rule_is_silent_when_every_subject_is_distinct(db, source):
    """Seven unrelated stories from seven sites must all survive."""
    bodies = [
        "Alpha describes a storage engine rewrite with measured throughput gains. ",
        "Bravo reports on a scheduler that reorders work across many machines. ",
        "Charlie documents a compiler pass that removes redundant memory loads. ",
    ]
    for index, body in enumerate(bodies):
        article = Article.objects.create(
            source=source,
            canonical_url=f"https://site{index}.example/post",
            content_hash=f"dist{index}",
            title=f"Distinct story {index}",
            extracted_text=body * 30,
            status=Article.Status.CLASSIFIED,
        )
        Analysis.objects.create(
            article=article,
            stage=Analysis.Stage.CLASSIFICATION,
            model_tag="gemma4:31b",
            payload={
                "primary_topic": "ai_agents",
                "maturity": "live_product",
                "novelty": 8,
                "evidence": 8,
                "production_readiness": 8,
                "reason": "fixture",
            },
            latency_ms=1000,
        )
        make_editorial(article)

    assert len(ranking.select_digest_candidates()) == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_ranking.py -q -k "repetitive or same_subject or backfill_keeps or silent"
```

Expected: `1 failed, 3 passed`.

Only `test_repetitive_subjects_are_dropped` fails, on the assertion that `v0.32.9` is absent.
The other three are guards rather than drivers — they describe behaviour that is already
correct and must survive the change:

| Test | Before | After |
|---|---|---|
| `test_repetitive_subjects_are_dropped` | FAIL | PASS |
| `test_same_subject_different_topic_both_survive` | PASS | PASS |
| `test_backfill_keeps_the_digest_at_its_cap` | PASS | PASS |
| `test_rule_is_silent_when_every_subject_is_distinct` | PASS | PASS |

If any guard is red before you touch `ranking.py`, the fixture is wrong, not the code — most
likely two fixture bodies are similar enough that clustering merged them. Fix the fixture
before continuing.

- [ ] **Step 3: Apply the rule**

In `apps/digest/ranking.py`, read `max_per_subject` next to the existing limits. Find:

```python
    max_items = getattr(settings, "DIGEST_MAX_ITEMS", 7)
    max_per_topic = getattr(settings, "DIGEST_MAX_PER_TOPIC", 2)
```

and add a third line:

```python
    max_items = getattr(settings, "DIGEST_MAX_ITEMS", 7)
    max_per_topic = getattr(settings, "DIGEST_MAX_PER_TOPIC", 2)
    max_per_subject = getattr(settings, "DIGEST_MAX_PER_SUBJECT", 1)
```

Then replace the whole diversification block. Find:

```python
    # Apply topic diversification limit on clusters
    topic_counts: Counter = Counter()
    selected: list[tuple[Article, Analysis, float, list[Article]]] = []

    for art, analysis, score, secondary_arts in clustered_candidates:
        topic = analysis.topic
        if topic_counts[topic] < max_per_topic:
            selected.append((art, analysis, score, secondary_arts))
            topic_counts[topic] += 1
            if len(selected) >= max_items:
                break

    return selected
```

and replace it with:

```python
    # Diversification, applied to clusters so a merged story is counted once.
    #
    # Two caps, not one. The topic cap keeps a digest from being all of one subject area.
    # The subject cap keeps it from being the same project three times: digest #11 opened
    # with ollama v0.32.10, v0.32.9 and v0.32.8, three genuinely different releases that
    # clustering correctly refused to merge and that the topic cap correctly allowed.
    #
    # The topic stays in the subject key. anthropic.com produced two items that day, one
    # frontier_models and one safety_security, and both were worth publishing.
    topic_counts: Counter = Counter()
    subject_counts: Counter = Counter()
    selected: list[tuple[Article, Analysis, float, list[Article]]] = []

    for art, analysis, score, secondary_arts in clustered_candidates:
        topic = analysis.topic
        subject = (subject_key(art.canonical_url), topic)
        if topic_counts[topic] >= max_per_topic:
            continue
        if subject_counts[subject] >= max_per_subject:
            continue

        selected.append((art, analysis, score, secondary_arts))
        topic_counts[topic] += 1
        subject_counts[subject] += 1
        if len(selected) >= max_items:
            break

    return selected
```

Note the shape change: the original nested the body inside `if topic_counts[topic] < max_per_topic:`,
and this uses `continue` guards instead so two conditions read as two lines rather than one
compound test. Skipping with `continue` rather than `break` is what makes backfill work, and it
was already the behaviour of the original.

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
uv run pytest tests/test_ranking.py -q -k "repetitive or same_subject or backfill_keeps or silent"
```

Expected: PASS, 4 passed

- [ ] **Step 5: Run the whole suite**

```bash
uv run pytest -q
```

Expected: PASS, 132 passed — 117 before this plan, plus 11 parametrised cases from Task 1 and
4 selection tests from Task 2. The existing `classified_articles` fixture uses four
`https://example.com/artN` URLs, which share a network location, but this does not collide:
`art3` and `art4` are excluded earlier by `EXCLUDED_MATURITIES`, and the two survivors carry
different topics (`frontier_models`, `ai_agents`), so their keys differ. If any pre-existing
test fails here, stop and report it rather than adjusting the assertion.

- [ ] **Step 6: Run ruff**

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add apps/digest/ranking.py tests/test_ranking.py
git commit -m "Cap how many digest items may share a subject"
```

---

## Verification against the real digest

After both tasks, confirm the rule reproduces the measurement the spec is built on. This is a
one-off check, not a test — it reads the production database.

- [ ] **Step 1: Replay digest #11**

```bash
uv run python -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup()
from apps.digest.models import Digest, Analysis
from apps.digest.ranking import subject_key
seen, dropped = {}, []
for it in Digest.objects.get(pk=11).items.order_by('position'):
    cls = Analysis.objects.filter(article=it.article, stage='classification').first()
    key = (subject_key(it.article.canonical_url), (cls.payload or {}).get('primary_topic'))
    if key in seen: dropped.append(it.position)
    else: seen[key] = it.position
print('would drop positions:', dropped)
print('would keep:', sorted(seen.values()))
"
```

Expected output:

```
would drop positions: [2, 3, 5]
would keep: [1, 4, 6, 7, 8, 9, 10, 11, 12]
```

If the dropped list differs, the key derivation does not match the spec's measurement — stop
and report before changing anything.

---

## Self-review

**Spec coverage**

| Spec section | Task |
|---|---|
| §3.1 placement after clustering | Task 2 Step 3 — the block replaced is the one following `cluster_candidates` |
| §3.2 `subject_key`, network location, code-host owner segment, exact equality | Task 1 Steps 2 and 4 |
| §3.3 topic stays in the key | Task 2 Step 3, pinned by `test_same_subject_different_topic_both_survive` |
| §3.4 backfill needs no code | Task 2 Step 3 note, pinned by `test_backfill_keeps_the_digest_at_its_cap` |
| §4 configuration and the knob rationale | Task 1 Step 1 |
| §5 `hn` is not suppressed | no code; the fixture's `api-docs.deepseek.com` entries are HN-sourced in production and carry the target host, which `test_subject_key` pins |
| §5 dropped articles stay eligible | no code by design; nothing to implement |
| §5 topic cap remains the outer bound | Task 2 Step 3 keeps `max_per_topic` unchanged |
| §6 tests 1-4 | Task 2 Step 1, plus the digest #11 replay |
| §7, §8 out of scope and rejected alternatives | nothing to implement |

No spec requirement is without a task.

**Placeholder scan:** no TBDs, no "add error handling", no "similar to Task N". Every code step
carries the code.

**Type consistency:** `subject_key(url: str) -> str` is defined in Task 1 and called in Task 2
with `art.canonical_url`, a `str`. `DIGEST_MAX_PER_SUBJECT` and `SUBJECT_CODE_HOSTS` are spelled
identically in `config/settings.py`, in `subject_key`, and in the loop. `select_digest_candidates`
keeps its signature, so `compose_digest` is unaffected.
