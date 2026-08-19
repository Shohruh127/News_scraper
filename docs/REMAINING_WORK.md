# Project map

Version: 2.0
Updated: 2026-08-18
Replaces: v1.0, which described T1.14–T1.20 as pending. Those are done. See §2.

This is the living map. When you want to know where the project stands, read this file, not
the git log. When something here turns out to be false, fix this file in the same commit that
makes it false.

---

## 0. How we work

| Role | Who | Does |
|---|---|---|
| Architect | Claude (this assistant) | writes specs and implementation plans, reviews delivered work, accepts or rejects it |
| Executor | the project owner's other AI agents | implements one plan at a time and reports back |
| Owner | the project owner | decides scope, priority and anything editorial |

An executor implements a plan; it does not design. If a plan is wrong or a step cannot be
followed, **stop and report** — do not improvise a substitute. A deviation nobody knows about
is a defect.

The architect does not accept a claim without evidence. "Tests pass" is accepted only with the
command and its verbatim output; a measurement is accepted only with the numbers it produced.
On 2026-08-18 a delivered task passed all its tests and still left a documented constraint
unenforced — a mutation test found it, reading the code did not. Expect that level of check.

### Rules for an executor

1. One task at a time, in the plan's order. Its acceptance check must pass before the next starts.
2. Every acceptance check is a command. Run it, paste the real output.
3. Where a plan says STOP, stop and ask.
4. Comments carrying a measurement or a rationale are part of the deliverable, not decoration.
   This project's constants are trusted because the number that produced them sits beside them.
5. One Django app, functions over classes, no abstraction before the second case.

### Do not

| Do not | Why |
|---|---|
| Touch `D:\IMV_IB_Support`, `D:\diarization`, `D:\Doni_project`, `D:\chatbot` | Unrelated production systems |
| Commit `.env`, a token, or the Ollama LAN address | Secrets stay out of git |
| Relax `CLUSTER_JACCARD_THRESHOLD` | Measured over 17,020 pairs with a 0.79 separation gap. Not a tuning knob |
| Remove the source-based maturity ceiling | The only thing making the anti-vapourware filter work. Without it seven arXiv papers reach the digest |
| Reintroduce title-based similarity | Scored 0.000 on both decisive cases. `tests/test_clustering.py` pins this |
| Send translation to `gemma4:31b` | Measured worse: it is the model that garbled Uzbek |
| Route triage or classification to MiMo | Only the editorial English stage is routed. `gemma4` measured precision 0.83 and costs nothing |
| Widen `subject_key` to a suffix match | `mygithub.com` is not GitHub. Pinned by `tests/test_ranking.py::test_subject_key` |
| Add a required glossary term that has an ordinary English sense | Measured three times: the gate then rejects correct Uzbek, and its retry makes the text worse |
| Raise `DIGEST_MAX_ITEMS` above 15 without asking | It is a post count, not a list length |

---

## 1. Verified state, 2026-08-18

**134 tests, ruff clean, `master` at `f01a9be`, working tree clean, 13 commits not yet pushed.**

23 sources · 506 articles · 1 digest published.

### GATE 1

| Claim | State | Evidence |
|---|---|---|
| Beat tasks consumed by a worker | ✅ | `09:40:00,013 Sending due task fetch-evening` → `09:40:00,940 Task succeeded`; `PeriodicTask.last_run_at` persisted |
| Non-empty `summary_uz` per item | ✅ | digest #11, 12/12 |
| One story from several sources = one item | ✅ | verified on the live pool |
| No article in two digests | ✅ | enforced by the `digestitem__isnull=True` filter |
| Kill switch leaves digest `composed` | ✅ | `[KILL SWITCH ACTIVE] ... status='composed', channel_message_id=None, items_count=12` |
| Edit and delete on a real post | ✅ | 16 posts deleted; one channel post edited and reverted with entities intact |
| 7 consecutive automatic days | ⏳ | not started, by decision — see §3 |

### Operating notes

**Applying a config change.** Edit `.env`, then `docker compose up -d <service>`.
`docker compose restart` does **not** pick the change up — it restarts the existing
container with the environment it was created with. Measured 2026-08-18: after adding a
value to `.env`, `restart` reported the old one and `up -d` the new one. This matters most
for `PUBLISHING_ENABLED`: flipping the kill switch with `restart` silently does nothing.

**Applying a code change.** Editing Python or a template needs `docker compose build` before
`docker compose up -d`. `up -d` alone recreates the container from the **existing image**, and
the source lives in that image because the Dockerfile ends with `COPY . .`.

