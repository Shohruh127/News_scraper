# Post format: what a reader sees, and what stops a bad post

**Status:** approved in chat 2026-08-19, before implementation planning.

**Supersedes** `docs/superpowers/specs/2026-08-18-post-archetypes-design.md` §2.1 (image
delivery), §4 (six archetype templates), §6.2 (the two blocks) and §6.3 (character budget).
The rest of that spec — the archetype vocabulary, the two-stage editorial split, the frozen
`evidence_level` enum — is unchanged and still binding.

---

## 1. Why this exists

The channel owner's boss approved a post style. The chain that produced it was:

```
t.me/naebnet, t.me/Wylsared    the taste actually came from here
        ↓
an agent read them and named a style "Exploit / Media-Punch"
        ↓
the agent hand-wrote a sample from that description
        ↓
the boss saw the sample and approved it
```

Each step added something Telegram makes easy and the reference channels never use: a bold
headline, an `Asosiy faktlar:` label, bullet points, three hashtags. Reading both reference
channels directly on 2026-08-19 — dozens of posts each — **none** of those four elements
appears in either.

So the approved sample is a copy of a copy. This spec targets the original.

---

## 2. The approved artifact

Sent to the owner's admin chat as message 39 on 2026-08-19 and confirmed: *"eng so'nggi
jo'natganing ma'qul keldi"*. Photo above, text below, sent with `sendPhoto`:

```
[photo: the article's og:image, full width]

EHang kompaniyasi Shenzhen va Hong Kong o'rtasida dunyodagi ilk cross-border,
to'liq avtonom yo'lovchi eVTOL xizmatini ⟨yo'lga qo'ymoqda⟩.

Parvoz 20 daqiqa davom etadi va bir o'rindiq uchun taxminan 800 yuan
(110 dollar)ga tushadi. EH216-S samolyoti ikki kishilik bo'lib, 16 ta
propellerga va taxminan 30 km gacha bo'lgan maksimal masofaga ega.
Xitoyning fuqarolik aviatsiyasi regulyatori samolyot uchun to'liq tip va
uchishga yaroqlilik sertifikatlarini bergan.

Bu hozirda dunyodagi yagona joy bo'lib, bu yerda siz uchuvchisiz, odam
tashuvchi parvoz uchun chipta sotib olishingiz mumkin.

#robototexnika
```

`⟨…⟩` marks the only link in the post. Caption: 604 of 1024 characters.

**The text of this message was written by the pipeline, not by hand** — MiMo for the English,
`gemma4` for the Uzbek. Only the paragraph assembly and the anchor choice were done by the probe
script. That distinction matters: the boss has now approved output the system can actually
produce.

**Two gates in §6 would have changed this message, and that is intended.** Its closing line runs
17 words against a budget of 8, and its three facts arrived as one paragraph rather than one or
two. Tightened, the closing line reads *"Uchuvchisiz parvozga chipta sotiladigan dunyodagi yagona
joy."* — six words, same fact. The approved artifact fixes the **shape**; the gates tighten it
from there.

---

## 3. Decisions taken, and the evidence for each

### 3.1 `sendPhoto` replaces `LinkPreviewOptions`

This reverses §2.1 of the 2026-08-18 spec. That decision rested on two objections, both measured
again on 2026-08-19:

| Objection, 2026-08-18 | Status now |
|---|---|
| "Caption caps at 1024; the post is 1360 characters" | **Gone.** The new post is 422–604 characters. Measured on four real posts. |
| "~1 in 10 articles has no `og:image`, so the post exists in two shapes with two edit paths" | **Still true, and worse: 4 of 27 (15%).** Accepted, and §7 handles it. |

What `sendPhoto` buys, in the owner's priority order:

1. It is what the boss actually approved. The screenshot they signed off carries no link-preview
   card — it is a photo with a caption.
2. It removes the duplicate link. A preview card is itself a link to the article; with the old
   bold-linked headline the same URL appeared twice. A photo is not a link, so exactly one link
   remains.
3. It is the most compact option. A preview card spends three lines on chrome — site name,
   page title, description — all of which restate the post.
4. It closes the Instant View question. IV cannot be created, enabled, or positioned by a bot; it
   exists only where a third party published a template on Telegram's IV platform. With
   `sendPhoto` there is no preview card, so there is no IV button and nothing uncontrollable is
   left in the layout.

Measured cost: 52 KB and 56 KB for the two images fetched. One HTTP request per item, 7–15 a
night.

**One consequence to accept deliberately.** With a link preview, Telegram fetches the image and
shows it inside the publisher's own card. With `sendPhoto` the project downloads that image and
republishes it as its own media, with no card attributing it. This is ordinary practice for news
channels and it is the owner's decision, recorded here so it is not discovered later.

