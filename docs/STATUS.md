# Project Status Index

**Date:** 2026-08-19  
**Clustering architecture:** exact character 5-gram Jaccard at threshold `0.80` — complete.

## Feature status & proof artifacts

| Feature | Code Status | Proof Artifact | Activation / Owner Gate |
|---|---|---|---|
| **A: Artifact verification** | IMPLEMENTED | `tests/test_verification.py` (all passing), `manage.py recheck_artifacts --dry-run` | Safe default: dry-run only. Live non-dry-run is an **owner gate**. |
| **B: Clustering (Exact Jaccard)** | COMPLETE | `apps/digest/clustering.py`, `tests/test_clustering.py` | Operative at measured threshold `0.80`; no additional clustering system is planned. |
| **C: Benchmark verification** | IMPLEMENTED | `docs/spike/BENCHMARK_EVIDENCE_MEASUREMENT.md` | Default-off (`BENCHMARK_VERIFICATION_ENABLED=false`). Corpus acceptance is an **owner gate**. |
| **D: Feedback bot** | IMPLEMENTED | `apps/digest/bot.py`, `apps/digest/telegram_updates.py`, `tests/test_bot.py`, `tests/test_telegram_updates.py` | Local simulation verified. Live channel polling/smoke is an **owner gate**. |
| **E: Alert delivery** | IMPLEMENTED | `apps/digest/publish.py` (`send_admin_alert`), `tests/test_tasks.py`, `tests/test_publish.py` | Unit tests verified. Live admin-chat message send is an **owner gate**. |
| **Post format v2** | IMPLEMENTED (safe-off) | `apps/digest/post_format.py`, `apps/digest/media.py`, `tests/test_post_format.py`, `tests/test_media.py` | Default-off (`POST_FORMAT_V2_ENABLED=false`). Target chars <600 & measurement probe required before cutover. |

## Clustering architecture decision

Exact character 5-gram Jaccard at `0.80` is the complete clustering architecture for this
project's measured volume. It is dependency-free, measured against the live corpus, and no
additional clustering system is planned.

## Active plans

| Plan | Status |
|---|---|
| `2026-08-19-benchmark-verification.md` | IMPLEMENTED, flag off, corpus review pending |
| `2026-08-19-feedback-bot.md` | IMPLEMENTED, live smoke pending |
| `2026-08-19-post-format-redesign.md` | IMPLEMENTED, not accepted |
| `2026-08-19-alert-delivery.md` | IMPLEMENTED, live smoke pending |
| `2026-08-19-windows-always-on-readiness.md` | COMPLETE (Tasks 0–13 complete, 319 tests passing) |