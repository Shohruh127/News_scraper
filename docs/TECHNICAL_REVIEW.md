# Technical Review — PROJECT_PLAN.md v1

Sana: 2026-08-14
Tekshiruvchi: Claude (reviewer roli)
Tekshirilgan hujjat: `docs/PROJECT_PLAN.md` v1, `docs/ENVIRONMENT_INVENTORY.md`
Usul: har bir qaror rasmiy hujjat yoki jonli endpoint bilan tekshirildi. Xotiraga tayanilmadi.

Umumiy xulosa: **stack to'g'ri, uchta xato tuzatildi.** Bajarishga ruxsat beriladi.

---

## 1. Tasdiqlangan qarorlar

| # | Qaror | Dalil | Holat |
|---|---|---|---|
| V1 | Ollama `/api/chat` + `format` = JSON Schema | Ollama API docs. `format` maydoni `json` yoki JSON Schema object qabul qiladi. Rasmiy misolda model sifatida aynan `gemma4` ishlatilgan | ✅ |
| V2 | Structured output constrained decoding | Ollama ichida **XGrammar** ishlatadi — grammatika darajasida cheklaydi, ya'ni schema'ga mos JSON **kafolatlanadi** | ✅ |
| V3 | `trafilatura` article extraction | v2.2.0, 2026-07-31, faol qo'llab-quvvatlanadi, Python 3.10–3.14 | ✅ |
| V4 | `aiogram` — discussion group izohi | `Message.is_automatic_forward` maydoni orqali. aiogram 3.30.0 | ✅ |
| V5 | SQLAlchemy 2 async + psycopg 3 | URL: `postgresql+psycopg://` — bitta dialect, `create_engine` va `create_async_engine` avtomatik ajratadi | ✅ |
| V6 | Hacker News Algolia API | `https://hn.algolia.com/api/v1/search_by_date?tags=story` — jonli tekshirildi, valid JSON, auth talab qilmaydi | ✅ |
| V7 | Hugging Face daily papers API | `https://huggingface.co/api/daily_papers?date=&page=&limit=` — public, auth yo'q, JSON | ✅ |
| V8 | OpenAI RSS | `https://openai.com/news/rss.xml` — jonli tekshirildi, valid RSS 2.0, ~150 item | ✅ |
| V9 | Telegram = frontend, React yo'q | Talab o'zgarmagan | ✅ |
| V10 | LangChain / Celery / K8s / vector DB rad etilishi | Yuk hajmi va bitta server sharoitida asosli | ✅ |

---

## 2. Tuzatilgan xatolar

### C1 — TUZATILDI: inventarizatsiya to'g'ri edi, tekshiruvchi xato qildi

> **Reviewer erratum, 2026-08-14.** Bu bo'lim dastlab "JIDDIY xato — `gemma4:latest`
> 8B emas" deb yozilgan edi. **Bu noto'g'ri baho edi.** Loyiha egasi tuzatdi va
> tekshiruv uni tasdiqladi. Asl inventarizatsiya yozuvi to'g'ri bo'lgan.

**Haqiqat:** `gemma4:latest` → `gemma4:e4b`, va **E4B — bu 8B model**.

E4B per-layer embeddings (PLE) arxitekturasidan foydalanadi: **8B total parameters,
4.5B effective**. Ikkala raqam ham bitta modelni tavsiflaydi:

- **total** — modeldagi jami parametrlar soni
- **effective** — PLE lookup jadvallari hisobga olinmagandagi compute/memory izi

Men effective raqamini ko'rib, "8B varianti yo'q" degan xulosaga keldim. Xato shunda edi.

**To'liq teglar jadvali:**

| Tag | Parametrlar | Disk (default quant) | Context |
|---|---|---|---|
| `e2b` | 5B total / 2B effective | 7.2 GB | 128K |
| `e4b` | **8B total / 4.5B effective** — `latest` shu | 9.6 GB | 128K |
| `12b` | 12B dense | 7.6 GB | 256K |
| `26b` | 26B total / 4B active (MoE) | 18 GB | 256K |
| `31b` | 31B dense | 20 GB | 256K |

**Nima o'z kuchida qoladi:**

1. **Fast tier baribir benchmark bilan tanlanadi.** E4B on-device xotira samaradorligi
   uchun loyihalangan — uning 8B'i asosan PLE embedding jadvallari. Matn klassifikatsiyasi
   sifatida 12B dense modeldan ustunligi kafolatlanmagan. Buni parametr sonidan
   chiqarib bo'lmaydi, faqat o'lchash kerak.

2. **Diqqatga sazovor fakt:** `12b` diskda **7.6 GB — `e4b` ning 9.6 GB'idan kichik**,
   ustiga 256K context beradi (e4b'da 128K). Agar serverda `e4b` sig'sa, `12b` ham
   aniq sig'adi.

3. **`latest` kodda hech qachon qadalmaydi.** Bu sabab modelning kichikligida emas —
   `latest` ko'chuvchi ko'rsatkich, upstream uni boshqa tegga yo'naltirishi mumkin.
   Reproducibility uchun aniq teg yoziladi.

**Bonus topilma:** `12b`/`26b`/`31b` context = **256K**. To'liq maqola bemalol sig'adi —
M1'da chunking logikasi **kerak emas**. Reja soddalashadi.

### C2 — Anthropic'da rasmiy RSS yo'q

**Da'vo (eski):** M1.3 manbalar jadvalida "Anthropic news | RSS".

**Haqiqat:** Anthropic `anthropic.com/news` va `anthropic.com/engineering` uchun rasmiy RSS chiqarmaydi. Faqat jamoa tomonidan yaratilgan mirror'lar mavjud (RSSHub, GitHub scraper'lar).

