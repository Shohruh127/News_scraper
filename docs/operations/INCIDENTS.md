# News Radar: Incident Response and Post-Mortem Guide

## 1. Severity Classifications

| Severity | Definition | Examples | SLA |
| :--- | :--- | :--- | :--- |
| **SEV-1 (Critical)** | Duplicate posts to public channel, data corruption, total system outage during evening publication (18:00–19:00). | Double post sent; database dump corrupted; bot leaking credentials. | Immediate (< 15 min) |
| **SEV-2 (High)** | Digest publishing stalled or in `unknown` delivery state; worker process crashed; Ollama down. | Delivery timeout alert; LLM stage timeout; worker OOM. | < 1 hour |
| **SEV-3 (Medium)** | Non-critical source degraded; single RSS feed 404/500; watchdog restart triggered. | Substack source 500; rate limit on single blog. | < 24 hours |
| **SEV-4 (Low)** | Cosmetic formatting flaw; missing image fallback; minor log warning. | Single character typo; missing lead anchor verb. | Next release |

---

## 2. Emergency Incident Checklist

### Step 1: Containment
1. If runaway publishing or duplicate loop is suspected, immediately trip the publishing kill switch:
   ```bash
   # In .env:
   PUBLISHING_ENABLED=false
   # Apply:
   docker compose up -d worker-publish
   ```
2. Check current delivery states in Django admin (`/admin/digest/digestitem/`) or run:
   ```bash
   uv run python manage.py runtime_health
   ```

### Step 2: Investigation & Reconciliation
1. Check the Telegram admin chat for the exact error message and stack trace.
2. If Telegram delivery was ambiguous (`unknown` state):
   - Never auto-retry. Check the public channel manually.
   - Use `python manage.py reconcile_delivery` to record the message ID or safely reset to pending.

### Step 3: Resolution & Verification
1. Re-run `python manage.py runtime_health --strict` to ensure all workers and services are green.
2. Verify table counts and database consistency.

---

## 3. Post-Mortem Template

When a SEV-1 or SEV-2 incident occurs, complete the following log entry below:

```markdown
### Incident: [YYYY-MM-DD] [Brief Title]
- **Date & Time:** YYYY-MM-DD HH:MM UTC+5
- **Severity:** SEV-1 / SEV-2
- **Lead Responder:** [Name]
- **Summary:** Brief description of what happened and the impact.
- **Root Cause:** Detailed explanation of why the failure occurred.
- **Trigger:** What specific event initiated the issue.
- **Resolution:** Actions taken to mitigate and resolve the incident.
- **Lessons Learned:** What went well, what went wrong, and where we got lucky.
- **Preventative Action Items:**
  - [ ] Task 1 (Owner: ...)
  - [ ] Task 2 (Owner: ...)
```
