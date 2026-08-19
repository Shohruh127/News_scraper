# Embeddings Tier B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Catch one story written independently by two outlets, which Tier A cannot see because the words differ and subject diversity cannot see because the domains differ.

**Architecture:** A small embedding model on the existing Ollama server turns each candidate into a vector. After Tier A's Jaccard pass, cluster representatives are compared by cosine and merged, with the second article becoming a secondary source on the same digest item — the same shape Tier A already produces. The threshold is measured before it is written, never guessed.

**Tech Stack:** Ollama `/api/embed`, an embedding model the owner is downloading, Django, pytest.

**Spec:** ADR-004 §3 named Tier B as the open gap. This plan measures it into existence.

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- Record the suite count before you start and keep it green. It was **174 passed** on 2026-08-19; if the alert-delivery plan landed first it is 175
- **Do not write a cosine threshold this plan has not measured.** ADR-004 says "~0.85" and that is a guess. Tier A's 0.80 came from 17,020 pairs with a 0.79 separation gap, and that is the standard here
- Tier A is untouched. Its threshold, its shingle size and its behaviour stay exactly as they are
- Tier B merges; it never drops. The second article becomes a secondary source, which is what Tier A already does and what the owner chose
- Tests run offline. Never call the live Ollama server from the suite
- One Django app, functions over classes, no abstraction before the second case

---

## Why this change exists

Three mechanisms now reduce repetition, and each misses what the others catch:

| Mechanism | Catches | Misses |
|---|---|---|
| Tier A, char 5-gram Jaccard ≥ 0.80 | the same text twice | the same story in different words |
| Subject diversity, `(subject_key, topic)` | one site repeating itself | two sites covering one event |
| **Tier B, embedding cosine** | one story on two sites | — |

The gap became real on 2026-08-18 when aggregator and news sources were added. `techcrunch_ai`,
`crunchbase_news`, `sifted`, `nextgov`, `fedscoop` and `statescoop` all cover the same events as
each other and as `hn`. Tier A scores those pairs near zero because the wording is independent.

The Ollama server was checked on 2026-08-19: both `/api/embed` and `/api/embeddings` are routed,
and `gemma4` answers `this model does not support embeddings` on each. The endpoint exists; only
the model is missing, and the owner is downloading one.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/digest/embeddings.py` | talk to Ollama, return vectors, compare them | create |
| `spikes/probe_embedding_threshold.py` | measure the separation gap on the real corpus | create |
| `docs/spike/EMBEDDING_MEASUREMENT.md` | the record the threshold comes from | create |
| `apps/digest/clustering.py` | the Tier B pass after Tier A | modify |
| `config/settings.py` | model name, threshold, on/off | modify |
| `tests/test_clustering.py` | Tier B behaviour, offline | modify |

### Context an engineer new to this repo needs

`cluster_candidates(candidates, threshold=None)` takes `[(article, analysis, score), ...]` and
returns `[(primary, analysis, score, [secondary, ...]), ...]`. The highest-scoring member of a
cluster becomes the primary and the rest become evidence links on one `DigestItem`, so one story
consumes one slot. Tier B must produce exactly that same shape.

`spikes/` holds throwaway measurement scripts and is excluded from ruff. `docs/spike/` holds the
write-ups they produce — `DEDUP_MEASUREMENT.md` is the one this plan imitates.

Candidate counts are small: a run clusters roughly 15–40 articles, not the whole corpus. That is
why Tier A can afford O(n²) and why Tier B needs no vector store — embed per run, compare in
memory, keep nothing.

---

## Task 1: Measure the threshold before writing it

**Files:**
- Create: `apps/digest/embeddings.py`
- Create: `spikes/probe_embedding_threshold.py`
- Create: `docs/spike/EMBEDDING_MEASUREMENT.md`
- Modify: `config/settings.py`
- Test: `tests/test_clustering.py`

**Interfaces:**
- Consumes: nothing from other tasks
- Produces: `embed(texts: list[str], model: str | None = None, client=None) -> list[list[float]]`
  and `cosine(a: list[float], b: list[float]) -> float` in `apps.digest.embeddings`.
  Task 2 imports both

- [ ] **Step 1: Add the settings**

Append to `config/settings.py`:

```python

