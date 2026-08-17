# First End-to-End Pipeline Run

Date: 2026-08-17
Command: `manage.py run_pipeline --date today --skip-fetch`
Input: the 185 articles already stored
Models: `gemma4:latest` (triage), `gemma4:31b` (classification, editorial)

The first time the pipeline ran on real data. It completed and produced a digest.

## Counts

| Stage | Result |
|---|---|
| triage analyses | 184 |
| classification analyses | 198 |
| editorial analyses | 8 |
| articles skipped | 121 |
| articles classified (candidates) | 64 |
| digest | 1, **status `failed`**, 7 items |

## What worked

1. **The pipeline runs end to end.** Every stage executed, including the editorial stage
   that ADR-003 added.
2. **Uzbek text is produced.** All 7 items carry a non-empty `summary_uz`, 265–456
   characters. This was the criterion nothing in M1 could satisfy before ADR-003.
3. **The T1.9 failure-detection fix works.** The discussion-group appendix could not be
   posted, so the digest was marked `failed` rather than reported as published. Before
   T1.9 this path logged a warning and marked it `published`.
4. **Ranking selected and ordered 7 items**, scores 0.925–1.025.

## Three defects

### 1. Channel and group were not linked — configuration

```
getChat CHANNEL 'AI Frontier Dayjest'       linked_chat_id = None
getChat GROUP   'AI Frontier Dayjest Chat'  linked_chat_id = None
```

Without the link, a channel post is never auto-forwarded to the group, so no message with
`is_automatic_forward` ever appears and the appendix has nothing to reply to. Every item
shows `group_message_id = None`.

This is the STOP condition documented in `IMPLEMENTATION_PLAN.md` T1.7. Resolved by the
project owner linking the group as the channel's discussion group.

### 2. A duplicate story reached the digest

Items #1 and #3 were both `Qwen3.8-2.4T` — the FP8 and base variants of one model,
near-identical HuggingFace model cards, both arriving through `hn`. One of seven slots
wasted, and obvious to any reader.

Measured and solved in `DEDUP_MEASUREMENT.md`: char 5-gram Jaccard scores this pair at
0.900 while keeping consecutive Ollama releases at 0.110.

### 3. Uzbek quality is unstable on `gemma4:31b`

Some items read well, others are corrupted:

| # | Output | Verdict |
|---|---|---|
| 1 | "modelining **an'nagora an'nanash ownan** open-weight model ekanligini" | broken |
| 3 | "modelining **owni** ochilgan weights-lari bilan tanishtirildi" | not a word |
| 2 | "MCP-Memory bu AI agentlar uchun uzoq muddatli xotira imkoniyatlarini taqdim etuvchi MCP serveridir" | acceptable |
| 6 | "Ollama v0.32.7 versiyasida Muse Glimmer modeli qo'shildi" | acceptable |

Strategy C was chosen in `CONTENT_SCHEMA.md` §7 on the basis of T0.3 samples. This is its
first test at digest scale and the result is mixed. The strategy is not the problem — the
model is. See the MiMo comparison in ADR-004.

## Note on the run itself

The pipeline was started by a watcher script that was then stopped with `TaskStop`. On
Windows that killed the shell but not the `python manage.py run_pipeline` child, which kept
running orphaned and holding the Redis evening lock. A second invocation correctly detected
the held lock and returned `{"status": "skipped"}` — and then crashed, because
`run_pipeline` reads `llm_result["triage_survivors"]` unconditionally.

Two consequences worth keeping:

- **Killing a parent process is not stopping the work.** The lock has `ex=3600`, so it
  self-heals within an hour, but a container restart or `SIGKILL` in production produces
  the same orphan.
- **`run_pipeline` must handle the `skipped` return shape.** A working safety mechanism
  currently turns into a `KeyError`.
