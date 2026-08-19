# Subject diversity in digest selection

Version: 1.0
Date: 2026-08-18
Status: approved, not yet implemented
Relates to: `docs/decisions/004-architecture-and-product-corrections.md` (one post per item),
`docs/spike/DEDUP_MEASUREMENT.md` (clustering), `docs/REMAINING_WORK.md` (T1.19)

---

## 1. The problem, as it actually shipped

Digest #11 was published to the channel on 2026-08-18. It opened like this:

```
1. ollama/ollama v0.32.10     default repeat_penalty change, NVFP4 speedups
2. ollama/ollama v0.32.9      adds NVIDIA Nemotron 3.5 Lightning
3. ollama/ollama v0.32.8      adds the Muse Glimmer model
4. DeepSeek V4 Pro 0813 quietly released
5. DeepSeek peak/off-peak pricing update
```

Five of twelve posts are two stories. A reader opening the channel sees three near-identical
version bumps before anything else.

**No component malfunctioned.** That is what makes this worth writing down:

| Component | What it did | Was it right? |
|---|---|---|
| Clustering | scored the three Ollama pairs 0.093-0.149, far below the 0.80 threshold | yes — they are three genuinely different releases |
| Ranking | scored all three at 0.82 | yes — each is a real production-engineering release |
| `DIGEST_MAX_PER_TOPIC` | allowed exactly 3 `production_engineering` items | yes — that is the configured cap |

Every rule was satisfied and the result was still bad. The quality criterion — variety — lived
in no single component, only in their combination. Nothing owned it.

## 2. Why the existing mechanisms cannot catch this

Clustering merges **duplicates**: two articles about one event become one post with the second
as a secondary source. Its threshold was measured over 17,020 pairs with a 0.79 separation gap
and must not be relaxed (`docs/REMAINING_WORK.md` prohibitions table). Lowering it to catch
consecutive Ollama releases would also merge genuinely distinct stories.

These items are not duplicates. They are distinct events that *look* repetitive. That is a
different problem and needs a different rule.

## 3. The rule

Inside the existing diversification loop in `select_digest_candidates`, alongside the per-topic
cap: **at most `DIGEST_MAX_PER_SUBJECT` items may share a `(subject_key, topic)` pair.**

### 3.1 Placement

```
classified articles
  -> score
  -> sort by score, descending
  -> clustering.cluster_candidates      merges true duplicates
  -> diversification loop               <- the rule goes here
       topic cap      (existing, 3)
       subject cap    (new, 1)
  -> max_items (15)
  -> compose_digest
```

The rule runs **after** clustering, deliberately. A merged cluster is one candidate carrying its
secondary sources, so it is counted once. Running the rule first would let a cluster be penalised
for containing the duplicates that clustering exists to absorb.

No new pipeline stage, no new model field. The loop at `apps/digest/ranking.py:143` already
implements diversification by topic; this is a sibling condition.

### 3.2 `subject_key`

A pure function of the article URL:

- the network location, lowercased, without a leading `www.` — `api-docs.deepseek.com`, `anthropic.com`
- when that network location is **exactly equal** to a member of `SUBJECT_CODE_HOSTS`, the first
  path segment is appended — `github.com/ollama`

Equality rather than a suffix test, so `raw.githubusercontent.com` and any other subdomain are
treated as ordinary hosts and keep their own key.

The code-host case exists because one host carries many unrelated projects. Without the org
segment, `github.com/ollama` and `github.com/k2-fsa` collide, and one of two unrelated release
posts is dropped.

**Why the network location rather than the registrable domain.** Both were measured against
digest #11 and produce identical results on all twelve items. They differ on a case the project
already has:

| URL | registrable domain | network location |
|---|---|---|
| `gds.blog.gov.uk/...` | `gov.uk` | `gds.blog.gov.uk` |
| `technology.blog.gov.uk/...` | `gov.uk` | `technology.blog.gov.uk` |

`gds_uk` is an enabled source, and `technology.blog.gov.uk` is a candidate for the next source
round. Under a registrable-domain key these two unrelated government blogs share a subject.
The network location also needs no public-suffix list, so there is nothing to keep updated.

### 3.3 Why the topic stays in the key

`anthropic.com` produced two items in digest #11: *Introducing Claude Opus 5* (`frontier_models`)
and *Improving Fable 5 Safeguards* (`safety_security`). Both are legitimate and unrelated. A key
of `subject_key` alone would have dropped one.

