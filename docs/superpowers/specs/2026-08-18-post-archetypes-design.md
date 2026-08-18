# Post archetypes: what a post says and how it looks

Version: 1.0
Date: 2026-08-18
Status: approved, not yet implemented
Merges: P2 (post templates and images) and P4 (content depth), which were separate phases in
`docs/REMAINING_WORK.md` v2.0 until measurement showed they cannot be built in that order.
Relates to: `docs/CONTENT_SCHEMA.md` (the frozen contract), ADR-005 (two-stage editorial)

---

## 1. Why these two phases merged

The plan was six post templates first, richer content second. That order is impossible, and
the measurement says so plainly. Across 37 stored `editorial_en` rows:

| field | populated |
|---|---|
| `what_was_built` | 100% |
| `evidence_level` | 100% |
| `limitations` | 78% |
| `local_deployable` (true) | 43% |
| `architecture` | 40% |
| `benchmarks` | 40% |
| `hardware` | 13% |
| `repo_url` | 10% |
| `install` | 10% |
| `api_url` | 5% |
| `license` | **0%** |

Six templates can only differ if they show different information. The fields that would
differentiate them are the empty ones. A template keyed on `repo_url` renders an empty section
nine times out of ten; one keyed on `license` renders an empty section always.

A schema says what is possible. Data says what is there. Designing from the schema designs the
empty space.

## 2. Decisions taken, and the evidence for each

### 2.1 Images arrive through `LinkPreviewOptions`, not `sendPhoto`

Telegram caps a photo caption at **1024 characters** and a message at **4096** — both measured
against the live API on 2026-08-18. The current post is 1360 characters, so `sendPhoto` would
force a quarter of the text out.

The deeper cost is structural. 11 of 12 articles in digest #11 carried an `og:image`, so roughly
one in ten does not. Choosing `sendPhoto` therefore keeps `sendMessage` as well, and the post
exists in two shapes with two length limits and two edit paths — `editMessageText` for one and
`editMessageCaption` for the other. `LinkPreviewOptions` never creates that split: the post is
always one `sendMessage` and whether an image appears is Telegram's problem.

Settings: `TELEGRAM_LINK_PREVIEW=true`, `prefer_small_media: true`, `show_above_text: false`.
Small rather than large media because twelve large cards make the channel very tall, which is
the complaint that started this work. It is a one-line change either way.

### 2.2 The archetype is chosen by the model, with boundary definitions

Measured on the first six items of digest #11, using MiMo with `strict: true` and a schema
carrying six optional detail blocks:

| prompt | archetype correct | irrelevant blocks filled |
|---|---|---|
| minimal, no definitions | 0/6 | 6 |
| with boundary definitions | 5/6 | **0** |

Without definitions the model labelled three Ollama release notes `agent_protocol`,
`company_product` and `agent_protocol`, assigned `HIGH` severity to a change in a default
sampling parameter, and attached "IMMEDIATE ACTION REQUIRED" to an item with no compliance
dimension.

With definitions, both failures disappeared. This repeats a finding the project already owns:
enum boundary definitions raised topic accuracy from roughly 1/10 to roughly 9.5/10, and they do
the same here.

The 5/6 figure needs one honest note. The raw run scored 4/6 against the labels used, but one of
those two misses was a bad label rather than a bad answer: a DeepSeek pricing update was expected
to be `policy` while the definition handed to the model said "pricing is not policy". The model
followed the definition. The one genuine error was a DeepSeek release read as `agent_protocol`.

The archetype is a field of the existing English editorial response, not a separate call. The
editorial stage already reads the article; the archetype is a result of that reading, not an
independent question. A separate call would add twelve requests a day, a timeout path and a
latency budget for information already in hand.

### 2.3 `security` became `risk_hardening`

The proposed security archetype carried `severity_level: CRITICAL | HIGH | MEDIUM` and an
affected-versions field. Measured across the 11 stored `safety_security` articles:

| signal | present |
|---|---|
| a CVE identifier | 1/11 (9%) |
| a CVSS score | **0/11** |
| any severity word | 2/11 (18%) |
| affected or patched versions | **0/11** |

A required three-value severity enum would be invented in at least nine of eleven cases, and an
enum offers no way to say "not stated". For a security post an invented severity is the most
expensive error available.

The reason is visible in the corpus. Digest #11's security items were *Improving Fable 5
Safeguards*, *Text AI watermarks will always be trivial to remove*, and *We eliminated 1,400 CVEs
in NanoClaw's container images*. None is a vulnerability advisory. They are risk analysis and
hardening reports, and the archetype is now shaped to that: what can go wrong, what was done
about it, what remains.

If an article does state a CVE or a severity, it belongs in the prose of `risk_en`, where it is
quoted rather than classified.

### 2.4 The two-stage editorial split is preserved

The English stage produces `*_en` fields only. The Uzbek translation remains a separate stage on
`TRANSLATION_PROVIDER=ollama`.

