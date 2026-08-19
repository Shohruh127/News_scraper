# Publish Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a second `publish_digest()` call on the same digest post nothing that was already posted, and stop marking a digest FAILED when every channel post succeeded and only an appendix was missing.

**Architecture:** One guard and one split. `DigestItem.channel_message_id` already records that an item reached the channel; the publisher writes it and never reads it. Reading it is the whole fix. Separately, appendix failures are currently pooled with post failures into one `failed_items` list that decides the digest status, so a missing appendix marks a fully-published digest FAILED.

**Tech Stack:** Django, httpx, pytest, respx.

**Spec:** none — this closes a defect found on 2026-08-19 while auditing plans A–E.

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- The suite is green at **239 passed** before this plan and must stay green
- The worktree already carries uncommitted work from plans A–E. Do not commit it as part of this plan; commit only the files this plan names
- One Django app, functions over classes, no abstraction before the second case
- `tests/test_publish.py` has an autouse `zero_send_delay` fixture, so no test in this plan
  sets `TELEGRAM_SEND_DELAY`

---

## Why this change exists

Measured on the live channel, 2026-08-19, by probing every message ID from 1 to 105:

```
live in the channel                     82   (IDs 21, 25-105)
live and recorded in the database       21   (IDs 25-33, 94-105)
live with no database record            61   (IDs 21, 34-93)
```

**Seventy-four percent of the channel had no database record.** The owner deleted the 60-message
block by hand.

`publish_digest()` loops over every item and sends unconditionally:

```python
for idx, item in enumerate(items):
    ...
    res_post = send_message(chat_id=channel_id, text=post_html, ...)
    ...
    item.channel_message_id = ch_msg_id      # apps/digest/publish.py:295
    item.save(update_fields=["channel_message_id"])
```

`channel_message_id` appears in that file only as a write. It is never read as a guard, and no
test calls `publish_digest()` twice.

Two callers make a second call easy to reach:

```python
except IntegrityError:                                   # apps/digest/tasks.py
    digest = Digest.objects.get(digest_date=target_date)  # already published
res = publish.publish_digest(digest)                      # posts everything again
```

and `manage.py publish_digest --digest-id N`, which an operator runs after seeing a FAILED digest.

**Which is why the second half matters.** A digest is marked FAILED when `failed_items` is
non-empty, and a missing auto-forward appends to that same list:

```python
failed_items.append(f"#{item.position} (forward not found for msg {ch_msg_id})")
...
if failed_items:  # apps/digest/publish.py:337
    digest.status = Digest.Status.FAILED
```

