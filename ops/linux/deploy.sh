#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

SKIP_BACKUP=false
if [ "${1:-}" = "--skip-backup" ]; then
    SKIP_BACKUP=true
elif [ "$#" -gt 0 ]; then
    fail "Usage: $0 [--skip-backup]"
fi

[ -z "$(git status --porcelain)" ] || fail "Deployment checkout is dirty."
compose config --quiet

if [ "$SKIP_BACKUP" = false ] && [ -n "$(compose ps -q postgres)" ]; then
    "$(dirname "$0")/backup.sh"
fi

compose build
"$(dirname "$0")/preflight.sh"
compose up -d postgres redis
compose run --rm web python manage.py migrate --noinput
compose up -d

attempt=1
while [ "$attempt" -le 12 ]; do
    if curl --fail --silent http://127.0.0.1:8000/healthz/ >/dev/null 2>&1 &&
       curl --fail --silent http://127.0.0.1:8000/readyz/ >/dev/null 2>&1; then
        compose exec -T web python manage.py runtime_health --json || true
        printf 'Deployment succeeded at commit %s\n' "$(git rev-parse HEAD)"
        exit 0
    fi
    sleep 5
    attempt=$((attempt + 1))
done

compose ps
fail "Deployment health check timed out."