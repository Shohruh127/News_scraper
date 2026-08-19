# Alert Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a source-failure alert being recorded as sent when it was not, so a degraded source is reported rather than silently marked as already reported.

**Architecture:** `send_admin_alert` returns whether Telegram accepted the message, and `_alert_once_per_day` writes `last_alerted_on` only when it did. Nothing else changes: the rate limit, the ADR-002 rule that a source is never auto-disabled, and the degradation threshold all stay exactly as they are.

**Tech Stack:** Django, httpx, respx, pytest.

**Spec:** none — bounded fix for a defect measured on 2026-08-19 while exercising ADR-002.

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- The suite must stay green: `uv run pytest -q` → **174 passed** before this plan
- ADR-002 is unchanged: `enabled` is never touched by failure handling. Only a human disables a source
- The once-per-day rate limit stays. A permanently broken source must not flood the admin chat
- One Django app, functions over classes, no abstraction before the second case

---

## Why this change exists

Exercising ADR-002 on 2026-08-19 with a deliberately broken source produced the right policy
outcome:

```
attempt  consecutive_failures  is_degraded  enabled  last_alerted_on
   1              1              False       True        None
   2              2              False       True        None
   3              3              True        True     2026-08-19
```

The source degraded at the threshold and was not disabled, which is what ADR-002 promises. But
reading the code around that result shows the alert flag is optimistic:

```
tasks.py:159   source.last_alerted_on = today      set BEFORE any attempt
tasks.py:173   publish.send_admin_alert(msg)       returns None — no success signal
               └─ inside, a non-200 becomes log.warning and nothing else
tasks.py:174   except Exception: log.warning       an exception is swallowed too
```

So a failed alert is indistinguishable from a delivered one, and because the date is already
written, the same source is not alerted again for the rest of the day.

This matters because it has already happened. Until 2026-08-18 `TELEGRAM_ADMIN_CHAT_ID` pointed
at an account that had never started a conversation with the bot, so every alert returned
`Forbidden: bot can't initiate conversation with a user`. Had a source degraded in that window,
the database would have shown `last_alerted_on` set and nobody would have been told.

The existing test cannot catch this: `test_send_admin_alert_and_source_failure_trigger` mocks a
`200 OK` and asserts `route.call_count == 1`. It pins that a send was *attempted*, never that it
*arrived*.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/digest/publish.py` | report whether Telegram accepted the alert | modify `send_admin_alert` |
| `apps/digest/tasks.py` | record the alert only when it was delivered | modify `_alert_once_per_day` |
| `tests/test_publish.py` | pin the failure path | add one test |

### Context an engineer new to this repo needs

`_record_failure(source, exc)` increments `consecutive_failures`, and at
`settings.SOURCE_DEGRADED_AFTER` (3) sets `is_degraded` and calls `_alert_once_per_day(source)`.
It then saves with an explicit `update_fields` list that already includes `last_alerted_on`, so
leaving that field unset simply saves `None` — no extra save is needed.

`send_admin_alert` has three early returns before it ever reaches HTTP: no admin chat configured,
no bot token configured, and the kill switch is *not* one of them — alerts are sent even when
publishing is disabled, deliberately, because an operator still needs to hear about failures.

---

## Task 1: Record the alert only when Telegram accepted it

**Files:**
- Modify: `apps/digest/publish.py` — `send_admin_alert`, near line 215
- Modify: `apps/digest/tasks.py` — `_alert_once_per_day`, near line 154
- Test: `tests/test_publish.py`

**Interfaces:**
- Consumes: nothing from other tasks
- Produces: `send_admin_alert(text: str, client: httpx.Client | None = None) -> bool`, returning
  `True` only when Telegram answered `200`. It has three existing callers:
  `apps/digest/tasks.py:173` inside `_alert_once_per_day`, which Step 4 changes;
  `apps/digest/tasks.py:364` in the `compose_and_publish` failure branch; and
  `apps/digest/publish.py:376` in the partial-publish branch. The last two ignore the return
  value and stay exactly as they are — a plain call to a function that now returns `bool` is
  still valid

- [ ] **Step 1: Write the failing test**

Append to `tests/test_publish.py`:

```python
@respx.mock
def test_a_failed_alert_is_not_recorded_as_sent(db, settings):
    """A rejected alert must leave `last_alerted_on` unset, so the next failure retries.

    Measured 2026-08-19: the date was written before the send was attempted, so a rejected
    alert looked exactly like a delivered one and suppressed the rest of the day's attempts.
    Until 2026-08-18 the admin chat id pointed at an account that had never started the bot,
    and every alert returned `Forbidden: bot can't initiate conversation with a user`.
    """
    settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF"
    settings.TELEGRAM_ADMIN_CHAT_ID = "999888777"
    base_tg = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"

    from apps.digest import tasks

    route = respx.post(f"{base_tg}/sendMessage").mock(
        return_value=httpx.Response(
            403, json={"ok": False, "description": "Forbidden: bot can't initiate conversation"}
        )
    )

    src = Source.objects.create(
        name="rejected_alert_source",
        connector=Source.Connector.RSS,
        url="https://example.com/dead",
    )

    for _ in range(settings.SOURCE_DEGRADED_AFTER):
        tasks._record_failure(src, Exception("Connection timeout"))

    src.refresh_from_db()
    assert route.call_count == 1, "the alert must still be attempted"
    assert src.is_degraded is True, "degradation does not depend on the alert"
    assert src.enabled is True, "ADR-002: only a human disables a source"
    assert src.last_alerted_on is None, "a rejected alert must not be recorded as sent"
