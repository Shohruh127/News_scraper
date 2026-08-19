# Plan: Telegram feedback bot

> **Execution:** Work task-by-task. Stop if the callback validation, Redis handoff, or live
> test-channel acceptance fails; do not enable the public channel while either update path is
> unobserved.

**Current status (2026-08-19):** Implemented in the worktree: aiogram bot callbacks, Redis forward
correlation with TTL/retry, shared reaction keyboard, stale result-contract cleanup, management
command output, Docker service, and focused tests. The public publishing kill switch remains an
explicit deployment setting; changes are not committed or deployed.

**Goal:** put three working reactions on every per-item channel post and persist one reaction
per Telegram user per `DigestItem`, while keeping one owner of Telegram's update queue.

**Depends on:** the per-item publishing flow already in `apps/digest/publish.py`, Postgres, and
the Redis service already used by Celery.

**Out of scope:** ranking weights, feedback learning, fine-tuning, discussion comments, or a new
public HTTP endpoint. Learning starts only after enough reactions exist to measure.

## Existing contracts

- `DigestItem.channel_message_id` identifies the individual channel post.
- `DigestItem.group_message_id` is the ID of the technical appendix sent by this application.
  It is not the linked group's automatic-forward ID and must keep this meaning.
- `Feedback.Reaction` already has `useful`, `not_useful`, and `want_to_build`.
- `Feedback` already enforces `one_reaction_per_user_per_item`.
- Publishing is synchronous `httpx`; only the bot needs `aiogram` and long polling.
- `find_group_forward_message_id()` currently calls Telegram `getUpdates`. A second long-polling
  process with the same token would compete for one shared update queue.
- Redis is already a runtime dependency through `settings.CELERY_BROKER_URL`.
- Legacy debt: live and suppressed `publish_digest()` paths return different dictionary shapes,
  while the management command still expects removed digest-level channel/group message IDs.

## Design decisions

1. Callback data is `feedback:<digest_item_id>:<reaction>`. Parse and validate every part.
2. The publisher sends a raw Bot API `reply_markup` dictionary and does not import `aiogram`.
3. A callback is valid only when its chat ID and message ID match the configured channel and the
   stored `DigestItem.channel_message_id`. Rejected callbacks create no row and are answered.
4. `apps.digest.bot` is the only `getUpdates` owner. It handles callbacks and linked-group
   automatic forwards.
5. Automatic-forward handoff uses a short-lived Redis mapping, not a model field:
   `news_radar:telegram_forward:<channel_id>:<channel_message_id>` stores the destination group's
   forward message ID with `TELEGRAM_FORWARD_TTL` (default 300 seconds). Including the channel ID
   prevents collisions across test and production channels.
6. The bot validates configured origin channel and destination group, then writes the Redis key.
   It must not require a `DigestItem` lookup first: Telegram can deliver the update before the
   publisher has saved `channel_message_id`. The publisher only waits for IDs it just sent, so an
   unrelated channel post cannot be consumed by a digest item.
7. `group_message_id` remains appendix-only. The publisher waits for the Redis forward ID, sends
   the appendix as a reply to that forward, then stores the appendix response ID in
   `group_message_id` exactly as it does today.
8. The first reaction row wins. A repeated or concurrent click by the same user/item is reported
   as already recorded, never as a process error. Telegram user ID, not username, is identity.
9. Publication returns one stable summary shape from live and suppressed paths. The management
   command reports item counts/status and never refers to removed aggregate message IDs.

## Task 1 — Add the keyboard to every channel post

Files:

- `apps/digest/publish.py`
- `tests/test_publish.py`

1. Add a pure `feedback_keyboard(digest_item_id)` helper. Keep reaction values in one shared
   constant importable by the bot.
2. Extend `send_message()` with optional `reply_markup`; include it only when supplied.
3. Pass the keyboard only to the channel post. The group appendix has no keyboard.
4. Through `respx`, assert the first `sendMessage` payload has exactly three callback values and
   the appendix payload has no `reply_markup`.
5. Preserve the kill switch: no request and no message ID when publishing is disabled.

Acceptance:

```bash
uv run pytest tests/test_publish.py -q
```

## Task 2 — Make the bot the sole update consumer and hand forwards through Redis

Files:

- `apps/digest/bot.py` (new)
- `apps/digest/telegram_updates.py` (new)
- `apps/digest/publish.py`
- `config/settings.py`
- `tests/test_bot.py` (new)
- `tests/test_telegram_updates.py` (new)
- `tests/test_publish.py`
- `pyproject.toml`
- `uv.lock`

1. Add an `aiogram>=3,<4` release compatible with Python 3.13 and commit the resolved lock.
2. In `telegram_updates.py`, own the key format and synchronous Redis operations. Provide small
   helpers equivalent to `remember_group_forward(channel_id, channel_message_id,
   forward_message_id, client=None, ttl=None)` and `wait_for_group_forward(channel_id,
   channel_message_id, client=None, max_retries=4, retry_delay=1.5)`. Default clients use
   `settings.CELERY_BROKER_URL`; tests inject a fake client.
