# Benchmark evidence measurement

Date: 2026-08-19  
Script: `apps.digest.verification` read-only database probe  
Data: the currently stored Tier A cluster links

## Question

Can the deterministic metric-token rule find a shared benchmark number in an existing
cluster, while refusing same-outlet corroboration and date/version numbers?

## Result

| Population | Pairs inspected | Pairs with shared metric tokens | Eligible cross-outlet pairs |
|---|---:|---:|---:|
| Stored Tier A secondary links | 1 | 1 | 0 |

The only stored pair is two Hugging Face Qwen model-card URLs. The accepted-token probe
returned `3`, `4.8`, `11.8`, `27.0`, `30.6`, `41.8`, `44.4`, `45.1`, `52.5`, `53.6`,
`53.8`, `55.9`, `87`, `95`. Both URLs normalize to the
same canonical subject key, `huggingface.co/Qwen`, so the pair is correctly rejected as
non-independent. No editorial payload was promoted.

## Manual review

- The Qwen pair is a same-outlet model-card variant, not independent reporting.
- The rule rejects shared years in the 1900–2100 range unless the year itself has a direct
  metric unit.
- The rule rejects model/version tokens such as `v2` and `GPT-4` unless the exact token has
  a direct metric unit.
- The existing corpus contains no eligible cross-outlet Tier A pair to review.

## Gate

This is an offline rule check, not evidence that any benchmark is true. It does not claim
Arena, Artificial Analysis, SWE-bench, Terminal-Bench, code reproduction, or external truth
verification. Exact character 5-gram Jaccard at 0.80 is the sole pairing mechanism.
`BENCHMARK_VERIFICATION_ENABLED` remains `False` by default. The corpus currently provides
zero eligible pairs, so there is no measured basis to enable the verifier in production.
