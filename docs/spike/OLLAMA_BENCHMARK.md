# M0.1 — Ollama Capability Benchmark

Date: 2026-08-14
Server: remote Ollama on the org LAN (address in `.env`, not recorded here)
Method: `spikes/probe_ollama.py`, `spikes/probe_prompt.py`
Sample: 10 real articles — 3 OpenAI news posts, 4 Hugging Face papers, 3 Ollama GitHub releases

**Status: T0.2 complete.**

---

## 1. Models available

Only two models are installed. `gemma4:12b` and `gemma4:26b` are **not** on this server,
so the three-way fast-tier comparison in the plan could not be run.

| Tag | parameter_size | Quantization | Size | Digest |
|---|---|---|---|---|
| `gemma4:latest` | **8.0B** | Q4_K_M | 9.61 GB | `c6eb396dbd5992bb` |
| `gemma4:31b` | 31.3B | Q4_K_M | 19.87 GB | `6316f0629137b426` |

`gemma4:latest` is the 8B model. Ollama reports `parameter_size: "8.0B"` directly.
Both are 4-bit quantized (Q4_K_M).

**Tag pinning note.** On this server the 8B model has no tag other than `latest`, so the
"never pin `:latest`" rule cannot be satisfied by tag name. Instead, `Analysis.model_digest`
records the Ollama digest on every call. If the tag is ever repointed, the digest changes
and we see it immediately.

---

## 2. Latency

Classification requests, `stream:false`, `format` = JSON Schema, `temperature: 0`,
article text capped at 8000 characters.

| Model | n | p50 | p95 | min | max | mean |
|---|---|---|---|---|---|---|
| `gemma4:latest` | 20 | **5.59s** | 6.20s | 4.38s | 18.65s | 6.02s |
| `gemma4:31b` | 10 | **11.93s** | 27.52s | 9.18s | 45.05s | 17.06s |

`max` in both rows is the first request — cold start, model loading into VRAM.
Warm requests are the p50 figures.

### Correction to prior evidence

`ENVIRONMENT_INVENTORY.md` recorded, from other projects, that `gemma4:31b` takes
roughly 98 seconds and has exceeded a 120-second timeout. **That does not reproduce at
this prompt size.** Measured p50 is 11.93s — roughly 8x faster.

The difference is prompt length. Those projects fed long meeting transcripts; this
project feeds 8000-character articles. The plan's assumption that 31B is too slow for
routine use was wrong at our input size.

### Model swap cost

A request issued right after the other model had been in use took 16.77s versus a warm
5.59s — roughly 11 seconds of model loading.

**Design consequence:** batch by model. Run all 8B triage, then all 31B classification.
Never alternate per article, or every article pays the swap.

---

## 3. Schema compliance

Ollama constrains decoding with XGrammar, so schema *shape* is enforced.

| Model | Prompt | Parseable | Schema-valid |
|---|---|---|---|
| `gemma4:latest` | v1 | 20/20 | **20/20** |
| `gemma4:latest` | v2 | 10/10 | **10/10** |
| `gemma4:31b` | v1 | 9/10 | **9/10** |
| `gemma4:31b` | v2 | 10/10 | **10/10** |

The single failure in the whole run was 31B on the v1 prompt. The v2 prompt fixed it.
Better definitions improve structural reliability too, not just accuracy.

**Determinism:** at `temperature: 0`, identical input produced byte-identical output
across repeated runs. Confirmed by cycling the same 10 articles twice within one 20-request
benchmark — rows 1–10 and 11–20 matched exactly.

---

## 4. The decisive finding — enum definitions beat model size

The v1 prompt defined the three numeric dimensions but gave **no definition** for any of
the 12 topics or 6 maturity levels — only the enum lists. The v2 prompt added one line
per category plus an explicit instruction that non-technical content must be `irrelevant`.

### Topic accuracy, judged by hand against the 10 articles

| Model | v1 (no definitions) | v2 (definitions) |
|---|---|---|
| `gemma4:latest` | ~4/10 | ~7/10 |
| `gemma4:31b` | **~1/10** | **~9.5/10** |

Under v1, 31B collapsed 9 of 10 articles into `startups` — **worse than the 8B**. A larger
model given an underspecified task does not do better; it picks a different arbitrary
attractor and sticks to it.

Under v2 the collapse vanished entirely and every topic changed, almost all toward correct.

### Examples

