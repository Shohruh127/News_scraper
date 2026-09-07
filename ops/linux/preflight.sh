#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

ALLOW_PUBLISHING=false
if [ "${1:-}" = "--allow-publishing" ]; then
    ALLOW_PUBLISHING=true
elif [ "$#" -gt 0 ]; then
    fail "Usage: $0 [--allow-publishing]"
fi

compose config --quiet

compose run --rm --no-deps -e NEWS_RADAR_ALLOW_PUBLISHING="$ALLOW_PUBLISHING" \
    web python - <<'PY'
import os

import django
from django.conf import settings

# `python -` is not manage.py: nothing has pointed Django at a settings module or run
# setup(), so the first settings access raises ImproperlyConfigured. `settings` itself is
# a lazy proxy, so importing it above is fine; only attribute access needs this.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

errors = []
if settings.DEBUG:
    errors.append("DJANGO_DEBUG must be false")
if settings.SECRET_KEY == "dev-only-not-for-production" or len(settings.SECRET_KEY) < 40:
    errors.append("DJANGO_SECRET_KEY must be a fresh value with at least 40 characters")
if not settings.ALLOWED_HOSTS:
    errors.append("DJANGO_ALLOWED_HOSTS must not be empty")
if settings.PUBLISHING_ENABLED:
    if os.environ.get("NEWS_RADAR_ALLOW_PUBLISHING") == "true":
        print("WARNING: kill switch is on and --allow-publishing was passed.")
    else:
        errors.append(
            "PUBLISHING_ENABLED must be false during deployment "
            "(pass --allow-publishing to accept the risk)"
        )
known = {"gateway", "mimo", "gemini"}
providers = {
    "LLM_PROVIDER": settings.LLM_PROVIDER,
    "EDITORIAL_UZ_PROVIDER": settings.EDITORIAL_UZ_PROVIDER,
    "CLASSIFIER_PROVIDER": settings.CLASSIFIER_PROVIDER,
}
for name, value in providers.items():
    if value not in known:
        errors.append(f"{name}={value!r} must be one of {sorted(known)}")

in_use = set(providers.values())
if "gateway" in in_use and not (settings.GATEWAY_BASE_URL and settings.GATEWAY_TOKEN):
    errors.append("a stage runs on the gateway but GATEWAY_BASE_URL/GATEWAY_TOKEN are unset")
if "mimo" in in_use and not (settings.MIMO_BASE_URL and settings.MIMO_API_KEY):
    errors.append("a stage runs on mimo but MIMO_BASE_URL/MIMO_API_KEY are unset")
if "gemini" in in_use and not (settings.GEMINI_BASE_URL and settings.GEMINI_API_KEY):
    errors.append("a stage runs on gemini but GEMINI_BASE_URL/GEMINI_API_KEY are unset")

# The gateway addresses models by tier alias only and answers 404 for a real model name,
# so an empty alias fails every call in that stage rather than falling back to anything.
if "gateway" in in_use:
    for name in ("GATEWAY_FAST_MODEL", "GATEWAY_SMART_MODEL"):
        if not getattr(settings, name):
            errors.append(f"{name} is required: it names the tier the gateway is asked for")

if not settings.TELEGRAM_BOT_TOKEN:
    errors.append("TELEGRAM_BOT_TOKEN is required")
database_host = settings.DATABASES["default"]["HOST"]
if database_host != "postgres":
    errors.append("DATABASE_URL must use Docker Compose host 'postgres'")

if errors:
    raise SystemExit("Production preflight failed:\n- " + "\n- ".join(errors))
print("Django production settings preflight passed.")
PY

leaks=$(compose run --rm --no-deps web sh -ec \
    "find /app -type f \( -name '.env' -o -name '*.dump' -o -name '*.backup' \) -print")
[ -z "$leaks" ] || fail "Sensitive files found in runtime image:\n$leaks"

printf 'Production preflight passed.\n'