```

This matches the decoration `test_send_admin_alert_and_source_failure_trigger` already uses in
the same file: `@respx.mock` on top, with database access taken as the `db` fixture argument
rather than a marker.

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_publish.py -q -k failed_alert
```

Expected: FAIL on the final assertion — `last_alerted_on` holds today's date because it is
written before the send.

- [ ] **Step 3: Return a delivery result from `send_admin_alert`**

In `apps/digest/publish.py`, change the signature and the three early returns, then the outcome:

```python
def send_admin_alert(text: str, client: httpx.Client | None = None) -> bool:
    """Send an administrative alert. Returns True only if Telegram accepted it.

    The caller needs the answer: `_alert_once_per_day` records the alert as sent, and writing
    that record for a message Telegram rejected suppresses every retry for the rest of the day.
    """
```

Replace each of the two early `return` statements with `return False`:

```python
    if not admin_chat_id:
        log.warning("TELEGRAM_ADMIN_CHAT_ID not configured; alert dropped: %s", text)
        return False
```

```python
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not configured; alert dropped: %s", text)
        return False
```

Then rewrite the send block so it reports the outcome. It currently reads:

```python
    try:
        r = client.post(
            _bot_url("sendMessage"),
            json={
                "chat_id": admin_chat_id,
                "text": f"🚨 <b>News Radar Alert</b>\n\n{text}",
                "parse_mode": "HTML",
            },
        )
        if r.status_code != 200:
            log.warning("Failed to send admin alert: %s %s", r.status_code, r.text)
    except Exception as exc:
        log.error("Exception sending admin alert: %s", exc)
    finally:
        if close_client:
            client.close()
```

Replace it with:

```python
    try:
        r = client.post(
            _bot_url("sendMessage"),
            json={
                "chat_id": admin_chat_id,
                "text": f"🚨 <b>News Radar Alert</b>\n\n{text}",
                "parse_mode": "HTML",
            },
        )
        if r.status_code != 200:
            log.warning("Failed to send admin alert: %s %s", r.status_code, r.text)
            return False
        return True
    except Exception as exc:
        log.error("Exception sending admin alert: %s", exc)
        return False
    finally:
        if close_client:
            client.close()
```

- [ ] **Step 4: Record the date only on success**

In `apps/digest/tasks.py`, `_alert_once_per_day` currently reads:

```python
def _alert_once_per_day(source) -> None:
    """Rate-limited so a permanently broken source does not flood the admin chat."""
    today = timezone.localdate()
    if source.last_alerted_on == today:
        return
    source.last_alerted_on = today
    msg = (
        f"Source <b>{source.name}</b> is degraded ({source.consecutive_failures} "
        f"consecutive failures).\nLast error: <code>{source.last_error}</code>"
    )
    log.error(
        "SOURCE DEGRADED: %s — %s consecutive failures — %s",
        source.name,
        source.consecutive_failures,
        source.last_error,
    )
    try:
        from . import publish

        publish.send_admin_alert(msg)
    except Exception as exc:
        log.warning("Could not dispatch admin alert for %s: %s", source.name, exc)
```

