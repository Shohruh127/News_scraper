# Master Plan — AI Frontier & Applied Tech Digest

Sana: 2026-08-14
Loyiha: `D:\News_scraper`
Versiya: v1
Holat: tasdiqlangan, bajarishga tayyor

---

## 1. Mahsulot

Har kuni AI dunyosidagi eng muhim yangiliklarni avtomatik yig'ib, tekshirib,
o'zbek tilida Telegram kanaliga chiqaradigan tizim.

Auditoriya ikkita:

| Auditoriya | Nima oladi | Qayerda |
|---|---|---|
| Rahbariyat / qaror qabul qiluvchilar | Nima yaratildi, nega muhim, bizda qayerga tegishli | Kanal posti |
| Texnik mutaxassislar | Repo, API, license, hardware, benchmark, deployment | Discussion group izohi |

### Kontent oqimlari

1. Frontier Models
2. AI Agents
3. New Approaches
4. Speech & Voice AI
5. Robotics & Physical AI
6. FinTech & Public Finance
7. GovTech
8. Production Engineering & Open Source
9. Startups & Deployed Products
10. Technical Talks & Demos
11. Safety & Infrastructure

### Kirmaydi (scope tashqarisi)

- Umumiy IT yangiliklari, gadget, telefon, consumer tech
- Faqat e'lon qilingan, dalilsiz materiallar (🔴 maturity)
- Kripto narx yangiliklari
- Web frontend (MVP'da Telegram = frontend)
- Real-time / daqiqalik yangilanish

---

## 2. MVP muvaffaqiyat mezoni

Tizim tayyor hisoblanadi, agar:

- 7 kun ketma-ket odam aralashuvisiz digest chiqarsa
- Har digestda 2–7 ta material, har biri original manbaga bog'langan
- 🔴 maturity materiallar chiqmasa
- O'zbekcha matn tahrirsiz o'qiladigan bo'lsa
- Bitta yangilik bir necha manbadan kelsa, bitta post bo'lsa
- Kunlik to'liq pipeline 60 daqiqadan kam vaqt olsa
- Xato bo'lsa admin chatiga xabar kelsa

---

## 3. Tuzilma — 3 milestone

Prinsip: **eng qimmat noma'lumni eng arzon kod bilan birinchi tekshir.**

```
M0 — SPIKE            2–3 kun     14–18 Aug     tashlab yuboriladigan kod
   │  GATE 0: uchta savolga o'lchangan javob
   ▼
M1 — THIN PRODUCT     2–3 hafta   18 Aug–7 Sep  kunlik ishlaydigan tizim
   │  GATE 1: 7 kun ketma-ket avtomatik digest
   ▼
M2 — HARDEN & PUBLIC  3–4 hafta   7 Sep–5 Oct   ommaviy kanal + chuqurlik
```

Har milestone oxirida **ishlaydigan narsa** bo'ladi. Gate o'tmasa — keyingisi boshlanmaydi.

---

## 4. M0 — SPIKE (2–3 kun)

**Maqsad:** mahsulotni o'zgartirishi mumkin bo'lgan uchta noma'lumni aniqlash.
**Kod sifati muhim emas** — bu kod M1'da tashlab yuboriladi.

### Javob kerak bo'lgan savollar

| # | Savol | Nega muhim |
|---|---|---|
| A1 | Fast tier qaysi model — `e4b`, `12b` yoki `26b`? | `latest` = `e4b` = 8B total / 4.5B effective. Parametr sonidan sifat chiqmaydi — o'lchash kerak. `31b` ~98s, ba'zan 120s timeout |
| A2 | gemma4 o'zbekchasi chop etsa bo'ladigan darajadami? | Yo'q bo'lsa — model, tarjima qatlami yoki chiqish tili o'zgaradi |
| A3 | Kuniga nechta material filtrdan **o'tadi**? | 1–2 ta bo'lsa — bu kunlik emas, haftalik mahsulot |

### Steplar

**M0.1 — Ollama capability probe**

- `GET /api/tags` → haqiqiy model teglari ro'yxati
- **Fast tier nomzodlari: `gemma4:e4b`, `gemma4:12b`, `gemma4:26b`** — uchalasi ham o'lchanadi,
  hech biri oldindan afzal ko'rilmaydi. `e4b` = 8B total / 4.5B effective (PLE),
  `12b` = dense va diskda kichikroq, `26b` = MoE, ~4B active. Kodda `latest` emas,
  aniq teg yoziladi (`latest` ko'chuvchi ko'rsatkich).
- Har model uchun 10 ta real maqola bilan latency o'lchovi (p50, p95)
- 2, 4, 8 parallel request — concurrency chegarasi
- Strict JSON schema output ishonchliligi: 20 ta urinishdan nechtasi valid
- Context window chegarasi
- Tool: `httpx`, oddiy Python skript, `time.perf_counter`
- Natija: `docs/spike/OLLAMA_BENCHMARK.md`

**M0.2 — O'zbek tili sifati**

- 10 ta har xil maqola (frontier model release, agent repo, paper, PR-fluff, production case)
- M0.1'da tanlangan fast-tier model va `gemma4:31b` — ikkalasi ham o'zbekcha xulosa yozadi
- **Siz o'qib baholaysiz**: 1–5 ball, tahrir kerakmi
- Uchta variant taqqoslanadi: (a) to'g'ridan-to'g'ri o'zbekcha, (b) inglizcha xulosa → o'zbekchaga tarjima, (c) aralash
- Natija: `docs/spike/LANGUAGE_QUALITY.md` + qaror

**M0.3 — Kontent oqimi hajmi**

- 5 ta manba: OpenAI blog RSS, Anthropic news, HF papers, GitHub releases (3 repo), HN Algolia
- 3 kunlik tarixiy ma'lumot yig'iladi
- Qo'lda sanaladi: jami nechta, filtrdan nechtasi o'tadi
- **Bir vaqtning o'zida gold set tuziladi**: 25–30 ta maqolani siz o'zingiz baholaysiz (kirsin / kirmasin + sabab)
- Natija: `docs/spike/CONTENT_VOLUME.md`, `data/gold_set.jsonl`

### Tool'lar

`httpx` · `feedparser` · `trafilatura` · Ollama `/api/chat` · oddiy `.py` skriptlar · Markdown

### GATE 0 — o'tish sharti

- [ ] Ishlatiladigan model teglari **aniq nomi bilan** ma'lum
- [ ] Har model uchun p95 latency o'lchangan
- [ ] O'zbekcha chiqish strategiyasi tanlangan
- [ ] Kunlik kontent hajmi sanalgan
- [ ] Gold set (25–30 ta) tayyor

### Fallback qarorlar

| Agar | Unda |
|---|---|
| `31b` juda sekin (>60s barqaror) | 31b faqat kuniga 3–5 ta top materialga; qolgani 8B |
| O'zbekcha sifat past (<3/5) | Inglizcha xulosa → alohida tarjima chaqiruvi |
| Ikkalasi ham past | Chiqish tili inglizcha + qisqa o'zbekcha sarlavha |
| Filtrdan kuniga <3 ta o'tsa | Digest kunlik emas, 2 kunda bir; yoki filtr yumshatiladi |
| JSON valid emas (<90%) | Schema soddalashtiriladi + retry/repair qatlami |

---

## 5. M1 — THIN PRODUCT (2–3 hafta)

**Maqsad:** ingichka, lekin **to'liq** tizim — har kuni o'zi ishlaydi.
Chuqurlik emas, **butunlik** muhim.

### Steplar

Batafsil bajarish qadamlari — `IMPLEMENTATION_PLAN.md`. Bu yerda faqat umumiy ko'rinish,
takrorlanish bo'lmasligi uchun.

| # | Task | Kun | Asosiy natija |
|---|---|---|---|
| T1.1 | Django + Celery skeleti | 1 | `manage.py check`, `migrate`, admin ochiladi |
| T1.2 | Modellar + admin | 2 | 6 ta model, manba admin orqali qo'shiladi |
| T1.3 | Connector'lar (rss, github, hn, html) | 4 | 8 ta manbadan real yig'ish |
| T1.4 | Extraction + dedup | 2 | Takror yo'q, qisqa qoldiqlar rad etiladi |
| T1.5 | Triage (8B) + klassifikatsiya (31B) | 3 | Gold set'da precision ≥ 0.80 |
| T1.6 | Ranking + digest | 2 | Snapshot test, majburiy to'ldirish yo'q |
| T1.7 | Telegram publishing | 3 | Kanal + izoh, edit/delete, kill-switch |
| T1.8 | Celery Beat jadvali | 1 | Uchidan-uchiga avtomatik ishlaydi |

Arxitektura qarorlari: `decisions/001-django-celery-stack.md`,
`decisions/002-source-failure-policy.md` va **`decisions/003-m1-scope-correction.md`**.

> **ADR-003 (2026-08-14).** Yuqoridagi 8 ta task §2 dagi MVP mezonlarini bajara olmaydi:
> hech biri o'zbekcha matn ishlab chiqarmaydi va clustering M2 ga surilgan edi. Uchta
> qobiliyatning **minimal ishlaydigan versiyasi** M1 ga qaytarildi — editorial bosqich,
> sodda clustering, va taksonomiyaga mos manba qamrovi. Batafsil ro'yxat:
> `IMPLEMENTATION_PLAN.md` T1.9–T1.13.

Uchta soddalashtirish v1 ga nisbatan:

- **`aiogram` M1'da yo'q.** Publishing — oddiy `httpx.post` Bot API'ga. Celery task'lari
  sinxron, `sendMessage` esa bitta POST. `aiogram` M2'da feedback bot bilan keladi,
  chunki long polling faqat o'sha yerda kerak.
- **Feedback tugmalari M2'da.** Handler'siz tugma — foydalanuvchi bosadi va spinner
  aylanadi. Tugma, bot process va feedback learning birga keladi.
- **`job_runs` jadvali yo'q.** `django-celery-results` `TaskResult` modelini admin bilan
  birga beradi.

### Manbalar (M1 uchun 12 ta)

| Manba | Connector | Oqim |
|---|---|---|
| OpenAI blog | RSS | frontier_models |
| Anthropic news | **HTML** (rasmiy RSS yo'q) | frontier_models |
| Google DeepMind blog | RSS `deepmind.google/blog/feed/basic/` | frontier_models |
| Hugging Face papers | HF | new_approaches |
| LangGraph releases | GitHub | ai_agents |
| MCP spec/SDK releases | GitHub | ai_agents |
| Ollama repo releases | GitHub | production_engineering |
| Whisper releases | GitHub | speech_voice |
| Faster-Whisper releases | GitHub | speech_voice |
| NVIDIA developer blog | RSS | robotics |
| arXiv cs.CR | RSS | safety_security |
| Hacker News (AI filter) | HN | discovery / opportunistik |

### GATE 1 — o'tish sharti (ommaviy kanalga chiqishdan oldin)

- [ ] 7 kun ketma-ket avtomatik digest — qo'l bilan tegilmagan
- [ ] Siz o'sha 7 digestni o'qib, sifatni qabul qilasiz
- [ ] Classifier gold set'da precision ≥ 0.8
- [ ] To'liq pipeline < 60 daqiqa
- [ ] Kill-switch va post tahrirlash ishlaydi
- [ ] Testlar yashil

> **Eslatma — launch qarori.** Siz ommaviy kanalga darhol chiqishni tanladingiz.
> Shuning uchun GATE 1 qattiqroq: kanal ulanishidan oldin 7 kunlik avtomatik
> ishlash isbotlangan bo'lishi shart. Qo'lda tasdiqlash yo'q, lekin
> post'ni tahrirlash, o'chirish va kill-switch — majburiy.

---

## 6. M2 — HARDEN & PUBLIC (3–4 hafta)

**Maqsad:** chuqurlik, ishonchlilik va ommaviy ishlash.

**M2.1 — Story clustering**
Bitta yangilik bir necha manbadan kelsa → bitta post, ko'p dalil.
Boshida: canonical URL + fuzzy title + entity matching. Embedding faqat isbotlangan ehtiyoj bo'lsa.

**M2.2 — 31B deep analysis**
Faqat top 3–5 material. Technical appendix: architecture, repo, license, VRAM, install, benchmark, cheklovlar, integration points.

**M2.3 — Verification layer**
Vendor benchmark'i yetarli emas. Arena, Artificial Analysis, SWE-bench, Terminal-Bench bilan tekshiruv.
Evidence darajasi: `vendor claim only` / `multiple evidence`.

**M2.4 — Feedback learning**
👍 → topic/source weight ↑ · 👎 → ↓ · 🛠 → applicability ↑
Exponential moving average. Fine-tuning yo'q.

**M2.5 — Manbalarni kengaytirish**
25–40 ta manba, 6 connector: RSS, GitHub, Hugging Face, HN, HTML, YouTube/transcript.
Manbalar `Source` modelida, admin orqali qo'shiladi — kod o'zgarmaydi.

**M2.6 — Monitoring va operations**
JSON structured logging · `TaskResult` metrikalari · manba baseline'lari · healthcheck ·
kritik xato → admin Telegram chat · PostgreSQL backup/restore skripti

**M2.7 — Deployment**
Dockerfile + Compose (`app`, `scheduler`, `bot`, `postgres`) · healthcheck · restart policy ·
`.env` serverda · deployment preflight check

**M2.8 — Breaking news kanali**
Katta release (yangi frontier model, muhim weights) → kunlik digestni kutmaydi.

### GATE 2

- [ ] 30 kun barqaror ishlash
- [ ] Clustering ishlaydi — takroriy post yo'q
- [ ] Feedback ranking'ga ta'sir qiladi
- [ ] Backup tiklanishi sinalgan
- [ ] Serverda Docker orqali ishlaydi

---

## 7. Texnik stack va sabablari

Barchasi siz IMV loyihasida allaqachon ishlatgan stack (ADR-001).

| Qatlam | Tool | Nega |
|---|---|---|
| Til | Python 3.12 | Data/AI ekotizimi |
| Paket | `uv` | Tez, `uv.lock` bilan reproducible |
| Framework | **Django** | ORM + migration + admin + settings + management commands |
| ORM / Migration | **Django ORM** | Alohida Alembic kerak emas |
| Ops UI | **Django admin** | Manba boshqaruvi, degraded ko'rish — bepul keladi |
| Task queue | **Celery + Redis** | Ikkita navbat = ikkita concurrency byudjeti |
| Scheduler | **Celery Beat** | Alohida process; APScheduler'dan ishonchliroq |
| Job ko'rinishi | `django-celery-results` | `TaskResult` admin bilan; custom jadval kerak emas |
| DB | PostgreSQL | Constraint'lar invariantni majburlaydi |
| Validatsiya | Pydantic | Faqat LLM JSON uchun — ORM'dan mustaqil |
| HTTP | `httpx` | Sinxron; Celery task'lari sinxron |
| RSS | `feedparser` | Standart |
| Extraction | `trafilatura` 2.2.0 | HTML'dan asosiy matn + metadata |
| LLM | Ollama `/api/chat` | `format` = JSON Schema, `temperature: 0` |
| Retry | `tenacity` | Timeout, 5xx, 429 uchun |
| Template | Django templates | Telegram HTML |
| Telegram (M1) | `httpx` → Bot API | `sendMessage` bitta POST |
| Telegram (M2) | `aiogram 3` | Faqat feedback bot uchun — long polling |
| Test | pytest-django + respx | IMV bilan bir xil |
| Sifat | Ruff | Formatter + linter |
| Deploy | Docker Compose | Bitta server |

## 8. Ataylab ishlatilmaydiganlar

| Tool | Nega |
|---|---|
| DRF, view, template (admin'dan tashqari) | Telegram = frontend. Django faqat ORM+admin |
| LangChain | To'g'ridan-to'g'ri Ollama chaqiruvi soddaroq |
| Kubernetes | Bitta server |
| Vector DB (Qdrant/pgvector) | Semantic search hali isbotlangan ehtiyoj emas |
| React / Vue | Web frontend yo'q |
| `asyncio` | Celery sinxron. 40 manba uchun sinxron `httpx` yetarli |
| Fine-tuning | Feedback ma'lumoti hali yo'q |
| Autonomous agent framework | Publishing deterministik bo'lishi kerak |
| Pandas (production'da) | Faqat offline calibration uchun |

Django qabul qilinishi — tipik Django loyihasining qolgan qismini qabul qilish
degani emas.

## 9. Repository tuzilishi

Bitta Django app. Ko'p app'ga bo'linmaydi.

```
News_scraper/
├── manage.py
├── config/
│   ├── settings.py           bitta fayl, bitta muhit
│   ├── celery.py             ikkita navbat: fetch, llm
│   └── urls.py               faqat admin
├── apps/digest/
│   ├── models.py             6 ta model
│   ├── admin.py
│   ├── tasks.py              celery task'lar
│   ├── connectors.py         4 ta fetcher, 4 ta funksiya
│   ├── extract.py            trafilatura, canonical url, hash
│   ├── llm.py                ollama + pydantic + promptlar
│   ├── ranking.py
│   ├── publish.py            telegram sendMessage
│   ├── templates/digest/
│   └── management/commands/
├── docs/
│   ├── ENVIRONMENT_INVENTORY.md
│   ├── PROJECT_PLAN.md               shu fayl
│   ├── IMPLEMENTATION_PLAN.md        batafsil task'lar
│   ├── TECHNICAL_REVIEW.md
│   ├── CONTENT_SCHEMA.md             M0
│   ├── decisions/                    ADR'lar
│   └── spike/                        M0 natijalari
├── data/gold_set.jsonl               M0
├── spikes/                           M1 boshida o'chiriladi
├── tests/
├── docker-compose.yml                postgres + redis
├── pyproject.toml
└── .env.example
```

To'qqizta Python fayli. O'ninchisi kerak bo'lsa — avval sababini so'rang.

## 10. Asosiy xavflar

| Xavf | Ehtimol | Ta'sir | Qarshi chora |
|---|---|---|---|
| O'zbekcha sifat past | O'rta | Yuqori | M0.2'da o'lchanadi, tarjima qatlami fallback |
| 31b juda sekin | Yuqori | O'rta | Faqat top 3–5 material |
| Kontent kam | O'rta | O'rta | Digest chastotasi o'zgaradi |
| Manba HTML o'zgaradi | Yuqori | Past | Connector izolyatsiyasi + monitoring |
| LLM hallucination | O'rta | Yuqori | Har fakt manbaga bog'lanadi, maturity filtri |
| Ommaviy kanalda sifatsiz post | O'rta | Yuqori | GATE 1, post tahrirlash, kill-switch |
| Loyiha yarim yo'lda to'xtaydi | Yuqori | Yuqori | Har milestone oxirida ishlaydigan mahsulot |

## 11. Keyingi qadam

**M0.1 — Ollama capability probe.**
Remote Ollama serverga ulanib, haqiqiy model teglari, latency va JSON ishonchliligini o'lchash.

Hech qanday production kod yozilmaydi — bu tashlab yuboriladigan o'lchov skripti.