### 3.4 Backfill needs no code

The existing loop does not `break` when a candidate is rejected; it continues to the next one.
Slots freed by the rule are therefore filled from lower-ranked candidates automatically. The
digest does not shrink — it becomes more varied.

Measured on digest #11 with `DIGEST_MAX_PER_SUBJECT = 1`: positions 2, 3 and 5 are dropped and
positions 6 and 9 both survive, leaving nine items with nine distinct `(subject_key, topic)`
pairs. Note that 6 and 9 share a subject and differ only by topic — they are precisely the pair
that a subject-only key would have collapsed.

## 4. Configuration

```python
DIGEST_MAX_PER_SUBJECT = env.int("DIGEST_MAX_PER_SUBJECT", default=1)
SUBJECT_CODE_HOSTS = ("github.com", "gitlab.com", "huggingface.co")
```

`DIGEST_MAX_PER_SUBJECT` is a genuine editorial knob, unlike `CLUSTER_JACCARD_THRESHOLD`. The
clustering threshold is settled by a 0.79 separation gap in the measurement; any value between
0.2 and 0.9 decides both known cases identically, so it is not a knob. The choice between one
and two items per subject is not settled by any measurement — it is a judgement about how the
channel should read. `1` is the default because it produced the correct result on digest #11.

## 5. Edge cases

**`hn` does not become a firehose under this rule.** Articles arriving through the HN connector
carry the target site as `canonical_url`, not the HN discussion URL. Digest #11's HN items
resolved to seven distinct subjects: `deepseek.com`, `antithesis.com`, `earendil.com`,
`seangoedecke.com`, `echo.ai`, `chad.cm`. The rule does not suppress HN content.

**Dropped articles remain eligible on later days.** `select_digest_candidates` filters on
`digestitem__isnull=True`, and an article rejected by this rule never becomes a `DigestItem`, so
it stays a candidate until it ages past `ARTICLE_MAX_AGE_DAYS`.

This is accepted deliberately, not overlooked. The alternative — recording the rejection — costs
new state for little gain: a newer release outranks an older one, so `v0.32.9` loses again the
next day, and on a day when Ollama publishes nothing the skipped release can surface, which is a
gain rather than a loss. The behaviour is documented here so it reads as a decision rather than
a surprise.

**The topic cap remains the outer bound.** `DIGEST_MAX_PER_TOPIC = 3` is unchanged, but now
requires three *different* subjects to be reached. The two rules do not conflict.

## 6. Testing

Real measured data is used as fixtures, following the project's existing practice.

1. **Replay of digest #11.** Exactly positions 2, 3 and 5 are dropped, and **positions 6 and 9
   both survive.** The second half matters more than the first: it pins over-filtering, which is
   the silent failure mode.
2. **`subject_key` units.** `github.com/ollama` differs from `github.com/k2-fsa`;
   `gds.blog.gov.uk` differs from `technology.blog.gov.uk`; a leading `www.` is stripped; both
   DeepSeek articles yield `api-docs.deepseek.com`.
3. **Backfill.** With fifteen slots and enough candidates, the digest still reaches its cap; the
   rule reorders what is selected without shortening the result.
4. **Silence.** Twelve candidates with twelve distinct subjects produce no drops.

## 7. Out of scope

**The same story published on two different sites** — a launch covered by both TechCrunch and
VentureBeat — may have different subject keys. It is merged only when article text reaches the
measured exact-Jaccard threshold. No second clustering mechanism is planned.

**Merging the dropped items into the surviving post.** Considered and rejected: the project owner
chose to drop the extras rather than merge them. Turning `v0.32.8` and `v0.32.9` into secondary
links on the `v0.32.10` post is a plausible later refinement, not part of this change.

## 8. Rejected alternatives

**A diversity penalty on the score** — multiply a candidate's score by some factor for each
earlier item sharing its key. Rejected because the factor has no measurement behind it. Every
constant in this project carries the measurement that produced it: `0.80` came from 17,020 pairs,
`2500` from a 7/7 versus 2/7 comparison. An unmeasured `0.5` would become a permanent argument.
The deterministic rule removes the question instead of parameterising it.

**An LLM-assigned `subject` field in the classification schema** — most accurate, and it would
also catch the cross-site case in §7. Rejected for now because it changes the classification
schema, requires reclassifying the stored corpus, and introduces model variance into a decision
that a URL answers for free and answers identically every time.
