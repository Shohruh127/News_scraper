#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

SKIP_BACKUP=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-backup) SKIP_BACKUP=true ;;
        # Obsolete since preflight left the deploy (2026-08-28). Accepted and ignored so
        # the command operators have in their chat history keeps deploying.
        --allow-publishing) printf 'note: --allow-publishing is obsolete; ignored.\n' ;;
        *) fail "Usage: $0 [--skip-backup]" ;;
    esac
    shift
done

[ -z "$(git status --porcelain)" ] || fail "Deployment checkout is dirty."
compose config --quiet

if [ "$SKIP_BACKUP" = false ] && [ -n "$(compose ps -q postgres)" ]; then
    "$(dirname "$0")/backup.sh"
fi

compose build
# Preflight left the deploy path on 2026-08-28 by the operator's decision: the release
# stays lean, and a configuration error now surfaces at runtime instead of blocking the
# deploy. preflight.sh remains a hand tool, run after a build:
#   docker compose build && sh ops/linux/preflight.sh
compose up -d postgres redis
compose up -d

attempt=1
while [ "$attempt" -le 12 ]; do
    if curl --fail --silent http://127.0.0.1:8000/healthz/ >/dev/null 2>&1 &&
       curl --fail --silent http://127.0.0.1:8000/readyz/ >/dev/null 2>&1; then
        # Both of these were manual steps, and a manual step in a deploy is a step that
        # eventually does not happen. They run after the health gate because they need
        # `web` answering, and both are idempotent, so a re-run costs nothing.
        #
        # seed_sources adds sources new to the code and updates what the code owns. It
        # will not re-enable a source the operator switched off; see OPERATOR_OWNED there.
        compose exec -T web python manage.py seed_sources
        # prune_schedule deletes PeriodicTask rows for beat entries the code no longer
        # has. It reads config/celery.py, not the database, so beat having already
        # written its own rows does not change the answer. beat then re-reads a clean
        # schedule — without the restart the deleted entries stay in its memory until
        # its next sync.
        compose exec -T web python manage.py prune_schedule
        compose restart beat
        compose exec -T web python manage.py runtime_health --json || true
        printf 'Deployment succeeded at commit %s\n' "$(git rev-parse HEAD)"
        exit 0
    fi
    sleep 5
    attempt=$((attempt + 1))
done

compose ps
fail "Deployment health check timed out."