| Article | 31B v1 | 31B v2 |
|---|---|---|
| GPT-5.6 Sol preview | `startups` | `frontier_models` |
| "OpenAI appoints Chief Revenue Officer" | `startups` | `irrelevant` |
| Enterprise AI adoption report | `ai_agents` | `irrelevant` |
| PixSDS (text-to-3D method) | `startups` | `new_approaches` |
| Ollama v0.32.10 changelog | `startups` | `production_engineering` |

### Prompt-length cost

| Model | v1 mean | v2 mean | Delta |
|---|---|---|---|
| `gemma4:latest` | 6.40s | 6.03s | **-6%** |
| `gemma4:31b` | 14.68s | 20.17s | **+37%** |

On the 8B the longer prompt was free — slightly faster, because the model wrote shorter
`reason` text. On 31B prefill scales with parameter count, so definitions cost real time.
Worth paying: 20s for 9.5/10 beats 15s for 1/10.

---

## 5. Known remaining defect

Both models regress on **maturity** under v2: two arXiv papers that merely mention
releasing code were labelled `reproducible_open_source` instead of `paper_only`.

Cause is in the v2 wording: *"reproducible_open_source: code or weights published and
installable"*. A paper promising code matches that phrasing.

Fix for `CONTENT_SCHEMA.md`: definitions must state the **boundary against the neighbouring
category**, not describe themselves. Specifically — a paper is `paper_only` even if it
promises code; `reproducible_open_source` requires a working link that resolves today.

Also unresolved: the `relevant` boolean does little work on the 8B (8/10 true, including
articles the model itself described as non-technical). The numeric scores are well
calibrated and should drive filtering instead.

---

## 6. Concurrency

`gemma4:latest`, identical classification requests issued in parallel.

| Parallel | Wall time | Mean per request | vs 5.59s solo |
|---|---|---|---|
| 1 | 16.77s | 16.77s | cold start / model swap |
| 2 | 6.46s | 5.66s | unchanged — **real parallelism** |
| 4 | 14.65s | 12.44s | 2.2x slower each |
| 8 | 47.56s | 43.37s | **7.8x slower each** |

Reading the wall times against a serial baseline of 5.59s per request:

| Parallel | Serial would be | Actual | Speedup |
|---|---|---|---|
| 2 | 11.2s | 6.46s | **1.73x** |
| 4 | 22.4s | 14.65s | 1.53x |
| 8 | 44.8s | 47.56s | **0.94x — worse than serial** |

The server parallelises two requests cleanly, gains little at four, and at eight is
slower than doing them one at a time. No errors at any level — it degrades, it does not fail.

**Decision: `llm` Celery queue runs at concurrency 2.** Higher values add latency without
throughput.

---

## 7. Model routing decided by these numbers

```
~200 articles
    ↓  8B triage       5.6s each   → reject obvious junk
~50 survivors
    ↓  31B classify   20.2s each   → the real decision
top 3–5
    ↓  31B deep analysis
```

Budget at concurrency 2:

| Stage | Items | Per item | Wall (÷1.73) |
|---|---|---|---|
| 8B triage | 200 | 5.6s | ~11 min |
| 31B classify | 50 | 20.2s | ~10 min |
| 31B deep | 5 | ~30s | ~2 min |
| | | | **~23 min** |

Comfortably inside the 60-minute budget. Sending all 200 articles to 31B would take
~39 minutes at concurrency 2 — feasible but leaves no headroom, and triage is nearly free.

---

## 8. Answers to the M0 A1 question

| Question | Answer |
|---|---|
| Which models exist | `gemma4:latest` (8.0B) and `gemma4:31b` only |
| Fast tier | `gemma4:latest`, p50 5.59s |
| Deep tier | `gemma4:31b`, p50 11.93s |
| Schema reliability | 100% with the v2 prompt on both |
| Concurrency ceiling | **2** |
| Blocking problem | none |

**GATE 0 item A1: passed.** Remaining for GATE 0: T0.3 Uzbek quality (needs human
scoring) and T0.4 content volume plus gold set (needs human labelling).

## 9. Open question for the human

`gemma4:12b` (7.6 GB, dense, 256K context) is **smaller on disk** than the installed
`gemma4:latest` (9.61 GB) and would likely sit between the two on quality and speed.
Pulling it would allow a real three-way comparison.

This requires server disk space and admin access, so it is not the agent's call. The
project can proceed without it — the two installed models already cover triage and
classification adequately.
