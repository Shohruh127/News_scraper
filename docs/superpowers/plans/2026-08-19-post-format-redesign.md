# Post Format Redesign — Corrected Implementation Plan

**Date:** 2026-08-19
**Status:** implementation may start; production cutover is blocked by measurement and owner approval
**Spec:** docs/superpowers/specs/2026-08-19-post-format-redesign.md

## Outcome

Each item becomes clean Uzbek prose: safe image above when available; no heading, bullets, or visible Nextgov/GitHub/Source footer; exactly one source link inside one word in the first sentence (preferably its final verb); one closed topic hashtag as the final token; optional <=8-word kicker; <=900 visible characters.

This plan does not authorize deployment, production migration, commits, or pushes.

## Corrections

1. Build v2 behind POST_FORMAT_V2_ENABLED=False, measure real articles, then request approval before cutover/deletion.
2. Use real contracts: EditorialEn, Translation, EDITORIAL_EN_SCHEMA, TRANSLATION_SCHEMA, COMMON_TRANSLATED_FIELDS, TRANSLATION_PROMPT, translation_schema_for(fields). There is no EditorialUz.
3. Translation gates check content fidelity; post_format checks final escaped HTML, one link, tag placement, and visible length.
4. Ingestion records image_url in Article.meta while HTML exists; publisher has only a bounded legacy fallback.
5. Kicker suppression is render-time: announcement_only is normally excluded and evidence may be promoted after editorial generation.
6. Cover sendPhoto/editMessageCaption, uncertain Telegram failures, and joint channel_message_id + sent_as_photo persistence.
7. Remove legacy debt only after approved stable rollout.

## Guardrails and target flow

Do not change ranking/clustering/source policy or broaden the kicker conjunction to OR. Probe is read-only. Preserve flattened technical appendix translation and historical reads. Before each task reopen files/tests, inspect git status, write focused tests first, make a surgical change, run focused tests, and double-check the diff.

    fetch/connector -> Article.meta["image_url"]
    editorial -> optional evidence promotion -> v2 renderer
    renderer -> escaped prose + one linked word + one final tag + <=900 chars
    publisher -> sendPhoto/editMessageCaption OR sendMessage/editMessageText

Record a fresh baseline. The previously seen 239 passes are context, not a fixed expected count.

## Task 0 — Baseline and caller audit

Inspect apps/digest/{llm,tasks,publish,ranking,translation_gates,extract,models}.py, templates, tests/test_editorial.py, tests/helpers.py, tests/test_llm.py, tests/test_publish.py, and tests/test_translation_gates.py.

- Run full tests, ruff, Django check, git diff --check.
- rg every runtime caller of render_item_post, render_channel_post, render_group_comment.
- Confirm editorial/evidence/compose/publish order and existing factories.
- Make no production change.

## Task 1 — Default-off boundary

Modify config/settings.py, .env.example, tests/test_settings.py. Add POST_FORMAT_V2_ENABLED via the existing boolean convention, default False. Test missing env=false and flag-off preserves current rendering/publishing.

    uv run pytest tests/test_settings.py tests/test_publish.py -q

## Task 2 — Real LLM contracts

Modify llm.py, existing prompt location, tests/test_editorial.py, tests/test_llm.py, tests/helpers.py, docs/CONTENT_SCHEMA.md.

EditorialEn v2: lead, body_1, body_2, optional kicker, link_anchor. Translation remains dynamic/flattened; add fields through COMMON_TRANSLATED_FIELDS and translation_schema_for(fields). Never add EditorialUz or nested technical_uz.

Prompt: no headline/bullets/markdown/source footer/hashtags; first sentence ends with a natural one-word verb anchor; short prose bodies; optional <=8-word cliché-free kicker with no new number; translation preserves numbers/glossary and returns Uzbek anchor.

Compatibility: reuse checks accept lead_en or summary_en and lead_uz or summary_uz; v1 stays readable; technical appendix/evidence remains flattened; extend shared factories.

Test v2 schema, dynamic Translation, v1/v2 reuse, and real prompt names.

    uv run pytest tests/test_editorial.py tests/test_llm.py -q

## Task 3 — Pure post-format layer

Create apps/digest/post_format.py and tests/test_post_format.py. Modify translation_gates/tests. Do not delete legacy templates.

