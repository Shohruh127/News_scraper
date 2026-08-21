#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

expected="postgres redis worker-fetch worker-llm worker-publish bot beat web"
for service in $expected; do
    container_id=$(compose ps -q "$service")
    [ -n "$container_id" ] || fail "Missing container for service: $service"
    running=$(docker inspect --format '{{.State.Running}}' "$container_id")
    [ "$running" = "true" ] || fail "Service is not running: $service"
done

curl --fail --silent --show-error http://127.0.0.1:8000/healthz/ >/dev/null
curl --fail --silent --show-error http://127.0.0.1:8000/readyz/ >/dev/null
compose exec -T web python manage.py runtime_health --strict

printf 'All News Radar health checks passed.\n'