Measured 2026-08-18, after the archetype work was committed and the containers recreated
without a rebuild: `subject_key` was present but `ARCHETYPES` was not, and
`/app/config/settings.py` still read `default=False` for `TELEGRAM_LINK_PREVIEW` while the repo
read `default=True`. The next scheduled run would have published the old format.

So there are two separate rules, and confusing them is silent:

| what changed | what to run |
|---|---|
| a value in `.env` | `docker compose up -d <service>` |
| Python, a template, a dependency | `docker compose build` then `docker compose up -d` |

`docker compose restart` applies neither.

**`data/` is not in the image.** `.dockerignore` excludes it, so `eval_classifier` cannot
find `data/gold_set.jsonl` inside a container. Run it from the host, or pass `--gold-set`
with a path that exists in the container. Nothing in the scheduled pipeline reads `data/`.

### Measured facts an executor will need

| Fact | Value |
|---|---|
| Editorial providers | `EDITORIAL_EN_PROVIDER=mimo`, `TRANSLATION_PROVIDER=ollama` (`gemma4:latest`) |
| Translation `num_predict` | **2500**. At 1200 the JSON truncated and 5 of 7 translations were lost |
| MiMo structured output | `response_format` must be `json_schema` with `strict: true`. `json_object` conformed 2/7 |
| Clustering | char 5-gram Jaccard on text, 0.80, source ignored |
| Subject diversity | `(subject_key, topic)`, cap 1. Network location, plus owner segment for code hosts |
| Maturity ceiling | URL-keyed. arXiv/HF-papers → `paper_only`. A HuggingFace *model card* is not a paper |
| Paper prefilter | 216 of 411 articles were paper domains, consumed 169 LLM calls, produced 0 digest items |
| Article status machine | `fetched → triaged → classified \| skipped`. Four states. Reading it as three made `source_yield` report a queue as a rejection |
| Ollama concurrency | 2. Eight parallel requests are slower than serial |
| Telegram limits | message 4096 chars, photo caption **1024** chars — both measured against the live API |
| Pipeline timings | fetch 97s · compose + editorial 329s · publish 12 posts + 12 appendices 82.6s |
| Database growth | 14 MB at 506 articles; ~9.2 KB per article and its analyses; ~2 MB/day; ~800 MB/year |
| Appendix delivery | The publisher reads the auto-forward ID from Redis, and only the `bot` service writes it. With `bot` down every appendix is missed — the posts still land, and the digest stays `published` with an admin alert |
| Publish idempotency | `publish_digest` skips items that already carry a `channel_message_id`. Measured 2026-08-19 before the guard: 61 of 82 live channel messages had no database record |
| Artifact verification | `repo_is_real` returns `None` when GitHub did not answer, and `None` is never stored. The unauthenticated API allows 60 requests/hour/IP, so a rate limit must not become a permanent "no artifact" |

---

## 2. Done

- **T1.1–T1.13** — Django + Celery, six models, five connectors, extraction, triage,
  classification, ranking, publishing, Celery Beat, Docker with per-queue workers.
- **T1.14** one post per news item · **T1.15** three carried defects · **T1.17** prompt fixes.
- **T1.16 translation gates**, then recalibrated on 2026-08-18. A live run rejected 7 of 15
  translations, over the STOP threshold the task itself set. Three false-positive classes were
  traced and removed: an acronym carrying an Uzbek suffix (`CVEni`), and two terms with ordinary
  English senses (`context`, `framework`). The number gate misfired zero times.
- **T1.18 live publishing** — all four sub-items. Digest #11: 12 items, 12 distinct
  `channel_message_id`, 12 distinct `group_message_id`, appendix pairing proven by reply probe.
- **T1.19 post format** — `expandable_blockquote`, ~1200 → 347 visible characters.
- **Subject diversity** — `docs/superpowers/specs/2026-08-18-digest-subject-diversity-design.md`.
  Digest #11 opened with three Ollama releases and two DeepSeek posts while every component
  behaved correctly; the rule drops exactly those three.
- **Paper prefilter** — papers are excluded from every digest by construction, so triaging one
  spends the model for nothing. Removed 140 of 241 pending triage calls.
- **Per-stage `model_digest`** — was read from the global `LLM_PROVIDER`, blanking provenance on
  Ollama-produced translation rows.

---

## 3. Decisions in force

**MiMo stays, monitored rather than replaced.** The Token Plan forbids automated backend use.
The owner has weighed this and chosen to keep `EDITORIAL_EN_PROVIDER=mimo`, watching the Django
admin and switching if the account is blocked or revoked. This is a pull strategy, and it is
sound: a MiMo outage sets `Digest.status = failed` and writes the reason to the worker log, both
of which survive without any notification being delivered. Only the push alert is currently
broken. Do not change the provider without the owner's decision.

