# ADR-002 — Alert on source failure, never auto-disable

Date: 2026-08-14
Status: **Accepted**
Decided by: project owner

## Context

Sources break. RSS feeds move, HTML layouts change, sites add paywalls or JavaScript
rendering, APIs rate-limit. The system fetches from 8 sources in M1 and 25–40 in M2,
so breakage is routine rather than exceptional.

The plan needed a policy for what happens after repeated failures. Two options were
considered:

- **Auto-disable** after N consecutive failures: reduces alert noise, but a source can
  fall silent after a transient outage and stay silent indefinitely.
- **Alert only**: the system keeps retrying and keeps complaining until a human acts.

## Decision

**Alert, never auto-disable.** A source is disabled only by a human, through Django admin.

Escalation ladder:

| Consecutive failures | Action |
|---|---|
| 1–2 | Log only. Transient failures are normal |
| 3 | Mark `degraded`, send one alert to the admin Telegram chat |
| 7+ | Escalate the alert; **keep fetching**; source stays enabled |

The alert is rate-limited so a permanently broken source produces one message per day,
not one per run.

## Reasons

A silently disabled source is the worst failure mode this system has. The digest keeps
publishing, nothing looks wrong, and a primary source has quietly vanished from
coverage. Nobody notices for months.

A noisy alert is a much cheaper failure than a silent gap. The escalation ladder plus
daily rate-limiting keeps the noise bounded without ever hiding the problem.

This also matches how breakage actually resolves: most "failures" are transient
(server hiccup, temporary rate limit) and fix themselves. Auto-disable would turn a
two-hour outage into a permanent one.

## Consequences

- `sources.consecutive_failures` is a counter and an alert trigger, never a kill switch.
- Django admin must expose `consecutive_failures`, `last_fetched_at`, and a `degraded`
  filter so review is a two-click operation (ADR-001, reason 3).
- Alerts must be deduplicated per source per day, or a broken source floods the chat.
- A degraded source must never abort the ingestion run for the others.