Replace it with:

```python
def _alert_once_per_day(source) -> None:
    """Rate-limited so a permanently broken source does not flood the admin chat.

    `last_alerted_on` is written only after Telegram accepts the message. Writing it first
    makes a rejected alert indistinguishable from a delivered one and suppresses every retry
    for the rest of the day — which is exactly what happened while the admin chat id pointed
    at an account that had never started the bot.

    The log line above the send is unconditional on purpose: it is the record that survives
    when delivery does not.
    """
    today = timezone.localdate()
    if source.last_alerted_on == today:
        return
    msg = (
        f"Source <b>{source.name}</b> is degraded ({source.consecutive_failures} "
        f"consecutive failures).\nLast error: <code>{source.last_error}</code>"
    )
    log.error(
        "SOURCE DEGRADED: %s — %s consecutive failures — %s",
        source.name,
        source.consecutive_failures,
        source.last_error,
    )
    try:
        from . import publish

        if publish.send_admin_alert(msg):
            source.last_alerted_on = today
        else:
            log.warning("Admin alert for %s was not delivered; will retry", source.name)
    except Exception as exc:
        log.warning("Could not dispatch admin alert for %s: %s", source.name, exc)
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
uv run pytest tests/test_publish.py -q -k failed_alert
```

Expected: PASS

- [ ] **Step 6: Confirm the happy path and the rate limit still hold**

```bash
uv run pytest tests/test_publish.py -q -k "admin_alert or failed_alert"
```

Expected: both pass. `test_send_admin_alert_and_source_failure_trigger` mocks a `200` and asserts
one call; it must stay green, because a delivered alert is still recorded and still rate-limited.

- [ ] **Step 7: Remove the duplicated section comment**

`apps/digest/tasks.py` carries this line twice, near lines 178 and 180:

```python
# --- LLM Tasks (on 'llm' queue) ---------------------------------------------
```

Delete the second occurrence, leaving one.

- [ ] **Step 8: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `175 passed` and `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add apps/digest/publish.py apps/digest/tasks.py tests/test_publish.py
git commit -m "Record a source alert only when Telegram accepted it"
```

- [ ] **Step 10: Rebuild, because this is code**

```bash
docker compose build
docker compose up -d
```

`docker compose up -d` alone recreates containers from the existing image and would leave this
change out of the running system. Confirm afterwards:

```bash
docker compose exec -T worker-fetch uv run python -c "
import os, django, inspect; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.digest.publish import send_admin_alert
print('returns bool:', inspect.signature(send_admin_alert).return_annotation)
"
```

Expected: `returns bool: <class 'bool'>`

---

## Not in this plan

**The kill switch does not gate alerts, and must not start.** An operator needs to hear about a
broken source whether or not publishing is enabled.

**No retry loop.** The alert retries naturally: the source keeps failing, `_record_failure` runs
on the next fetch, and `last_alerted_on` is still unset. Adding a retry inside the alert would
duplicate a mechanism the schedule already provides.

**ADR-002 is untouched.** The verification run on 2026-08-19 confirmed the policy behaves as
written — degrade at three, never disable — and this plan changes none of it.

---

## Self-review

**Coverage**

| Defect | Step |
|---|---|
| `last_alerted_on` written before the attempt | Step 4 |
| `send_admin_alert` gives the caller no success signal | Step 3 |
| A non-200 is logged and then indistinguishable from success | Step 3 |
| No test covers a rejected alert | Step 1 |
| Duplicated section comment | Step 7 |

**Placeholder scan:** none. Every step carries its code or command and its expected output.

**Type consistency:** `send_admin_alert` returns `bool` after Step 3 and is read as a truth value
in Step 4. The other two callers — `tasks.py:364` and `publish.py:376` — ignore the return value
and need no change.

**One note for the reviewer.** Step 1's test asserts `route.call_count == 1` *and*
`last_alerted_on is None`. Both matter: the first says the alert was still attempted, the second
says the attempt was not mistaken for a success. Dropping either half would let a fix pass that
simply stops alerting.