**No freeze before deploy.** The owner's sequence is: finish the requested work in phases, reach
a deployable state, deploy, observe for one week, then iterate on what the week reveals. The
seven-day GATE 1 claim is therefore satisfied after deploy, not before it.

**Deployment target.** Local Docker Compose for the first week, then the same server that hosts
Ollama at the address kept in `.env`. All six services already carry `restart: unless-stopped`.

---

## 4. Phases

### Phase 0 — Debt

Not improvements. Each one blocks something else.

| Item | Owner | State |
|---|---|---|
| Merge the branch into `master` | architect | ✅ done |
| Rewrite this map | architect | ✅ this file |
| Rotate the bot token | **project owner** | ⏳ BotFather `/revoke`; the current token was exposed in a session transcript |
| Repair the admin alert path | **project owner** | ⏳ `sendMessage` to `TELEGRAM_ADMIN_CHAT_ID` returns `Forbidden: bot can't initiate conversation with a user` — that account has never pressed `/start` |

### Phase 1 — Serve the Django admin

Moved ahead of the format work because the MiMo decision in §3 depends on it. The models are
already registered (`Source`, `Article`, `Analysis`, `Digest`, `Feedback`), a superuser exists,
and `django_celery_results` and `django_celery_beat` bring their own admin. What is missing is a
process that serves HTTP: `docker-compose.yml` has six services and none of them listens.

Scope: a `web` service, static files for the admin CSS, and list columns that make each stage
legible — `Analysis` already stores `latency_ms`, `model_tag` and the full `payload`.

### Phase 2 — Post templates and images (P2)

Three template shapes measured against the publishable corpus: release/product 69%,
policy/incident 18%, method/research 11%. Template choice is made before rendering, from
`primary_topic` and `maturity`, with no extra LLM call.

Images: 11 of 12 articles in digest #11 carried an `og:image`, but three Ollama posts shared one
GitHub card style and two DeepSeek posts shared an identical URL — variety is weakest exactly
where repetition is worst. `sendPhoto` caps the caption at 1024 characters against a current post
of 1360, so attaching an image costs a quarter of the text; `LinkPreviewOptions` shows the same
image and keeps the 4096 limit. This is an editorial choice for the owner.

**Rich Messages are rejected.** Bot API 10.1 and 10.2 added `sendRichMessage` with headings,
tables, lists, dividers and collapsible details, and the vocabulary was verified against the live
API. Telegram Web renders none of it — subscribers see an unsupported-message placeholder. You
control the bot; you do not control the reader's client.

### Phase 3 — Translation policy (P3)

Which English words survive into Uzbek: established technical terms, product names such as
Ollama, and proper nouns stay; everything else is translated. This runs entirely on
`TRANSLATION_PROVIDER=ollama` and is independent of the MiMo decision.

The work is the structure of the term list, not the gate. The gate's mechanism is sound and its
three false positives are documented in `apps/digest/translation_gates.py`.

### Phase 4 — Content depth (P4)

What the editorial stage writes in "Nima uchun muhim", "Boshqaruv uchun" and "O'zbekistonda".
This is the one phase that runs on MiMo, so it is tuned against whatever provider §3 leaves in
force.

### Phase 5 — Source expansion (T1.20)

12 candidates verified live on 2026-08-18. Six of them publish less often than the seven-day
window, so they contribute nothing on most days without being broken. Measure all twelve in
shadow mode for a week, keep the six to eight that earn their place on usable items, extraction
failures, and share of items reaching a digest.

`technical_talks` stays empty until a transcript connector exists; the RSS connector fetches the
page, and a video page has no article body.

### Phase 6 — Deploy, then one week of observation

Then iterate on what the week reveals.

---

## 5. Not now

| Task | Why it waits |
|---|---|
| Artifact verification | Would let genuine open-source papers back in by checking whether a promised repo resolves. Until then `SKIP_PAPER_DOMAINS=True` keeps them out of triage entirely |
| Feedback bot (aiogram) + buttons + learning | Each post now has its own thread, so this is unblocked but not scheduled |
| Independent benchmark verification | `evidence_level` is always `vendor_claim_only` today |
| Merging dropped items as secondary links | The owner chose to drop repetitive items rather than merge them |

---

## 6. Reporting

Per task: what changed, the acceptance command, its verbatim output, any deviation and why.
Nothing else.