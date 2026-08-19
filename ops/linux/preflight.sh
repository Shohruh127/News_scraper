#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

compose config --quiet

compose run --rm --no-deps web python - <<'PY'
from django.conf import settings

errors = []
if settings.DEBUG:
    errors.append("DJANGO_DEBUG must be false")
if settings.SECRET_KEY == "dev-only-not-for-production" or len(settings.SECRET_KEY) < 40:
    errors.append("DJANGO_SECRET_KEY must be a fresh value with at least 40 characters")
if not settings.ALLOWED_HOSTS:
    errors.append("DJANGO_ALLOWED_HOSTS must not be empty")
if settings.PUBLISHING_ENABLED:
    errors.append("PUBLISHING_ENABLED must be false during deployment")
providers = {
    "LLM_PROVIDER": settings.LLM_PROVIDER,
    "EDITORIAL_EN_PROVIDER": settings.EDITORIAL_EN_PROVIDER,
    "TRANSLATION_PROVIDER": settings.TRANSLATION_PROVIDER,
}
for name, value in providers.items():
    if value != "ollama":
        errors.append(f"{name} must be ollama for production")
if not settings.OLLAMA_BASE_URL:
    errors.append("OLLAMA_BASE_URL is required")
for name in ("OLLAMA_FAST_MODEL", "OLLAMA_DEEP_MODEL"):
    value = getattr(settings, name)
    if not value or value.endswith(":latest"):
        errors.append(f"{name} must use an explicit pinned model tag")
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