**Ta'sir:** Ikkita variant bor va ikkalasi ham rejani o'zgartiradi:
- (a) Uchinchi tomon mirror'iga tayanish → **rad etiladi**. Bu asosiy manba uchun ishonchsiz: mirror to'xtasa, biz sezmaymiz, va kontent nazorati bizda emas.
- (b) `anthropic.com/news` uchun o'z HTML connector'imizni yozish → **qabul qilinadi**.

**Tuzatish:** HTML connector M2'dan **M1'ga ko'chiriladi**. M1 connector soni 3 → 4.

**Qo'shimcha:** DeepMind RSS manzili ham noto'g'ri edi. To'g'risi `https://deepmind.google/blog/feed/basic/` (`/discover/blog` emas).

### C3 — APScheduler versiyasi qadalmagan

**Da'vo (eski):** "APScheduler" — versiyasiz.

**Haqiqat:** Barqaror liniya **3.11.3** (2026-06-28). **4.0 hali pre-release** va rasmiy hujjatda aniq yozilgan: migration yo'li bo'lmagan holda backwards-incompatible o'zgarishi mumkin, production'da ishlatilmasin. 4.0'da job tushunchasi Task/Schedule/Job ga bo'lingan — API butunlay boshqa.

**Ta'sir:** `uv add apscheduler` bugun 3.x beradi, lekin bajaruvchi agent 4.0 alpha'ni "yangiroq" deb o'rnatib qo'yishi mumkin.

**Tuzatish:** `apscheduler>=3.11,<4` — qattiq qadaladi.

---

## 3. Aniqlashtirilgan nuanslar

**N1 — "Structured output" nimani kafolatlaydi, nimani yo'q.**
XGrammar constrained decoding **strukturani** kafolatlaydi: JSON valid bo'ladi, maydonlar mavjud bo'ladi, enum ichidan tanlanadi. Lekin **mazmun** kafolatlanmaydi — model `novelty: 9` deb yozishi mumkin, aslida yangilik bo'lmasa ham.

Demak Pydantic validation faqat shaklni tutadi. Mazmun sifati uchun **gold set** majburiy. Bu M0.3'dagi qaror to'g'ri ekanini tasdiqlaydi.

**N2 — HN Algolia rate limit hujjatlashtirilmagan.**
Jonli javobda limit ko'rinmadi. Hujjatlashtirilmagan limit — limit yo'q degani emas. Connector baribir konservativ backoff bilan yozilsin.

**N3 — gemma4'da vision bor.**
Kelajakda screenshot/grafik o'qish mumkin. M1'ga kirmaydi, lekin `26b`/`31b` tanlanganda bu imkoniyat saqlanadi.

**N4 — Telegram izoh mexanizmi.**
`getDiscussionMessage` — bu MTProto metodi, Bot API'da yo'q. To'g'ri Bot API yo'li: kanal posti linked group'ga avtomatik forward bo'lganda bot `is_automatic_forward=true` bo'lgan update oladi, va o'sha xabarga `reply_to_message_id` bilan javob yozadi. Bot group'da admin bo'lishi shart.

---

## 4. Tekshiruv xulosasi

| Kategoriya | Soni |
|---|---|
| Tasdiqlangan qarorlar | 10 |
| Rejadagi xato | 2 (C2, C3) |
| Tekshiruvchining xatosi | 1 (C1 — bekor qilindi) |
| Aniqlashtirish | 4 |
| Rad etilgan qaror | 0 |

Rejaning arxitekturasi to'g'ri. Ikkita real xato **fakt darajasida** edi — feed mavjudligi
va kutubxona versiyasi. Ikkalasi ham kod yozilgandan keyin topilsa qimmatga tushardi.

C1 esa tekshiruvchining o'z xatosi bo'lib chiqdi: `8B total / 4.5B effective` bitta
modelning ikki o'lchovi ekanini hisobga olmadim va inventarizatsiyani asossiz ravishda
noto'g'ri deb baholadim. Loyiha egasi tuzatdi.

**Metodik xulosa:** parametr sonlari haqidagi da'volarni model kartasining **to'liq
spetsifikatsiyasi** bilan tekshirish kerak — bitta xulosa jumlasi (`~4.5B effective`)
yetarli emas. PLE, MoE va MatFormer arxitekturalarida bitta model bir nechta
qonuniy "hajm" raqamiga ega bo'ladi.

**Ruxsat:** M0 bajarilishi mumkin.

## 5. Manbalar

- Ollama API — https://docs.ollama.com/api/chat
- Ollama structured outputs — https://docs.ollama.com/capabilities/structured-outputs
- Gemma 4 model library — https://ollama.com/library/gemma4
- APScheduler versions — https://apscheduler.readthedocs.io/en/stable/versionhistory.html
- APScheduler migration — https://apscheduler.readthedocs.io/en/master/migration.html
- aiogram Message type — https://docs.aiogram.dev/en/latest/api/types/message.html
- Telegram Bot API — https://core.telegram.org/bots/api
- SQLAlchemy engines — https://docs.sqlalchemy.org/en/20/core/engines.html
- trafilatura — https://pypi.org/project/trafilatura/
- Anthropic newsroom — https://www.anthropic.com/news
- OpenAI news RSS — https://openai.com/news/rss.xml
