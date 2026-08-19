# Plan: cluster-backed benchmark corroboration

Status: **implemented, default-off; owner corpus acceptance remains**
Date: 2026-08-19

## Goal

Upgrade an editorial payload from `vendor_claim_only` to `multiple_evidence` only when a
secondary article in the same exact-Jaccard cluster comes from a different canonical outlet
and repeats the same metric-bearing benchmark number.

This records cross-outlet corroboration. It does not claim independent reproduction, external
truth verification, or benchmark validity.

## Existing contracts

- Exact character 5-gram Jaccard at `0.80` is the complete clustering architecture.
- `DigestItem.secondary_articles` contains stored members of that cluster.
- Canonical outlet identity comes from `story_identity.subject_key()`, not `Source.id`.
- `translation_gates.extract_numbers()` is the shared numeric normalizer.
- `EditorialEn.evidence_level` remains the frozen
  `vendor_claim_only | multiple_evidence` enum.
- `BENCHMARK_VERIFICATION_ENABLED` defaults to `False`.

## Implemented design

1. `shared_benchmark_numbers()` accepts only locally metric-bearing values and rejects
   calendar years, versions, and ordinary-prose numbers.
2. `cluster_has_independent_benchmark()` requires different canonical outlets and performs
   no network or LLM call.
3. `apply_cluster_evidence()` upgrades only the latest English editorial row, is idempotent,
   never downgrades an existing value, and logs identifiers rather than article bodies.
4. `compose_and_publish()` invokes the verifier after digest composition and before publishing
   only when the feature flag is enabled.
5. The appendix renders restrained Uzbek labels and never says that a result was reproduced.
6. Ranking, digest membership, and ordering are unchanged.

## Proof

- `tests/test_verification.py`
- pipeline flag/order tests in `tests/test_tasks.py`
- appendix/data tests in `tests/test_ranking.py`
- `docs/spike/BENCHMARK_EVIDENCE_MEASUREMENT.md`

The current stored exact-Jaccard pair is from one canonical outlet, so the real-corpus
measurement found zero eligible cross-outlet corroborations. Zero is a valid safe result and
the feature remains disabled.

## Remaining owner gate

Before activation:

1. Replay every stored primary/secondary pair.
2. Inspect canonical outlet keys, accepted metric tokens, and local contexts.
3. Reject activation on any same-outlet, date, version, or wrong-metric promotion.
4. Run the full test, lint, and migration checks.
5. Enable the flag only in the reviewed environment.

No external benchmark scraper, model judge, new clustering mechanism, ranking feedback loop,
or schema migration belongs in this plan.