# spikes/ — throwaway measurement code

**This directory is deleted at the start of M1.**

Rules:

1. Nothing in `src/` may import from here.
2. No tests, no type hints, no abstractions are required here.
3. Code quality does not matter. Answers do.
4. Everything written to `spikes/out/` is gitignored measurement data.

Purpose: answer the three M0 questions from `docs/IMPLEMENTATION_PLAN.md` before any
production code is written.

| Script | Answers |
|---|---|
| `probe_ollama.py` | Which models exist, how fast, how schema-reliable, what concurrency |
| `probe_language.py` | Which Uzbek generation strategy produces publishable text |
| `probe_volume.py` | How many items per day survive a strict filter |

Reports land in `docs/spike/`. Those are kept; this directory is not.