# --- Embeddings, clustering Tier B -------------------------------------------
# Tier A catches the same text twice; subject diversity catches one site repeating itself.
# Neither sees one story written independently by two outlets, which is what arrived on
# 2026-08-18 with the aggregator sources.
#
# CLUSTER_COSINE_THRESHOLD has no default on purpose. It is written by
# docs/spike/EMBEDDING_MEASUREMENT.md, the way Tier A's 0.80 was written by
# DEDUP_MEASUREMENT.md. Until that measurement exists, EMBEDDING_ENABLED stays False and
# Tier B does not run.
EMBEDDING_ENABLED = env.bool("EMBEDDING_ENABLED", default=False)
EMBEDDING_MODEL = env("EMBEDDING_MODEL", default="qwen3-embedding:0.6b")
EMBEDDING_TIMEOUT = env.int("EMBEDDING_TIMEOUT", default=120)
#: Set by Task 2, from the measurement Task 1 produces. Do not guess it.
CLUSTER_COSINE_THRESHOLD = env.float("CLUSTER_COSINE_THRESHOLD", default=1.01)
```

A default above 1.0 is deliberate: cosine never exceeds 1.0, so an unmeasured threshold merges
nothing rather than merging everything.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_clustering.py`:

```python
def test_cosine_of_identical_vectors_is_one():
    from apps.digest.embeddings import cosine

    assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero():
    from apps.digest.embeddings import cosine

    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_handles_an_empty_or_mismatched_vector():
    """A model that returns nothing must not raise inside clustering."""
    from apps.digest.embeddings import cosine

    assert cosine([], [1.0, 2.0]) == 0.0
    assert cosine([1.0, 2.0], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 2.0]) == 0.0
```

No import changes: `tests/test_clustering.py` already imports `pytest`, `clustering`, and
`Analysis`, `Article`, `Source`.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/test_clustering.py -q -k cosine
```

Expected: FAIL, `ModuleNotFoundError: No module named 'apps.digest.embeddings'`

- [ ] **Step 4: Write the embedding client**

Create `apps/digest/embeddings.py`:

```python
"""Vectors from the Ollama server, and the one comparison clustering needs.

No vector store. A run clusters roughly 15-40 candidates, so embedding them each time costs
one request and keeping them would cost a schema, a migration and a staleness question.

`/api/embed` is the current endpoint and takes a list; `/api/embeddings` is its predecessor
and takes one string. Both are routed on this server, verified 2026-08-19. This uses the
former.
"""

import logging
import math

import httpx
from django.conf import settings

log = logging.getLogger(__name__)


def embed(
    texts: list[str],
    model: str | None = None,
    client: httpx.Client | None = None,
) -> list[list[float]]:
    """Embed a batch of texts. Returns [] for the whole batch if the server refuses.

    Failing soft is deliberate: Tier B is an improvement on top of Tier A, and an embedding
    outage must degrade clustering rather than stop a digest.
    """
    if not texts:
        return []

    model = model or settings.EMBEDDING_MODEL
    base = getattr(settings, "OLLAMA_BASE_URL", "").rstrip("/")
    if not base:
        log.warning("OLLAMA_BASE_URL is not set; skipping embeddings")
        return []

    close_client = False
    if client is None:
        client = httpx.Client(timeout=getattr(settings, "EMBEDDING_TIMEOUT", 120))
        close_client = True

    try:
        r = client.post(f"{base}/api/embed", json={"model": model, "input": texts})
        if r.status_code != 200:
            log.warning("Embedding request failed: %s %s", r.status_code, r.text[:200])
            return []
        vectors = r.json().get("embeddings") or []
        if len(vectors) != len(texts):
            log.warning("Embedding count mismatch: asked %s, got %s", len(texts), len(vectors))
            return []
        return vectors
    except Exception as exc:
        log.warning("Embedding request raised: %s", exc)
        return []
    finally:
        if close_client:
            client.close()


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, 0.0 whenever it cannot be computed.

    Returning 0.0 rather than raising keeps a malformed vector from stopping a digest: an
    unmeasurable pair simply does not merge.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_clustering.py -q -k cosine
```

Expected: PASS, 3 passed

- [ ] **Step 6: Write the measurement probe**

Create `spikes/probe_embedding_threshold.py`:

```python
"""Measure the Tier B cosine threshold on the real corpus.

Two questions, both settled by numbers rather than by preference:

  1. Embed the title, or the body? Tier A found titles useless -- they scored 0.000 on both
     decisive cases -- but that was lexical overlap. Embeddings compare meaning, so titles may
     carry the signal after all. Measure both.
  2. Where is the separation gap? Tier A's 0.80 sits in a 0.79-wide empty band, which is why
     it needs no tuning. Tier B deserves the same treatment or it becomes a knob nobody can
     justify.

Run from the repo root:  uv run python spikes/probe_embedding_threshold.py
"""