Both the 2026-08-17 and 2026-08-19 digests are `failed` for this reason, with every channel post
delivered. FAILED invites a re-run; the re-run has no guard; the channel doubles. The error state
manufactures its own cause.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/digest/publish.py` | the per-item publisher | modify |
| `apps/digest/management/commands/publish_digest.py` | operator entry point | modify |
| `config/settings.py` | logging configuration | modify |
| `tests/test_publish.py` | publisher tests | modify |
| `docs/REMAINING_WORK.md` | the project's map | modify |

---

## Task 1: Refuse to post an item that already has a message ID

**Files:**
- Modify: `apps/digest/publish.py`
- Modify: `apps/digest/management/commands/publish_digest.py`
- Modify: `tests/test_publish.py`

**Interfaces:**
- Changes `publish_digest(digest, client=None)` → `publish_digest(digest, client=None, *, republish=False)`
- The returned dict gains `items_skipped: int`

- [ ] **Step 1: Write the failing test first**

Add to `tests/test_publish.py`, after `test_publish_digest_live_success_single_item`:

```python
@respx.mock
def test_publishing_twice_posts_each_item_once(db, digest_15, settings, monkeypatch):
    """The defect this guards: 61 of 82 live channel messages had no database record."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    counter = iter(range(600, 700))
    route = respx.post(f"{base_tg}/sendMessage").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"ok": True, "result": {"message_id": next(counter)}}
        )
    )
    DigestItem.objects.filter(digest=digest_15, position__gt=3).delete()

    first = publish.publish_digest(digest_15)
    calls_after_first = route.call_count
    second = publish.publish_digest(digest_15)

    assert first["items_sent"] == 3
    assert first["items_skipped"] == 0
    assert second["items_sent"] == 0
    assert second["items_skipped"] == 3
    assert route.call_count == calls_after_first, "the second run must send nothing"

    ids = list(
        DigestItem.objects.filter(digest=digest_15)
        .order_by("position")
        .values_list("channel_message_id", flat=True)
    )
    assert ids == [600, 601, 602], "the first run's message IDs must survive the second run"


@respx.mock
def test_a_partly_published_digest_resumes_instead_of_restarting(db, digest_15, settings):
    """An item whose send failed is the only one a second run may post."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    DigestItem.objects.filter(digest=digest_15, position__gt=3).delete()
    done = DigestItem.objects.filter(digest=digest_15, position__lt=3)
    for offset, item in enumerate(done):
        item.channel_message_id = 900 + offset
        item.save(update_fields=["channel_message_id"])

    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 950}})
    )

    res = publish.publish_digest(digest_15)

    assert res["items_sent"] == 1
    assert res["items_skipped"] == 2
    assert route.call_count == 1
    assert DigestItem.objects.get(digest=digest_15, position=3).channel_message_id == 950


@respx.mock
def test_republish_overrides_the_guard(db, digest_15, settings):
    """The one escape hatch: a post deleted by hand can be sent again, on purpose."""
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_GROUP_ID = ""
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    DigestItem.objects.filter(digest=digest_15, position__gt=1).delete()
    item = DigestItem.objects.get(digest=digest_15, position=1)
    item.channel_message_id = 700
    item.save(update_fields=["channel_message_id"])

    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 800}})
    )

    res = publish.publish_digest(digest_15, republish=True)

    assert res["items_sent"] == 1
    assert res["items_skipped"] == 0
    assert route.call_count == 1
    item.refresh_from_db()
    assert item.channel_message_id == 800
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_publish.py -q -k "twice or resumes or republish"
```

Expected: all three FAIL. The first two on `KeyError: 'items_skipped'`; the third on the same key.
If any of them *passes*, stop and report — the guard would already exist and this plan is wrong
about the code.

- [ ] **Step 3: Add the guard**

In `apps/digest/publish.py`, change the signature:

```python
def publish_digest(
    digest: Digest,
    client: httpx.Client | None = None,
    *,
    republish: bool = False,
) -> dict:
```

Extend the docstring's bullet list with:

```
    - Idempotent by default: an item that already carries a channel_message_id is skipped, so a
      second call resumes a partial run instead of posting the digest again. Measured 2026-08-19,
      before this guard existed: 61 of the 82 live channel messages had no database record.
      Pass republish=True only to deliberately re-send posts that were deleted by hand.
```

Declare the counter beside `sent_count`:

```python
    sent_count = 0
    skipped_count = 0
    failed_items: list[str] = []
```

Then make the guard the **first** thing in the loop, above the rate-limit sleep, so a skipped item
costs no delay:

```python
        for idx, item in enumerate(items):
            if item.channel_message_id and not republish:
                skipped_count += 1
                log.info(
                    "Item #%s already posted as message %s; skipping",
                    item.position,
                    item.channel_message_id,
                )
                continue

            # Rate-limit: configurable delay between sends (20 msg/min budget shared with appendix)
            if idx > 0 and send_delay > 0:
                time.sleep(send_delay)
```

Add the key to both returned dicts. In the kill-switch branch:

```python
            "items_sent": 0,
            "items_skipped": 0,
            "items_failed": 0,