### 3.2 No markup at all

Neither reference channel uses `<b>`, `<i>`, `•`, section labels, or emoji prefixes. The first
sentence carries itself; facts live in prose; numbers sit inside sentences.

Removing bullets does not remove facts. In the approved artifact all three measured facts — 20
minutes, 800 yuan / 110 dollars, EH216-S with 16 propellers — survive as clauses. The post got
**34% shorter** than the approved sample while carrying *more* detail: the sample had three facts,
the pipeline found five.

### 3.3 The headline is removed entirely

There is no headline field any more. `headline_en` and `headline_uz` are deleted, along with the
headline-case translation gate that guarded them.

This is the largest single removal in this spec and it is deliberate: in both reference channels
the opening sentence *is* the headline, by position. A separate bold line duplicates it.

### 3.4 The link is inline, on the sentence's own words

Not an appended `Batafsil`, and not the source name. The owner rejected both: *"nextgov yoki
github yozuvlari kerak emas… gap ichida link ketadi"*.

The model returns `link_anchor_en`, translated to `link_anchor_uz` with the other fields, and the
renderer wraps its first occurrence in `lead_uz`. A compound Uzbek verb — `yo'lga qo'ymoqda`,
`taqdim etadi`, `ishga tushirdi` — is the natural anchor and the model picks it whole.

**The fallback is deterministic and was measured.** Uzbek is SOV, so the verb ends the sentence.
Across all six pipeline leads produced on 2026-08-19, the last word of the first sentence was a
verb, 6 of 6:

```
#2  chiqarishdi   #6  qo'shildi   #7  etadi
#11 qo'ymoqda     #12 tuzatdi     #15 jazoladi
```

Two of those six are fragments of a compound verb — `etadi` from `taqdim etadi`, `qo'ymoqda` from
`yo'lga qo'ymoqda` — which is exactly why the model's choice is preferred and the rule is only a
floor. A post is never published without a link.

### 3.5 Hashtags are derived from a closed table, never written by the model

The owner asked for hashtags for a reason the reference channels do not have: *"hamma o'ziga
keraklilarini ajratib olishi uchun"*. This audience is segmented — some readers follow speech
technology, some follow security — and a hashtag is how Telegram lets them filter.

A filter works only on a byte-identical string. A model asked for tags produces `#speech_voice`,
`#SpeechVoice`, `#nutq`, `#voice_ai` across four nights, and every one of those posts falls out of
the reader's filter. So the tag comes from `Topic`, which is already a closed enum, through a fixed
table:

```
frontier_models        → #modellar          govtech                → #davlat
ai_agents              → #agentlar          production_engineering → #infratuzilma
new_approaches         → #tadqiqot          startups               → #startap
speech_voice           → #nutq              technical_talks        → #suhbat
robotics               → #robototexnika     safety_security        → #xavfsizlik
fintech                → #fintex
```

One tag per post. `irrelevant` never reaches a digest and has no tag. The table lives beside the
`Topic` enum so a new topic without a tag fails a test rather than shipping untagged.

This is the same rule already in force for `subject_key` and `evidence_level`: **a value that will
be compared is derived, never generated.** Free text is for humans to read; keys are for code to
match; the two do not share a field.

### 3.6 The archetype survives, invisibly

Six archetype templates are deleted. The archetype itself stays, and changes job: it no longer
picks a layout, it decides **which facts the body paragraphs must carry**.

```
release          → version, what changed, how to get it
policy           → who, when, who is affected
research         → what was measured, against what, result
risk_hardening   → what is exposed, who is affected, what to do
agent_protocol   → what it connects, what it replaces, who implements it
company_product  → what it does, who it is for, price or availability
```

The reader sees one shape. Its content is correct for the kind of news it is. Before this change
the six archetypes differed only inside a collapsed blockquote — the part almost nobody opens.
This promotes that work to the visible body instead of discarding it.

### 3.7 The closing line is a kicker, not a summary

The reference channels end on a short, wry line that stands alone:

```
"Взрослеть оказалось больно."                     3 words
"Кажется, началось."                              2
"Похоже, экономим!"                               2
"Теперь вместо «чиз» говорим «пика-пика»."         5
"В следующий раз проверяем IQ по винной карте."    8
```

The pipeline's first attempt averaged 15–20 words, because the prompt asked *why it matters* —
which is an instruction to summarise. The lines that landed were the ones carrying a fact the
body had not already stated.

So the rule is: **the kicker carries a fact, in eight words or fewer, or it is empty.** An empty
kicker is a correct outcome; a bad one is not.