TOPIC_TAGS invariant:

    set(TOPIC_TAGS) == set(Topic) - {Topic.IRRELEVANT}

Tags are lowercase, one token; unknown/irrelevant fails closed; exactly one is final.

translation_gates keeps number/glossary fidelity, kicker <=8 words, cliché ban, and no new kicker number. Final markup/length belongs in post_format.

Add focused equivalents of resolve_anchor, linkify_lead, visible_length, trim_post_fields, validate_rendered_post, render_item_post_v2.

Rules:

- escape all LLM text/URL attributes; http/https only;
- requested anchor is one lexical token in first sentence; link first occurrence;
- missing anchor falls back to first sentence final lexical token; measurement judges verb quality;
- exactly one a; no b/i/code/blockquote, markdown, bullets, or visible source label;
- one approved hashtag is final.

Visible count strips tags and decodes entities. Over 900: remove trailing body_2 sentences, then body_1, then kicker; if lead+tag remains >900, fail/alert. Never split HTML/words. Test punctuation/decimals/versions, escaping, duplicate/fallback anchor, invalid URL/topic, trim order, 900/901.

    uv run pytest tests/test_post_format.py tests/test_translation_gates.py -q

## Task 4 — Safe image path

Create media.py/tests. Modify extract.py, relevant connector normalization/tests, tests/test_extract.py, pyproject.toml, uv.lock. Add Pillow via uv.

Discovery receives html+base_url. Precedence: og:image:secure_url, og:image, twitter:image. Resolve relatives. While extraction has HTML, merge image_url into Article.meta; preserve connector metadata. Only legacy rows without it may trigger one bounded page lookup.

Safety: http/https only; no credentials; DNS reject loopback/private/link-local/reserved/unspecified/multicast on every redirect; cap redirects/timeouts; stream with byte cap; verify status/MIME/magic/decoded format/dimensions; reject corrupt/zero/oversized/unsupported/decompression-bomb images; mocked offline tests; stable reason codes; no secret/query logging.

Tests cover precedence, relative URL, meta merge, bounded fallback, private redirect, early size stop, MIME spoofing, corruption/dimensions/timeout, valid image.

    uv run pytest tests/test_media.py tests/test_extract.py -q

## Task 5 — Photo-aware publishing, editing, and idempotency

Modify models.py, admin.py, a new digest migration, publish.py, edit_digest.py, tests/test_models.py, tests/test_publish.py, and tests/test_management_commands.py.

Add DigestItem.sent_as_photo = BooleanField(default=False), read-only in delivery admin. Persist channel_message_id and sent_as_photo together only after Telegram confirms success.

Paths:

- valid image -> sendPhoto with caption + feedback reply_markup;
- no/invalid preflight image -> sendMessage with preview explicitly disabled;
- photo edit -> editMessageCaption;
- text edit -> editMessageText.

Serialize reply_markup correctly in multipart. Preserve parse mode/callbacks, kill switch, auto-forward, and idempotency.

Text fallback is allowed only before Telegram is called when no usable image exists, or after a deterministic 400 photo rejection proving non-acceptance. Never fallback after timeout, connection loss, 429, or 5xx because the photo may have succeeded; use existing retry/failure alerting to avoid duplicates.

Audit TELEGRAM_LINK_PREVIEW. Channel fallback always disables preview. If no intentional consumer remains, remove this dead setting in Task 9.

Tests cover multipart/keyboard, disabled preview, no uncertain fallback, deterministic one-time fallback, both edit endpoints, joint persistence, kill switch, retry, forwarding, and callbacks.

Verify:

    uv run pytest tests/test_publish.py tests/test_management_commands.py tests/test_models.py -q
    uv run python manage.py makemigrations --check --dry-run

## Task 6 — Integrate v2 without activation

Modify tasks.py, publish.py, relevant pipeline tests, and ranking.py only if it currently owns a renderer call; keep presentation out of ranking.

- Flag off is unchanged v1 behavior.
- Flag on uses post_format + media publishing.
- Final rendering sees classification after optional evidence promotion. Preferred order: editorial payload -> promotion -> final compose/render -> publish.
- Suppress kicker only when evidence_level == vendor_claim_only AND maturity == announcement_only.
- Test that normal ranking still excludes announcement_only; the conjunction only defends manually composed/legacy items.
- Either condition alone preserves kicker.
- Render failure publishes nothing and uses existing failure/alert behavior.