```

and in the final return:

```python
        "items_sent": sent_count,
        "items_skipped": skipped_count,
        "items_failed": len(failed_items),
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_publish.py -q
```

Expected: PASS, with three more tests than before this plan.

- [ ] **Step 5: Prove the guard has teeth**

Delete the two words `and not republish` from the guard condition, so it reads
`if item.channel_message_id:`, and run:

```bash
uv run pytest tests/test_publish.py -q -k republish
```

Expected: FAIL. Restore the words. Then delete the whole guard block and run:

```bash
uv run pytest tests/test_publish.py -q -k "twice or resumes"
```

Expected: FAIL on both. Restore it. Report both mutation results; a guard whose removal leaves the
suite green is not a guard.

- [ ] **Step 6: Give the operator the escape hatch**

In `apps/digest/management/commands/publish_digest.py`, add the argument:

```python
parser.add_argument(
    "--republish",
    action="store_true",
    help="Re-send items that already have a channel_message_id. Use only after deleting "
    "those posts by hand; without it, published items are skipped.",
)
```

Pass it through:

```python
        res = publish.publish_digest(digest, republish=options["republish"])
```

And report the count, replacing the success line:

```python
        self.stdout.write(
            style(
                f"Digest {res['digest_date']}: status={res['status']}, "
                f"items_sent={res['items_sent']}, items_skipped={res['items_skipped']}, "
                f"items_failed={res['items_failed']}"
            )
        )
```

The file currently ends without a trailing newline. Add one.

- [ ] **Step 7: Update the command tests**

In `tests/test_management_commands.py`, both fake return dicts need `"items_skipped": 0`, and
`test_command_reports_stable_item_summary` gains:

```python
    assert "items_skipped=0" in output
```

---

## Task 2: A missing appendix must not fail a delivered digest

**Files:**
- Modify: `apps/digest/publish.py`
- Modify: `tests/test_publish.py`

**Interfaces:**
- The returned dict gains `appendix_failures: list[str]`. `failed_items` keeps its meaning:
  items whose **channel post** did not go out

- [ ] **Step 1: Write the failing test first**

Add to `tests/test_publish.py`:

```python
@respx.mock
def test_a_missing_appendix_alerts_but_leaves_the_digest_published(
    db, digest_15, settings, monkeypatch
):
    """Every post landed. A missing auto-forward is a degraded post, not a failed digest.

    Marking it FAILED is what invited the re-runs that put 61 untracked messages in the channel.
    """
    settings.PUBLISHING_ENABLED = True
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_GROUP_ID = "-100222222"
    settings.TELEGRAM_ADMIN_CHAT_ID = "999888777"
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    counter = iter(range(500, 600))
    respx.post(f"{base_tg}/sendMessage").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"ok": True, "result": {"message_id": next(counter)}}
        )
    )
    monkeypatch.setattr(publish, "find_group_forward_message_id", lambda _msg_id: None)
    DigestItem.objects.filter(digest=digest_15, position__gt=2).delete()

    res = publish.publish_digest(digest_15)

    assert res["items_sent"] == 2
    assert res["items_failed"] == 0
    assert len(res["appendix_failures"]) == 2
    assert res["status"] == Digest.Status.PUBLISHED

    digest_15.refresh_from_db()
    assert digest_15.status == Digest.Status.PUBLISHED
    assert digest_15.published_at is not None
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_publish.py -q -k missing_appendix
```

Expected: FAIL — `res["status"]` is `failed` and `items_failed` is 2.

- [ ] **Step 3: Split the two failure kinds**

Declare the second list beside `failed_items`:

```python
    failed_items: list[str] = []
    appendix_failures: list[str] = []
```

Change the two appendix error paths to use it. The render failure:

```python
                    except ValueError as exc:
                        log.error("Appendix render failed for item #%s: %s", item.position, exc)
                        appendix_failures.append(f"#{item.position} (appendix render: {exc})")
                        continue