The clichés below are banned by name, because a nightly channel dies of them faster than of
anything else. The test is portability: *a closing line that could be pasted onto a different
article is not a closing line.*

```
yangi davr boshlanmoqda · kelajak keldi · hammasi o'zgardi · o'yin qoidalari o'zgardi
bu faqat boshlanishi · vaqt ko'rsatadi · bir narsa aniq · dunyo o'zgarmoqda
```

The approved sample's own closing line — *"Tirbandlikda asabiylashadiganlar uchun yangi davr
boshlanmoqda"* — contains the first entry on that list. This is recorded so the difference is
expected rather than reported later as a regression.

### 3.8 Evidence discipline is unchanged

`evidence_level` keeps its frozen two-value enum. `maturity_ceiling`, `EXCLUDED_MATURITIES` and
the artifact check are untouched.

One rule connects the old discipline to the new voice: **when `evidence_level` is
`vendor_claim_only` and `maturity` is `announcement_only`, no kicker is written.** A press release
does not get a line telling the reader the world changed. The voice gets lighter; the claim
ceiling does not move.

---

## 4. Architecture

### 4.1 Fields

| Field | Today | After |
|---|---|---|
| `headline_en` / `headline_uz` | post, bold, linked | **deleted** |
| `summary_en` / `summary_uz` | post body | → `lead_en` / `lead_uz` |
| — | — | **new** `body_1_*`, `body_2_*` |
| — | — | **new** `kicker_*` |
| — | — | **new** `link_anchor_*` |
| `leadership_en` / `leadership_uz` | post blockquote | **deleted** |
| `why_it_matters_*` | post blockquote | → appendix |
| `uzbekistan_application_*` | post blockquote **and** appendix | appendix only |
| `secondary_sources` | post blockquote | → appendix |
| `archetype` | picks a template | picks required facts |
| `technical{}`, `evidence_level` | appendix | unchanged |

`leadership` is deleted rather than moved: it was written for a manager, and the channel is read
by engineers. If that judgement is wrong the field moves to the appendix; it is a one-line change
either way.

`lead_uz` falls back to `summary_uz` when absent, so digests composed before the cutover still
render in `edit_digest`.

### 4.2 Files

| File | Change |
|---|---|
| `apps/digest/llm.py` | `EditorialEn` schema; `EDITORIAL_EN_PROMPT` rewritten; `ARCHETYPE_DEFINITIONS` become fact requirements; six `*Details` models removed |
| `apps/digest/templates/digest/item_post.html` | rewritten as the single post template |
| `apps/digest/templates/digest/item_base.html` + six archetype templates | **deleted** |
| `apps/digest/templates/digest/item_appendix.html` | gains `why_it_matters_uz` and secondary sources |
| `apps/digest/ranking.py` | `ARCHETYPE_TEMPLATES` and `ARCHETYPE_REQUIRED` removed; `render_item_post` simplified; `_item_data` field map; anchor wrapping |
| `apps/digest/media.py` | **new** — find, fetch and validate `og:image` |
| `apps/digest/publish.py` | `sendPhoto` path with text fallback; single-`<a>` assertion; `edit_message` branches on `sent_as_photo` |
| `apps/digest/models.py` | `TOPIC_TAGS` beside `Topic`; `DigestItem.sent_as_photo` |
| `apps/digest/translation_gates.py` | Uzbek length budget, kicker gate, markup gate |
| `config/settings.py` | `POST_MAX_CHARS`, `POST_TARGET_CHARS`, image settings |

One migration: `DigestItem.sent_as_photo = BooleanField(default=False)`. It records what was
actually sent, so `edit_digest` calls `editMessageCaption` or `editMessageText` from a stored fact
rather than guessing and retrying. The project has been bitten twice by writing a verdict nothing
had established; this avoids a third.

### 4.3 The post contract

```
<lead — 1–2 sentences, exactly one <a> inside it>

<body paragraph 1 — the archetype's required facts, in prose>
[<body paragraph 2> — optional]

<kicker — one line, ≤8 words, or omitted entirely>

#<one tag from TOPIC_TAGS>
```

Sent as `sendPhoto(photo, caption=…, parse_mode="HTML")` when an image was obtained, and as
`sendMessage(text=…, link_preview_options={"is_disabled": True})` when it was not. The preview is
disabled in the text form so the two shapes look the same apart from the missing photo.

The feedback keyboard from the 2026-08-19 bot work attaches to both.

---

## 5. Rendering rules

- No `<b>`, `<i>`, `<code>`, `<blockquote>`, `•`, or emoji anywhere in the caption
- Exactly one `<a>`, inside the lead
- `#` appears only on the final line, exactly once
- Paragraphs separated by one blank line; no trailing whitespace
- The tag line is last, always present