import os
import sys
from itertools import combinations

import django

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.digest.clustering import text_similarity  # noqa: E402
from apps.digest.embeddings import cosine, embed  # noqa: E402
from apps.digest.models import Article  # noqa: E402

SAMPLE = 60


def main() -> None:
    articles = list(
        Article.objects.filter(status=Article.Status.CLASSIFIED)
        .exclude(extracted_text="")
        .select_related("source")[:SAMPLE]
    )
    print(f"articles: {len(articles)}")
    if len(articles) < 2:
        print("not enough classified articles to measure")
        return

    for label, texts in (
        ("title", [a.title for a in articles]),
        ("body", [(a.extracted_text or "")[:2000] for a in articles]),
    ):
        vectors = embed(texts)
        if not vectors:
            print(f"\n{label}: embedding failed — is EMBEDDING_MODEL pulled on the server?")
            continue

        rows = []
        for (i, a), (j, b) in combinations(list(enumerate(articles)), 2):
            rows.append((cosine(vectors[i], vectors[j]), text_similarity(a, b), a, b))
        rows.sort(reverse=True, key=lambda r: r[0])

        print(f"\n=== {label} — {len(rows)} pairs ===")
        print("top 15 by cosine (read these and mark which are genuinely the same story):")
        for cos, jac, a, b in rows[:15]:
            same_site = a.source_id == b.source_id
            print(f"  cos={cos:.3f} jaccard={jac:.3f} same_source={same_site}")
            print(f"      {a.title[:66]}")
            print(f"      {b.title[:66]}")

        buckets = [0.95, 0.90, 0.85, 0.80, 0.75, 0.70]
        print("  cumulative pairs at or above each cosine:")
        for t in buckets:
            print(f"    {t:.2f}: {sum(1 for c, _, _, _ in rows if c >= t)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Pull the model and run the probe**

The model must exist on the Ollama server first. That server is shared, so the owner pulls it —
do not run `ollama pull` yourself. Confirm it is there:

```bash
uv run python -c "
import os, django, json, urllib.request
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from django.conf import settings
d = json.load(urllib.request.urlopen(settings.OLLAMA_BASE_URL.rstrip('/') + '/api/tags', timeout=15))
print([m['name'] for m in d['models']])
"
```

Then run the probe and paste its whole output into your report:

```bash
uv run python spikes/probe_embedding_threshold.py
```

- [ ] **Step 8: STOP and report**

Do not choose a threshold yourself. The probe prints pairs; deciding which of them are genuinely
the same story is an editorial judgement, and it belongs to the architect and the owner.

Report the probe output and stop. Task 2 begins once the threshold is agreed and written into
`docs/spike/EMBEDDING_MEASUREMENT.md`.

---

## Task 2: The Tier B pass — GATED on Task 1's measurement

**Do not begin until `docs/spike/EMBEDDING_MEASUREMENT.md` exists and names a threshold.**

**Files:**
- Modify: `apps/digest/clustering.py`
- Modify: `config/settings.py` — `CLUSTER_COSINE_THRESHOLD`, `EMBEDDING_ENABLED`
- Test: `tests/test_clustering.py`

**Interfaces:**
- Consumes: `embed` and `cosine` from Task 1
- Produces: no new public callable. `cluster_candidates` keeps its signature and its return
  shape, `[(primary, analysis, score, [secondary, ...]), ...]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_clustering.py`. These never call the server — `embed` is monkeypatched, so
the test pins the merging logic rather than the model.

The file already carries what these need: `pytestmark = pytest.mark.django_db` at module level,
so no decorator; fixtures `src_a` and `src_b`; and a helper `make(source, url, title, text, n)`
that creates an article with its classification and returns `(article, analysis)`. Use it — do
not build rows by hand. `n` becomes the content hash, so it must be unique per article.

```python
def test_tier_b_merges_two_sites_covering_one_story(src_a, src_b, monkeypatch, settings):
    """Different words, different sites, one event. Tier A scores this near zero."""
    settings.EMBEDDING_ENABLED = True
    settings.CLUSTER_COSINE_THRESHOLD = 0.85

    a, an_a = make(
        src_a,
        "https://a.example/disclosure",
        "Regulator sets a deadline for model disclosure",
        "The authority published rules requiring disclosure of training data. " * 20,
        901,
    )
    b, an_b = make(
        src_b,
        "https://b.example/disclosure",
        "New disclosure obligation lands on model providers",
        "Providers must now reveal how their systems were trained under the rule. " * 20,
        902,
    )

    assert clustering.text_similarity(a, b) < 0.80, "Tier A must not already catch this"

    monkeypatch.setattr(
        clustering, "embed", lambda texts, **kw: [[1.0, 0.0], [0.99, 0.14]][: len(texts)]
    )

    out = clustering.cluster_candidates([(a, an_a, 0.9), (b, an_b, 0.8)])

    assert len(out) == 1, "one story must consume one slot"
    primary, _, _, secondary = out[0]
    assert primary is a, "the higher-scoring article is the primary"
    assert secondary == [b], "the other becomes a secondary source, not a deletion"


def test_tier_b_leaves_unrelated_stories_apart(src_a, src_b, monkeypatch, settings):
    """The over-merging guard. Two unrelated stories must stay two items."""
    settings.EMBEDDING_ENABLED = True
    settings.CLUSTER_COSINE_THRESHOLD = 0.85

    a, an_a = make(src_a, "https://a.example/storage", "A storage engine rewrite",
                   "Throughput improved after the rewrite landed. " * 20, 903)
    b, an_b = make(src_b, "https://b.example/scheduler", "A scheduler that reorders work",
                   "Jobs are now placed across machines differently. " * 20, 904)

    monkeypatch.setattr(clustering, "embed", lambda texts, **kw: [[1.0, 0.0], [0.0, 1.0]])

    out = clustering.cluster_candidates([(a, an_a, 0.9), (b, an_b, 0.8)])

    assert len(out) == 2


def test_tier_b_is_skipped_when_disabled(src_a, monkeypatch, settings):
    """With the flag off, no embedding request is made at all."""
    settings.EMBEDDING_ENABLED = False
    calls = []
    monkeypatch.setattr(clustering, "embed", lambda texts, **kw: calls.append(texts) or [])

    a, an_a = make(src_a, "https://a.example/off", "Anything", "Body text here. " * 40, 905)
    clustering.cluster_candidates([(a, an_a, 0.9)])

    assert calls == []


def test_tier_b_survives_an_embedding_outage(src_a, src_b, monkeypatch, settings):
    """An empty result from the server must degrade to Tier A, never lose a digest."""
    settings.EMBEDDING_ENABLED = True
    settings.CLUSTER_COSINE_THRESHOLD = 0.85
    monkeypatch.setattr(clustering, "embed", lambda texts, **kw: [])

    a, an_a = make(src_a, "https://a.example/outage", "One", "Alpha content here. " * 40, 906)
    b, an_b = make(src_b, "https://b.example/outage", "Two", "Bravo content here. " * 40, 907)

    out = clustering.cluster_candidates([(a, an_a, 0.9), (b, an_b, 0.8)])

    assert len(out) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_clustering.py -q -k tier_b
```

Expected: FAIL — `cluster_candidates` does not call `embed`, so the two-site pair stays two items
and `test_tier_b_merges_two_sites_covering_one_story` fails on `len(out) == 1`.
`test_tier_b_is_skipped_when_disabled` and `test_tier_b_survives_an_embedding_outage` pass
already; they are guards.

- [ ] **Step 3: Add the Tier B pass**

In `apps/digest/clustering.py`, add the import beside the existing ones:

```python
from .embeddings import cosine, embed
```

Then add this function directly above `cluster_candidates`:

```python
def _merge_by_embedding(clusters: list[list[tuple[Article, Analysis, float]]]) -> None:
    """Tier B, in place. Compare cluster representatives and fold the close ones together.

    Representatives rather than every pair: Tier A has already grouped the verbatim
    duplicates, so the question left is whether two *stories* are the same one, and each
    cluster speaks for one story.

    An empty result from the server leaves the clusters untouched. Tier B improves Tier A and
    must never be able to stop a digest.
    """
    if len(clusters) < 2:
        return

    reps = [c[0][0] for c in clusters]
    vectors = embed([(a.extracted_text or a.title or "")[:2000] for a in reps])
    if len(vectors) != len(reps):
        return

    threshold = settings.CLUSTER_COSINE_THRESHOLD
    merged_into: dict[int, int] = {}
    for i in range(len(clusters)):
        if i in merged_into:
            continue
        for j in range(i + 1, len(clusters)):
            if j in merged_into:
                continue
            score = cosine(vectors[i], vectors[j])
            if score >= threshold:
                log.info(
                    "Tier B merged '%s' with '%s' (cosine %.3f, sources %s/%s)",
                    reps[i].title[:50],
                    reps[j].title[:50],
                    score,
                    reps[i].source_id,
                    reps[j].source_id,
                )
                clusters[i].extend(clusters[j])
                merged_into[j] = i

    for j in sorted(merged_into, reverse=True):
        del clusters[j]
```

Then call it inside `cluster_candidates`, between the Tier A loop and the result assembly. The
function currently reads:

```python
        if not placed:
            clusters.append([cand])

    result = []
```

Change it to:

```python
        if not placed:
            clusters.append([cand])

    if getattr(settings, "EMBEDDING_ENABLED", False):
        _merge_by_embedding(clusters)

    result = []
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_clustering.py -q
```

Expected: PASS

- [ ] **Step 5: Write the measured threshold into settings**

Replace the placeholder default with the number from `docs/spike/EMBEDDING_MEASUREMENT.md`, and
replace the comment above it with the separation gap that measurement found — the same way
`CLUSTER_JACCARD_THRESHOLD` carries its 0.79 gap. Then set `EMBEDDING_ENABLED` to default `True`.

- [ ] **Step 6: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: green, with seven more tests than the count you recorded at the start —
three cosine tests from Task 1 and four Tier B tests here.

- [ ] **Step 7: Replay against the real corpus**

```bash
uv run python -c "
import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.ranking import select_digest_candidates
from django.utils import timezone
sel = select_digest_candidates(timezone.localdate())
print('items:', len(sel))
for art, _, score, sec in sel:
    if sec:
        print(f'  MERGED {art.title[:50]}')
        for s in sec:
            print(f'     + {s.title[:50]}  [{s.source.name}]')
"
```

Paste the output. Read every merge: a wrong one here reaches the channel as a missing story.

- [ ] **Step 8: Commit**

```bash
git add apps/digest/embeddings.py apps/digest/clustering.py config/settings.py \
        spikes/probe_embedding_threshold.py docs/spike/EMBEDDING_MEASUREMENT.md \
        tests/test_clustering.py
git commit -m "Cluster one story across two sites, at a measured cosine threshold"
```

- [ ] **Step 9: Rebuild, because this is code**

```bash
docker compose build
docker compose up -d
```

---

## Not in this plan

**No vector store.** A run embeds 15–40 candidates. Persisting them would add a schema, a
migration and a staleness question to save one request.

**Tier B does not drop.** It merges, and the second article becomes a secondary source. That is
the owner's choice and it matches Tier A.

**Benchmark verification builds on this, separately.** Once a Tier B cluster exists, checking
whether its members state the same figures answers `evidence_level`. That is its own plan.

---

## Self-review

**Coverage**

| Requirement | Step |
|---|---|
| Threshold measured, never guessed | Task 1 Steps 6–8, gate on Task 2 |
| Merge with the second as a secondary source | Task 2 Step 3, pinned by the merge test |
| Tier A untouched | no step edits its threshold, shingle size or loop |
| Fails soft on an embedding outage | Task 1 Step 4 returns `[]`, pinned by the outage test |
| Off by default until measured | Task 1 Step 1, pinned by the disabled test |
| Tests never touch the server | every Tier B test monkeypatches `embed` |

**Placeholder scan:** none. Every code step carries its code; the one number this plan does not
contain is the threshold, and that absence is the point.

**Type consistency:** `embed(texts) -> list[list[float]]` and `cosine(a, b) -> float` are defined
in Task 1 and used in Task 2's `_merge_by_embedding` with exactly those types. `cluster_candidates`
keeps its signature, so `select_digest_candidates` needs no change.

**One note for the reviewer.** Task 2's tests set `CLUSTER_COSINE_THRESHOLD = 0.85` directly.
That is a fixture value chosen to make the vectors in the test unambiguous, not a recommendation
— the real threshold comes from Task 1's measurement, and Step 5 is where it is written.