```

and the missing forward:

```python
appendix_failures.append(f"#{item.position} (forward not found for msg {ch_msg_id})")
```

Replace the whole status decision block with:

```python
    # --- Status decision ---
    # Only a channel post that did not go out fails a digest. An appendix that did not land
    # leaves a degraded post, and marking the digest FAILED for it invites a re-run that
    # posts everything a second time.
    if failed_items:
        digest.status = Digest.Status.FAILED
        digest.save(update_fields=["status"])
        alert_msg = (
            f"Digest {digest.digest_date}: {sent_count}/{len(items)} items posted. "
            f"Failed: {', '.join(failed_items)}"
        )
        send_admin_alert(alert_msg)
        log.error(alert_msg)
    else:
        digest.status = Digest.Status.PUBLISHED
        digest.published_at = timezone.now()
        digest.save(update_fields=["status", "published_at"])
        log.info("Digest %s published: %s items posted", digest.digest_date, sent_count)

    if appendix_failures:
        appendix_msg = (
            f"Digest {digest.digest_date}: every post was delivered, but "
            f"{len(appendix_failures)} appendix message(s) were not: "
            f"{', '.join(appendix_failures)}. Check that the bot service is running."
        )
        send_admin_alert(appendix_msg)
        log.warning(appendix_msg)
```

Add the key to the final return, and `"appendix_failures": []` to the kill-switch return:

```python
        "failed_items": failed_items,
        "appendix_failures": appendix_failures,
```

- [ ] **Step 4: Run it to verify it passes, then run the file**

```bash
uv run pytest tests/test_publish.py -q
```

Expected: PASS. `test_failure_on_item_8_leaves_1_through_7_sent_digest_failed` must still pass
unchanged — that is a *post* failure and still fails the digest. If it broke, the split went the
wrong way; stop and report.

- [ ] **Step 5: Record why the bot service is now load-bearing**

In `docs/REMAINING_WORK.md`, under **### Measured facts an executor will need**, add:

```
| Appendix delivery | The publisher reads the auto-forward ID from Redis, and only the `bot` service writes it. With `bot` down every appendix is missed — the posts still land, and the digest stays `published` with an admin alert |
| Publish idempotency | `publish_digest` skips items that already carry a `channel_message_id`. Measured 2026-08-19 before the guard: 61 of 82 live channel messages had no database record |
```

---

## Task 3: Stop printing the bot token

**Files:**
- Modify: `config/settings.py`
- Modify: `tests/test_publish.py`

- [ ] **Step 1: Write the failing test first**

```python
def test_a_publish_never_writes_the_bot_token_to_the_log(db, digest_1, settings, caplog):
    """The token is in the URL of every Telegram call. httpx logs URLs at INFO.

    It has leaked into terminal output twice. The logger config is the fix, not discipline.
    """
    import logging

    settings.PUBLISHING_ENABLED = False
    settings.TELEGRAM_BOT_TOKEN = "123456:SECRET-TOKEN-VALUE"

    with caplog.at_level(logging.DEBUG):
        publish.publish_digest(digest_1)

    assert "SECRET-TOKEN-VALUE" not in caplog.text
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_publish.py -q -k never_writes_the_bot_token
```

Expected: FAIL on the `httpx` level assertion — the logger inherits root's INFO.

- [ ] **Step 3: Silence the two loggers**

In `config/settings.py`, inside `LOGGING["loggers"]`, after the `trafilatura` entry:

```python
        # httpx logs every request URL at INFO, and every Telegram URL contains the bot token.
        # It has been printed to a terminal twice. Raise the level rather than rely on care.
        "httpx": {"level": "WARNING", "propagate": True},
        "httpcore": {"level": "WARNING", "propagate": True},