---

## 6. Gates

| Gate | Rule | On violation |
|---|---|---|
| Length | visible caption ≤ **900** characters, target ~450, measured on the **Uzbek** text | drop body sentences from the end, lowest-priority archetype fact first, until it fits |
| Kicker | ≤ 8 words; no banned cliché; must not repeat a number already in the body | the kicker is omitted |
| Kicker suppression | `evidence_level == vendor_claim_only` **and** `maturity == announcement_only` | the kicker is not requested at all |
| Markup | no `<b>`/`<i>`/`•`; exactly one `<a>`; one `#`, on the last line | render fails, item recorded as failed, digest marked FAILED |
| Anchor | `link_anchor_uz` occurs verbatim in `lead_uz` | fall back to the last word of the first sentence |
| Image | `og:image` present, fetched, ≤ 10 MB, image content-type | send as text with the preview disabled |

The length budget is enforced on Uzbek, not English, because **Uzbek runs 42% longer than the
English it is translated from** — measured across six pipeline outputs on 2026-08-19, where four
of six exceeded a 90-character headline budget that the English had satisfied. A budget checked on
the source language is not a budget.

---

## 7. Failure handling

**No image** (4 of 27 measured, 15%). Send as a text message with the preview disabled.
`sent_as_photo` is `False` and `edit_digest` uses `editMessageText`.

**Image fetch fails or is rejected.** Same path as no image. One retry, then text. An image
problem must never cost the post.

**Anchor not found.** Deterministic fallback, §3.4. Logged at INFO with the anchor and the lead so
the rate is measurable; if it exceeds one in five, the anchor field is not earning its place.

**Over budget after trimming.** If dropping both body paragraphs still leaves the caption over
900 characters, the lead alone is too long: render fails and the item is recorded as failed rather
than published truncated mid-sentence.

**Kicker fails its gate.** Omitted silently. This is expected, not exceptional.

---

## 8. Testing

Every gate gets a test that fails when the gate is removed. Specifically:

- a caption containing `<b>`, a bullet, or a second `<a>` must fail to render
- an over-budget Uzbek caption must shed body sentences and come in under 900
- a 9-word kicker, a cliché kicker, and a kicker repeating a body number must each be dropped
- `vendor_claim_only` + `announcement_only` must produce no kicker
- an anchor absent from `lead_uz` must fall back to the last word of the first sentence
- a missing `og:image` must produce a text message with `is_disabled: True`, not a crash
- every `Topic` member must have a `TOPIC_TAGS` entry — the test iterates the enum
- an old payload with `summary_uz` and no `lead_uz` must still render

Mutation testing on the markup gate and the kicker gate specifically: both exist to stop
something, and a gate whose removal leaves the suite green is not a gate.

---

## 9. The measurement gate before cutover

No template or prompt is switched over until this has run and been read:

> Take 8–10 real classified articles across at least four archetypes. Run them through the
> **pipeline** with the new prompt — not by hand. Record for each: visible caption length, kicker
> word count, whether the anchor was found or fell back, whether an image was obtained.

Ship when the median caption is under 600 characters, no kicker exceeds 8 words, and the anchor
fallback fires in fewer than one in five.

This is the same discipline the archetype work used: the first archetype measurement was 0 of 6
without boundary definitions and 5 of 6 with them, and that difference was only visible because it
was measured before being adopted.

---

## 10. Out of scope

**Cross-day subject repetition.** `ollama/ollama` produced six posts in three days, one per patch
release. The per-digest `(subject_key, topic)` cap cannot see across days. Separate spec.

**Instant View.** Cannot be created, enabled, or positioned by a bot. Not addressed because it
cannot be.

**The appendix layout.** It gains three fields and is otherwise untouched.


---

## 11. Rejected alternatives

**Keeping the bold headline.** Offered and declined. Both reference channels place the headline by
position, not by weight, and a bold line above a lead sentence says the same thing twice.

**Keeping the bullet list.** Bullets are easier for a model to produce and easier to verify one
fact at a time. Declined because the reference channels carry the same facts in prose at
two-thirds the length, and the visible cost of bullets is what started this work.

**`Batafsil` as the link anchor.** Deterministic, consistent, and a reader learns to look for it.
Declined: the owner wants the link inside the sentence's own words, which is also what both
reference channels do.

**The source name as the anchor** (`nextgov`, `github`). Declined — it changes every post, so a
reader cannot learn to look for it, and the photo already establishes provenance.

**Model-written hashtags.** Declined for the reason in §3.5: a filter needs a stable string, and
generation cannot promise one.

**Two hashtags, topic plus type.** Offered and declined in favour of one. Can be added later
without changing anything else in this spec.