This is ADR-005 and it is not negotiable here: the split exists so a bad summary can be traced to
comprehension or to translation rather than to one ambiguous step, and translation runs on
`gemma4:latest` because MiMo turned *2.4 trillion* into *2 trillion* and calqued *open-weight* in
two posts. A schema that emits `headline_uz` directly from MiMo discards both.

### 2.5 `evidence_level` is not redefined

It stays the frozen enum `vendor_claim_only | multiple_evidence`. `CONTENT_SCHEMA.md` states that
M1 is always `vendor_claim_only` and that verification arrives in M2.3. The research archetype
gets its own `evidence_strength_en` as free text instead.

---

## 3. Architecture

No new pipeline stage and no new model field.

```
classification  (gemma4:31b)                    unchanged
    primary_topic, maturity

editorial_en  (MiMo)
    archetype                                   NEW, with boundary definitions
    headline_en, summary_en, why_it_matters_en,
    leadership_en, uzbekistan_application_en    unchanged
    <archetype>_details                         NEW, exactly one block
    technical, evidence_level                   unchanged

editorial_uz  (Ollama gemma4:latest)
    schema derived from the English payload      NEW
    "Keep in English" term list                  UNTOUCHED — see §7

render
    template selected by archetype               NEW
    link preview enabled                         existing setting
```

`archetype` lives in the `editorial_en` payload. `_item_data` reads it and puts it in the
template context. Nothing else in the pipeline knows about archetypes: ranking, clustering,
subject diversity, the maturity ceiling and the paper prefilter are all untouched.

`technical` stays as it is. It feeds the group appendix, which serves technical readers; the
archetype block feeds the channel post, which serves engineering leaders. Different audiences,
different blocks.

The boundary definitions live in `CONTENT_SCHEMA.md` and the prompt reads from there, mirroring
how topic definitions are already handled. In this project the document is the contract.

---

## 4. The six archetypes

**"Required" here means required by the template, never by the JSON schema.** Nothing inside a
detail block is marked required in the schema handed to the model. A strict schema does not make
a model know an answer; it makes it produce one, which is how a change to a default sampling
parameter acquired a `HIGH` severity in §2.2. The renderer enforces these instead: if a required
field is absent the post falls back to the plain template, and no invented value ever reaches the
channel.

The block key is the archetype name plus `_details` — `release_details`, `agent_protocol_details`,
`risk_hardening_details`, `policy_details`, `research_details`, `company_product_details`.

| # | archetype | required by the template | optional | corpus |
|---|---|---|---|---|
| 1 | `release` | `what_changed_en` | `benchmarks_en`, `availability_en` | ~39 |
| 2 | `agent_protocol` | `connects_en` | `deployment_en` | ~13 |
| 3 | `risk_hardening` | `risk_en`, `mitigation_en` | `residual_en` | ~12 |
| 4 | `policy` | `who_issued_en`, `who_must_comply_en` | `deadline_en` | ~1 |
| 5 | `research` | `claim_en` | `evidence_strength_en`, `reproducible_en` | ~4 |
| 6 | `company_product` | `what_they_do_en` | `availability_en` | ~0 |

A field is required only when the article always contains it. For a release the changelog *is*
the article. For policy, a rule whose issuer is unstated is not news. Optional fields are the
ones measurement showed to be sparse — `benchmarks` at 40% means two release posts in three
render without that section, and the template must read correctly that way.

`policy` and `company_product` are nearly empty today. The sources added on 2026-08-18 —
`nextgov`, `fedscoop`, `statescoop`, `ec_digital`, `gds_uk`, `techcrunch_ai`, `crunchbase_news`,
`sifted` — feed exactly those two.

### Boundary definitions

These go into `CONTENT_SCHEMA.md` and are quoted verbatim by the prompt. They are the reason the
measurement moved from 0/6 to 5/6, so they are part of the deliverable, not commentary.

```
release          A named product or model shipped a new version. A changelog, a release note,
                 a version number. This is the default for any version bump.
agent_protocol   A protocol or framework for connecting tools to models, where the news IS the
                 connection mechanism. Not a runtime that happens to run agents.
risk_hardening   A risk, a weakness, or work done to reduce one. There must be something that
                 can go wrong and someone acting on it.
policy           A rule issued by a government or standards body, with someone obliged to
                 comply. Pricing is not policy.
research         A method or a finding with a claim and evidence, not a shipped artifact.
company_product  A company entering a market or making a commercial launch, where the company
                 is the news rather than the version.
```

The prompt also instructs: fill only the block for the chosen archetype, omit any field not
stated in the article, and never infer a severity. All three were verified to hold in the
measured run.

---

## 5. Translation

`TRANSLATION_SCHEMA` becomes derived rather than fixed: its properties are built from the keys
present in the English payload.

This is a small change because the stage is already generic in its other two parts.
`TRANSLATION_PROMPT` takes `{fields}` as a JSON dump and never names a field;
`validate_translation(en_fields, uz_fields)` takes two dicts. Only the schema was hard-coded, and
it was written without knowing the rest of the stage had no such assumption.