Verify focused tests, then uv run pytest -q. Leave the flag false.

## Task 7 — Pre-cutover editorial measurement

Create spikes/probe_post_format.py and docs/spike/POST_FORMAT_MEASUREMENT.md.

The probe imports v2 directly. It never changes the flag, publishes, or updates Article, Classification, or DigestItem.

Use 8–10 real classified articles across >=4 archetypes, including valid-image, no-image, multi-number technical, long/trimmed, and likely anchor-fallback items.

Record per item: id/title/source/archetype; generated fields and final preview; requested/resolved anchor, fallback, and human verb judgment; tag; visible count and every trim; kicker count/suppression; image source/result/reason; all validation errors.

Gate criteria:

- every preview: exactly one inline link and one final approved tag;
- no heading, bullets, raw HTML, markdown, or source footer;
- median <600 and max <=900 visible characters;
- zero invalid kickers and number/glossary regressions;
- anchor fallback <20% and every anchor reads naturally;
- both image/no-image paths reviewed;
- zero probe publications.

Run:

    uv run python spikes/probe_post_format.py

## STOP GATE 1 — Editorial approval

Stop. Owner marks the report APPROVED or requests revisions. Until approval: flag stays false; no legacy deletion, deployment, or production migration. Failed criteria require iteration and a rerun.

## Task 8 — Approved cutover preparation and smoke test

Open only after Gate 1 approval. Update CONTENT_SCHEMA and milestone/runbook docs. Enable only the reviewed environment, never repository defaults.

Pre-deployment checks:

    uv run pytest -q
    uv run ruff check .
    uv run python manage.py check
    uv run python manage.py makemigrations --check --dry-run
    uv run python manage.py migrate --plan
    docker compose config
    docker compose build
    git diff --check

After separate deployment approval, smoke-test in a non-production channel:

1. valid image uses sendPhoto;
2. no image uses sendMessage with no preview;
3. both show one linked word, no source footer, one final tag;
4. photo/text edits use caption/text endpoints;
5. feedback works on both;
6. forwarding and appendix remain correct;
7. retry is duplicate-free;
8. kill switch blocks both;
9. delivery failure alerts.

## STOP GATE 2 — Production approval

Do not migrate production, change production env, or run docker compose up -d without a new explicit owner instruction after smoke evidence is reviewed.

## Task 9 — Legacy debt after stable rollout

Open only after owner confirms v2 is stable and v1 rollback is unnecessary. After a fresh rg caller audit, remove:

- six legacy archetype templates and old item_post branches;
- unused render_channel_post/render_group_comment and channel_post/group_comment templates;
- v1-only headline/bullet prompt instructions/tests;
- dead TELEGRAM_LINK_PREVIEW configuration;
- obsolete comments such as stale “No aiogram” assumptions;
- the feature flag after all environments permanently use v2.

Do not remove legacy JSON read compatibility while historical items may need editing. Keep summary_* fallback when required and document its retirement condition.

Verify:

    rg -n "render_channel_post|render_group_comment|summary_en|summary_uz|TELEGRAM_LINK_PREVIEW" apps tests docs .env.example
    uv run pytest -q
    uv run ruff check .
    uv run python manage.py check
    git diff --check

Every remaining match must be intentional compatibility or historical documentation.

## Final acceptance

- [ ] Default-off boundary and v1 path tested.
- [ ] Real EditorialEn/dynamic Translation contracts used; technical appendix preserved.
- [ ] Reuse guards support v2 and historical payloads.
- [ ] Translation and final render validation are separate.
- [ ] Escaped output has exactly one linked word and no visible source footer.
- [ ] Exactly one closed hashtag is final; output is <=900.
- [ ] Image discovery is wired to real HTML/meta and protected against SSRF/size/type/dimension risks.
- [ ] Photo/text sends and edits handle uncertain failures without duplication.
- [ ] channel_message_id + sent_as_photo remain idempotent.
- [ ] Kicker uses current post-promotion state and the approved conjunction.
- [ ] Probe criteria and editorial approval are recorded.
- [ ] Full tests/static/Django/migration/compose/build checks pass.
- [ ] Smoke evidence is approved before production.
- [ ] Legacy debt is removed only after stability approval and a caller audit.

Execute tasks one by one and double-check every focused diff. This document authorizes planning and scoped implementation only, not deployment or production writes.
