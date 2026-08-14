# Development Environment Inventory

Date: 2026-08-14  
Project: `D:\News_scraper`  
Status: Step 0.1 complete

## Purpose

This document records the development environment and reusable production patterns for the AI news-digest project. It deliberately excludes passwords, tokens, private IP addresses, and live `.env` values.

## Workstation

| Component | Detected |
|---|---|
| OS | Windows 11 Pro (`10.0.26200`) |
| CPU | Intel Core i7-10700, 16 logical processors |
| RAM | 31.9 GB |
| GPU | NVIDIA GeForce GTX 1050 Ti, 4 GB VRAM |
| Git | 2.53.0.windows.2 |
| Python | 3.12.10 |
| `uv` | 0.11.8 |
| Docker | 29.6.2 |
| Docker Compose | 5.3.1 |
| Local Ollama CLI | Not installed |
| Local `psql` CLI | Not installed |

The missing local Ollama and `psql` CLIs are not blockers. Ollama is a remote server service, while PostgreSQL can run and be managed through Docker.

## Existing runtime

The current IMV Docker stack demonstrates that Docker and Compose work on this workstation. The inspected AI worker, PostgreSQL, and Redis containers are running and healthy.

Useful production patterns already present in the related projects:

- container health checks;
- loopback-only bindings for infrastructure that should not be public;
- `.env.example` files without committed secrets;
- external Ollama integration;
- Telegram bot long polling;
- immutable server builds and deployment preflight checks;
- read-only model mounts;
- separate process roles and test/quality gates.

## Relevant reference projects

These projects remain read-only references. Their code will not be copied blindly.

| Project | Relevant knowledge |
|---|---|
| `D:\IMV_IB_Support` | Docker Compose, PostgreSQL, Redis/Celery, FastAPI AI worker, Django, aiogram bot, health checks, tests |
| `D:\diarization` | GPU worker deployment, external Ollama/Kotib services, preflight checks, immutable images, model mounts |
| `D:\Doni_project` | Ollama structured JSON output, multilingual UZ/RU workflows, model-selection notes |
| `D:\chatbot` | Backend/frontend containerization and environment templates |

## Ollama findings

- Ollama runs as an external service on the organization LAN; its private address is intentionally not recorded here.
- The confirmed high-quality model name is `gemma4:31b`.
- **`gemma4:latest` resolves to `gemma4:e4b`, which is an 8B model.** The original "8B option" note in this document was correct. E4B uses per-layer embeddings (PLE): **8B total parameters, 4.5B effective**. Both figures describe the same model — total is the parameter count, effective is the compute/memory footprint after PLE lookup tables are excluded.
- Published tags: `e2b` (5B total / 2B effective), `e4b` (8B total / 4.5B effective), `12b` dense, `26b` (MoE, 26B total / 4B active), `31b` dense, `31b-cloud`, plus quantized and `-mlx` variants.
- Disk sizes at default quantization: `e2b` 7.2 GB, `e4b` 9.6 GB, `12b` 7.6 GB, `26b` 18 GB, `31b` 20 GB. Note that **`12b` is smaller on disk than `e4b`** — E4B's PLE embedding tables are large but cheap to use.
- Consequence for model routing: `e4b` is designed for on-device memory efficiency, so its 8B total overstates its capability relative to a 12B dense model. Whether `e4b`, `12b` or `26b` is the better fast tier is an open question to be **measured**, not argued from parameter counts.
- Never pin `latest` in code. It is a moving pointer that upstream can repoint; runs must be reproducible.
- Context windows: 128K for `e2b`/`e4b`, 256K for `12b`/`26b`/`31b`. At 256K, full articles fit without chunking.
- `gemma4` has native function-calling and vision support. Neither is required for the first version.
- Whatever tags this document lists, the server's real inventory must still be read from Ollama `/api/tags` before any model name is hard-coded.
- Existing projects call Ollama through `/api/chat`, with non-streaming responses and JSON-schema output where structured data is needed.
- Prior project evidence shows `gemma4:31b` can take roughly 98 seconds on a large prompt and has also exceeded a 120-second timeout. It should not process every article by default.

Initial model-routing hypothesis for this project:

1. pick the fast tier by benchmark from `gemma4:e4b`, `gemma4:12b` and `gemma4:26b` — do not default to `latest`;
2. use `gemma4:31b` only for a small number of high-value stories that need deeper analysis;
3. benchmark every candidate on the actual server before fixing timeouts or daily capacity.

This is a working hypothesis, not the final architecture decision.

## Initial scope decisions

For the first usable version, prefer the smallest stack that can reliably create two daily Telegram digests:

- Python 3.12 with `uv` for the application and dependency management;
- PostgreSQL for sources, articles, digest history, publication state, and user feedback;
- a simple scheduler for twice-daily collection and publication;
- `aiogram` for Telegram channel publishing and discussion-group feedback;
- remote Ollama for multilingual AI processing;
- Docker Compose for reproducible local/server execution.

Do not introduce these at the start unless a measured need appears:

- Vue or another custom frontend;
- MinIO and ClamAV;
- Kubernetes;
- a vector database;
- Celery and Redis for a workload that initially runs only twice per day;
- a full Django application.

FastAPI may be added later for health, admin, or monitoring endpoints. It is not required to produce the first digest.

## Still to verify before production

- the remote Ollama server's available model tags, latency, concurrency, and reachability from the future deployment host;
- Telegram bot, channel, and linked discussion-group identifiers and permissions;
- the final production host, deployment path, backup policy, and monitoring destination;
- candidate news sources, their RSS/API/scraping method, terms, reliability, and language coverage;
- digest publication times in the `Asia/Tashkent` timezone.

## Step outcome

The workstation is ready to begin development. The new project can reuse proven infrastructure patterns without inheriting the complexity of the existing production systems. The next step is to write one compact Product Brief and architecture boundary before generating the code skeleton.
