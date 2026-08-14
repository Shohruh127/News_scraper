# Gold Set Review

Date: 2026-08-14
File: `data/gold_set.jsonl` — 26 items, all labelled
Labelled by: **Claude, not a human editor** (`labelled_by: "claude"` on every row)
Applied by: `spikes/label_gold_set.py`

---

## 1. What this set can and cannot measure

The original plan required a human to label this set, because it is the standard the
classifier is measured against. The project owner asked for AI labels instead. That is
their call, and it changes what T1.5's precision number means.

| | Human labels | These labels |
|---|---|---|
| Measures | Does the classifier match editorial judgement? | Does `gemma4` agree with `claude`? |
| Independence | Full | Partial — different model, different training |
| Circularity | None | **Real**: Claude wrote the enum definitions in `CONTENT_SCHEMA.md` and then applied them here |

The circularity is the weak point. A classifier reading Claude's definitions and being
scored against Claude's application of those same definitions can hit high precision by
following the definitions faithfully, without the definitions being editorially right.

**Precision ≥ 0.80 against this set is therefore necessary but not sufficient.** It
proves the prompt communicates the taxonomy. It does not prove the taxonomy is correct.

### Cheapest way to close the gap

Spot-check **10 of the 26** rather than relabelling all. Fifteen minutes. Start with the
six flagged in §2 — those are where a human is most likely to disagree, so they carry
most of the information. If the human agrees on all six, the set is probably sound. If
they disagree on two or more, the taxonomy needs work before T1.5 is worth running.

---

## 2. The six calls a human should check

These were genuinely difficult. Each is recorded in the row's `human_note`.

| # | Item | Call made | Why it is contestable |
|---|---|---|---|
| 1 | Anthropic Economic Index Connector | keep · `ai_agents` | A shipped MCP-style connector, so the topic fits. But it is also a data-PR piece, and a stricter editor would call the whole thing `irrelevant` |
| 7 | Sign language AI (SL2T) | keep · `new_approaches` | It is a translation model, but the input is video, not audio, so `speech_voice` does not apply. Nothing in the taxonomy fits well |
| 9 | ollama v0.32.10 | **drop** · `production_engineering` | Weakest drop in the set. It changes the `repeat_penalty` default for everyone running Ollama — including this project — which is arguably publishable |
| 16 | AutoDesign | drop · `new_approaches` | Genuinely about agent harnesses, which argues for `ai_agents`. Filed as `new_approaches` because it is a method paper with no released framework |
| 20 | "AI agents lie, cheat and steal" | drop · `irrelevant` | Subject is agent trustworthiness, which is `safety_security`. Form is magazine opinion, which the rules call `irrelevant`. Form won |
| 23 | RingCentral case study | **drop** · `production_deployment` | Deployed at a named company, so maturity is high — but vendor-authored with no measurable results, so it is not published |

Item 23 is the useful one to internalise: **`human_label` and `human_maturity` are
independent.** A thing can be genuinely in production and still not belong in the digest.

---

## 3. Taxonomy gaps this exposed

Found while labelling. None block T1.5; all should be resolved before the source list
grows in M2.

1. **Experience reports have no maturity value.** Item 18 is a blog post about building a
   home AI box — no product, no paper, no repo. It was filed `announcement_only`, which
   is wrong in spirit but the least wrong available. A `blog_or_experience_report` value
   may be needed once more such sources are added.

2. **Video-to-text has no home.** `speech_voice` is defined around audio. Sign language
   translation is a language technology with a visual input. It landed in
   `new_approaches` by elimination.

3. **Subject versus form.** Item 20 is about a `safety_security` subject in an opinion
   article. The current rules resolve this — non-technical means `irrelevant` — but the
   rule is doing heavy lifting and should be stated explicitly in the prompt, or the
   model will file such pieces under the subject.

4. **Substance versus source.** Item 11 arrives as an Ollama changelog but announces an
   open 30B model that runs today. It was labelled `frontier_models`, not
   `production_engineering`. The prompt should say that the substance decides the topic,
   not the publisher.

5. **The same story from two sources.** Items 21 and 25 are Ultrafast mode, from Cerebras
   and from OpenAI. Both are individually keep-worthy and both are labelled keep. This is
   the clustering case for M2.2 — a correct classifier will keep both, and clustering,
   not classification, must merge them.

---

## 4. Distribution

| Label | Count |
|---|---|
| keep | 10 |
| drop | 16 |

| Topic | | Maturity | |
|---|---|---|---|
| `irrelevant` | 7 | `live_product` | 9 |
| `production_engineering` | 7 | `announcement_only` | 8 |
| `new_approaches` | 6 | `paper_only` | 5 |
| `frontier_models` | 4 | `public_pilot` | 2 |
| `ai_agents` | 2 | `reproducible_open_source` | 1 |
| | | `production_deployment` | 1 |

Two observations for the classifier:

- **`speech_voice`, `robotics`, `fintech`, `govtech`, `startups`, `technical_talks` and
  `safety_security` have zero examples.** Precision on those seven topics is unmeasured.
  The set proves nothing about them. It is a three-day sample from eight sources, and
  those topics need sources the project does not have yet.
- **`paper_only` plus `announcement_only` is 13 of 26 — exactly half.** Both are
  hard-excluded from publication, so the maturity filter alone removes half the input
  before ranking sees it.

---

## 5. What this implies for volume

A 38% keep rate against ~31 items per day gives roughly **12 publishable items per day**,
against a digest that takes 2–7. The daily cadence is supported with room to spare, and
the ranking stage will be selecting, not scraping the barrel.

Caveat: this rate comes from a 26-item sample chosen to span the quality range, not from
a random draw. The true rate is probably lower. Twelve against a ceiling of seven leaves
enough margin that the conclusion holds even if the real figure is half that.
