#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

DRILL_DB=${NEWS_RADAR_DRILL_DB:-news_radar_restore_drill}
case "$DRILL_DB" in
    *[!a-zA-Z0-9_]*) fail "Invalid drill database name." ;;
esac

validate_backup_dir
latest=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'backup_*.dump' -printf '%T@ %p\n' |
    sort -nr | sed -n '1s/^[^ ]* //p')
[ -n "$latest" ] || fail "No database backup found in $BACKUP_DIR."
checksum="$latest.sha256"
[ -f "$checksum" ] || fail "Missing checksum: $checksum"
(
    cd "$BACKUP_DIR"
    sha256sum -c "$(basename "$checksum")"
)

container_id=$(postgres_container_id)
container_path="/tmp/news_radar_restore_drill.dump"
source_db=$(compose exec -T postgres sh -ec 'printf "%s" "$POSTGRES_DB"')

cleanup() {
    compose exec -T postgres sh -ec \
        'dropdb -U "$POSTGRES_USER" --if-exists --force "$1"' -- "$DRILL_DB" \
        >/dev/null 2>&1 || true
    compose exec -T postgres rm -f "$container_path" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker cp "$latest" "$container_id:$container_path"
compose exec -T postgres pg_restore --list "$container_path" >/dev/null
compose exec -T postgres sh -ec \
    'dropdb -U "$POSTGRES_USER" --if-exists --force "$1"' -- "$DRILL_DB" \
    >/dev/null 2>&1 || true

compose exec -T postgres sh -ec 'createdb -U "$POSTGRES_USER" "$1"' -- "$DRILL_DB"
compose exec -T postgres sh -ec \
    'pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --no-privileges "$2"' \
    -- "$DRILL_DB" "$container_path"

table_count() {
    database=$1
    table=$2
    compose exec -T postgres sh -ec \
        'psql -U "$POSTGRES_USER" -d "$1" -t -A -c "$2"' \
        -- "$database" "SELECT count(*) FROM $table;"
}

for table in digest_article digest_digest digest_digestitem digest_source; do
    source_count=$(table_count "$source_db" "$table")
    restored_count=$(table_count "$DRILL_DB" "$table")
    printf '%s source=%s restored=%s\n' "$table" "$source_count" "$restored_count"
    [ "$source_count" = "$restored_count" ] || fail "Row-count mismatch for $table."
done

printf 'Restore drill succeeded: %s\n' "$latest"