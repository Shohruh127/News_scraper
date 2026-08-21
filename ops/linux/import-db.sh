#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

[ "$#" -eq 1 ] || fail "Usage: $0 /absolute/path/to/news_radar.dump"
dump_path=$1
case "$dump_path" in
    /*) ;;
    *) fail "Dump path must be absolute." ;;
esac
[ -s "$dump_path" ] || fail "Dump file is missing or empty: $dump_path"

compose up -d postgres redis
container_id=$(postgres_container_id)
container_path="/tmp/news_radar_import.dump"

cleanup() {
    compose exec -T postgres rm -f "$container_path" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker cp "$dump_path" "$container_id:$container_path"
compose exec -T postgres pg_restore --list "$container_path" >/dev/null

public_tables=$(compose exec -T postgres sh -ec \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -c "SELECT count(*) FROM pg_tables WHERE schemaname = '\''public'\'';"')
[ "$public_tables" = "0" ] || fail "Target database is not empty; refusing to overwrite it."

compose exec -T postgres sh -ec \
    'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges "$1"' \
    -- "$container_path"
compose run --rm web python manage.py migrate --noinput

printf 'Database import and migrations succeeded.\n'