```

- [ ] **Step 4: Run it to verify it passes**

```bash
uv run pytest tests/test_publish.py -q -k never_writes_the_bot_token
```

Expected: PASS.

- [ ] **Step 5: Confirm on a real call**

```bash
uv run python manage.py source_yield
```

Expected: the table, and no line containing `api.telegram.org`. Paste the first five lines.

---

## Task 4: Full suite, ruff, and commit

- [ ] **Step 1: Run everything**

```bash
uv run pytest -q
uv run ruff check .
uv run python manage.py check
```

Expected: green, with **six** new tests from this plan — three in Task 1, one in Task 2, one in
Task 3, plus the amended command assertions. `ruff check .` reports `All checks passed!`.

- [ ] **Step 2: Rebuild, because this is a code change**

```bash
docker compose build
docker compose up -d
```

`docker compose restart` does not pick up a code change. Confirm with:

```bash
docker compose exec worker grep -c "items_skipped" apps/digest/publish.py
```

Expected: a non-zero count. A zero means the containers are still running the old image.

- [ ] **Step 3: Commit only this plan's files**

The worktree carries uncommitted work from plans A–E. Stage these paths and nothing else:

```bash
git add apps/digest/publish.py \
        apps/digest/management/commands/publish_digest.py \
        config/settings.py \
        tests/test_publish.py \
        tests/test_management_commands.py \
        docs/REMAINING_WORK.md \
        docs/superpowers/plans/2026-08-19-publish-idempotency.md
git commit -m "Post each digest item once, and stop failing a digest over a missing appendix"
```

- [ ] **Step 4: STOP and report**

Report the two mutation results from Task 1 Step 5, the Task 3 Step 5 output, and the
`docker compose exec` count. Do not re-publish any existing digest.

---

## Not in this plan, and why

**The stale `channel_message_id` values are left alone.** Fifteen items from 2026-08-17 and three
from 2026-08-18 point at messages that no longer exist in the channel. They are not cleared,
because the field records that an item *was* published, which stays true after a manual deletion.
Clearing them would also hand the next run permission to post them again — the opposite of this
plan. `--republish` is the deliberate path when a re-send is actually wanted.

**`compose_and_publish`'s IntegrityError fallback is unchanged.** It reuses the existing digest and
calls `publish_digest`, which is now safe: already-posted items are skipped and any item whose send
failed is retried. That is the behaviour the fallback was always reaching for.

**Cross-day subject repetition is not addressed here.** `ollama/ollama` produced six posts in three
days, one per patch release. The per-digest `(subject_key, topic)` cap cannot see across days, and
closing that needs a decision about what a reader should get when a project ships daily. Separate
plan.

---

## Self-review

**Coverage**

| Gap found on 2026-08-19 | Task |
|---|---|
| `publish_digest` posts every item on every call | 1, Steps 1–5 |
| No test ever called `publish_digest` twice | 1, Step 1 |
| A partial run cannot be resumed without duplicating the rest | 1, Step 1, second test |
| No deliberate path to re-send a hand-deleted post | 1, Step 6 |
| A missing appendix marks a fully delivered digest FAILED | 2 |
| FAILED status invites the re-run that duplicates the channel | 2, Step 3 |
| The bot service is load-bearing and recorded nowhere | 2, Step 5 |
| The bot token reaches the terminal through httpx INFO logs | 3 |

**Placeholder scan:** none. Every step carries its code or command and its expected output.

**Type consistency:** `items_skipped` is an `int` in both return paths, including the kill-switch
branch, so `res["items_skipped"]` is safe for the management command without a `.get()`.
`appendix_failures` is a `list[str]` in both paths for the same reason.

**One note for the reviewer.** Task 2 changes when a digest is called FAILED, which is a
behavioural change to an existing acceptance test's neighbourhood. The distinction it draws is
that `failed_items` means *a reader did not get the post* and `appendix_failures` means *a reader
got the post without its technical appendix*. If that distinction is wrong, Task 1 still stands on
its own and should ship regardless — it is the half that stops the duplication.