3. Add `TELEGRAM_FORWARD_TTL = env.int(..., default=300)`. Every mapping uses Redis `SET` with
   expiry. Missing Redis or timeout returns `None`/fails soft to the publisher's existing visible
   item failure; it never changes database message IDs.
4. Initialise Django in `apps.digest.bot`, register an aiogram dispatcher, and poll only under
   `python -m apps.digest.bot`. Fail clearly when the bot token is empty.
5. The callback handler parses IDs/reactions, validates channel and stored message IDs, writes
   with `get_or_create`, handles the unique race, and always answers the callback.
6. The automatic-forward handler accepts only the configured origin channel and destination
   group. It writes the Redis mapping without waiting for the publisher's database save. Call the
   synchronous Redis helper via `asyncio.to_thread` (or an equivalent non-blocking wrapper).
7. Replace the publisher's HTTP `getUpdates` implementation with the shared Redis wait helper.
   Preserve bounded `max_retries`/`retry_delay`; remove Telegram offsets and client polling.

Tests:

- all three valid callbacks create the expected `Feedback` value;
- duplicate/concurrent clicks produce one row and an acknowledged duplicate response;
- malformed data, wrong channel/message, missing user, and unknown item write nothing;
- a valid forward writes a TTL-bound key; wrong origin/destination and ordinary messages do not;
- a mapping written before `DigestItem.channel_message_id` is saved is still found later;
- publisher waits on Redis, never calls `getUpdates`, times out cleanly, replies to the forward,
  and stores only the appendix response ID in `group_message_id`;
- all tests use fake aiogram/Redis objects and no live network or polling loop.

Acceptance:

```bash
uv run pytest tests/test_bot.py tests/test_telegram_updates.py tests/test_publish.py tests/test_models.py -q
uv run ruff check apps/digest tests/test_bot.py tests/test_telegram_updates.py tests/test_publish.py
```

## Task 3 — Retire the stale publish-result contract

Files:

- `apps/digest/publish.py`
- `apps/digest/management/commands/publish_digest.py`
- `tests/test_publish.py`
- `tests/test_management_commands.py` (new)

1. Make both live and suppressed `publish_digest()` returns contain `digest_id`, `digest_date`,
   `status`, `items_sent`, `items_failed`, `failed_items`, and `suppressed`. Suppressed means zero
   attempted sends and leaves status composed. Do not return misleading digest-level
   channel/group IDs from the per-item architecture.
2. Update the management command to print a warning for suppression and an item-count/status
   summary for a real attempt. It must not index `channel_message_id` or `group_message_id`.
3. Test suppressed, successful, and partial-failure output with the publisher mocked. Pin the
   dictionary contract in publish tests so another branch cannot silently diverge.

Acceptance:

```bash
uv run pytest tests/test_publish.py tests/test_management_commands.py -q
```

## Task 4 — Add the bot service and runtime configuration

Files:

- `docker-compose.yml`
- `.env.example`
- `docs/PROJECT_PLAN.md` or `docs/REMAINING_WORK.md`

1. Add a `bot` service using the worker image and `uv run python -m apps.digest.bot`. It depends
   on healthy Postgres and Redis and uses `restart: unless-stopped`.
2. Document `TELEGRAM_CHANNEL_ID`, `TELEGRAM_GROUP_ID`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_FORWARD_TTL`, and `PUBLISHING_ENABLED`. Remove stale `TELEGRAM_TEST_*` names or mark
   them explicitly local-only.
3. Document the invariant: one process calls `getUpdates`; Redis carries automatic-forward IDs;
   `DigestItem.group_message_id` stores the appendix ID.

Acceptance:

```bash
docker compose config
docker compose build bot
```

## Task 5 — Full verification and handoff

```bash
uv run pytest -q
uv run ruff check .
uv run python manage.py migrate --check
docker compose config
```

Run against a test channel. Publish one item and use three different Telegram accounts, one
reaction each, to create exactly three rows. Then click a second reaction from one of those
accounts and verify the count stays three. Observe both a callback and automatic-forward Redis
handoff before enabling the public channel.

## Not in this plan

- No feedback-learning or ranking update.
- No edit of old posts.
- No database migration: `Feedback` exists, and Redis owns only transient forward correlation.
- No second bot token; buttons sent by the publishing bot belong to that bot.
- No repurposing or renaming of `group_message_id`; a clearer schema name can be a later migration.

## Self-review

| Requirement | Covered by |
|---|---|
| Buttons are on individual posts | Task 1 |
| One reaction per user/item | Existing constraint + Task 2 |
| Invalid callbacks cannot write | Task 2 validation |
| One `getUpdates` owner | Task 2 |
| Early auto-forward cannot be lost | TTL Redis handoff test |
| Appendix ID semantics stay intact | Task 2 invariant and publish test |
| Stale management-command contract is removed | Task 3 |
| Runtime has Postgres and Redis | Task 4 |
| Live row count is possible | Task 5 uses three accounts |