Deriving the schema also avoids the failure measured in §2.2: a block absent from the schema
cannot be filled by a model that felt like filling it.

---

## 6. Rendering

### 6.1 Files

```
item_base.html            the invariant frame:
                            {% block lead %}     emoji and tag line
                            headline link, summary
                            <blockquote expandable>
                              {% block detail %} archetype-specific lines
                              why it matters / leadership / Uzbekistan
                              sources

item_release.html         extends the base, fills both blocks
item_agent_protocol.html
item_risk_hardening.html
item_policy.html
item_research.html
item_company_product.html

item_post.html            the existing template, kept as the fallback
```

The frame is shared because the headline link, the tag line and the `<blockquote expandable>`
syntax would otherwise be written six times. That syntax was verified against the live API rather
than assumed; writing it six times is six chances to get it wrong.

### 6.2 The two blocks

`lead` is the visible part, and it is where channel uniformity is actually decided — a reader
scrolling sees only this. Each archetype carries its own emoji and tag:

```
🚀 #release · gh_ollama
🔌 #agent_protocol · hn
🛡 #risk_hardening · anthropic
⚖️ #policy · nextgov
🔬 #research · hf_papers
🏢 #company_product · techcrunch
```

`detail` sits inside the expandable blockquote and holds the archetype's own lines **and their
order**. A risk post opens with the risk; a policy post opens with who must comply. Neither opens
with "why it matters".

### 6.3 Character budget

```
current post              1360 total /  475 visible
plus 2-3 detail lines     ~250-360
new post                 ~1600-1700 total / 475 visible
limit                     4096
```

Every new line sits inside the expandable blockquote, so the visible length does not change and
the "too long" complaint does not return.

---

## 7. Boundary with P3

P3 owns the *term list* in `TRANSLATION_PROMPT` — which English words survive into Uzbek. This
spec owns the *field set* that prompt receives. The two touch the same string and must not be
implemented at the same time.

Order does not matter, but overlap does. Whichever lands second rebases on the first.

---

## 8. Failure handling

| Condition | Behaviour |
|---|---|
| `archetype` missing or not in the enum | fall back to `item_post.html`, `log.info` |
| a required detail field is empty | fall back to `item_post.html`, `log.warning` |
| an optional detail field is empty | that section is omitted, the post is normal |
| `summary_uz` missing | the post is not published — existing behaviour, unchanged |

The governing rule: **the archetype block is an enhancement. Its absence simplifies the layout and
never loses the post.** A missing `summary_uz` is different because there is then nothing to read.

---

## 9. Testing

Two tests per archetype: one with every optional field present, one with none of them.

The second matters more. `benchmarks` is populated 40% of the time, so two release posts in three
take the "empty" path — the path that is usually left untested because the full case is the one
easy to imagine. Tests should be planned by how often a path is walked, and measurement answers
that where intuition does not.

Plus:

- an unknown archetype value falls back without raising
- a missing required field falls back and logs
- the visible portion of every archetype stays under 600 characters

All of these run against fixed payloads, never a live model. The 5/6 archetype accuracy is a
**measurement to re-run by hand** when the definitions change, not an automated test: asserting a
live model's output would spend MiMo quota on every suite run and go red for reasons unrelated to
the code. The existing suite is offline for the same reason.

Record the re-measurement in `docs/spike/` when it is run, following the practice already used
for clustering and language quality.

---

## 10. Out of scope

**`sendPhoto` and image selection.** Rejected in §2.1 for the dual send path, not deferred.

**Rich Messages.** Bot API 10.1 and 10.2 added `sendRichMessage` with headings, tables, lists and
collapsible details, and the vocabulary was verified against the live API on 2026-08-18. Telegram
Web renders none of it — subscribers see an unsupported-message placeholder. The bot is ours; the
reader's client is not.

**A seventh archetype.** Six covers the corpus. A new one is a template plus a definition plus two
tests, and nothing in this design prevents adding one.

**Changing `technical` or the appendix.** The appendix currently leaks English into Uzbek text
(`"Nima qurildi: Ollama, an open-source tool for..."`). That is real and belongs to P3.

---

## 11. Rejected alternatives

**A fixed translation schema carrying all six blocks.** Rejected on the measurement in §2.2: with
six blocks visible and no definitions, the model filled six irrelevant ones. Deriving the schema
removes the opportunity rather than instructing against it.

**Leaving archetype blocks untranslated.** Simplest, and rejected because it extends a defect
rather than containing it — the appendix already shows English inside Uzbek text and reads badly.

**A deterministic archetype from `primary_topic` and `maturity`.** No model variance, pinned by
tests. Rejected because topic names the field and archetype names the shape, and the two were
measured to diverge: `ai_agents` contains both releases and protocol news. With definitions the
model reaches 5/6 on shape, which a topic map cannot.

**Six templates differing only in surface.** Deliverable immediately from the five reliable
fields, and rejected by the project owner: six arrangements of identical information is variety a
reader notices is fake after a few days.
