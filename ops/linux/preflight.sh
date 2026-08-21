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
known = {"ollama", "gateway", "mimo"}
providers = {
    "LLM_PROVIDER": settings.LLM_PROVIDER,
    "EDITORIAL_EN_PROVIDER": settings.EDITORIAL_EN_PROVIDER,
    "TRANSLATION_PROVIDER": settings.TRANSLATION_PROVIDER,
    "CLASSIFIER_PROVIDER": settings.CLASSIFIER_PROVIDER,
}
for name, value in providers.items():
    if value not in known:
        errors.append(f"{name}={value!r} must be one of {sorted(known)}")

in_use = set(providers.values())
if "ollama" in in_use and not settings.OLLAMA_BASE_URL:
    errors.append("a stage runs on ollama but OLLAMA_BASE_URL is unset")
if "gateway" in in_use and not (settings.GATEWAY_BASE_URL and settings.GATEWAY_TOKEN):
    errors.append("a stage runs on the gateway but GATEWAY_BASE_URL/GATEWAY_TOKEN are unset")
if "mimo" in in_use and not (settings.MIMO_BASE_URL and settings.MIMO_API_KEY):
    errors.append("a stage runs on mimo but MIMO_BASE_URL/MIMO_API_KEY are unset")

# The Ollama tags name the fast and deep tiers for every provider: the gateway branch
# reads them to choose between its fast and smart aliases. So they must be set even when
# nothing talks to Ollama. Pinning is only enforced where a floating tag can actually
# move under us, which is Ollama itself; gateway aliases are pinned on the gateway side.
for name in ("OLLAMA_FAST_MODEL", "OLLAMA_DEEP_MODEL"):
    value = getattr(settings, name)
    if not value:
        errors.append(f"{name} is required: it names a tier, not just an Ollama model")
    elif "ollama" in in_use and value.endswith(":latest"):
        # A warning, not an error. Drift is already detected where it matters: every
        # classification records Analysis.model_digest, so a repointed tag shows up in the
        # data. Meanwhile gemma4:latest is the tag ADR-004 measured as correct for
        # translation, so refusing it here blocked the configuration the project chose.
        print(f"WARNING: {name}={value!r} is a floating tag; rely on Analysis.model_digest.")
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