#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

RETENTION_DAYS=${NEWS_RADAR_BACKUP_RETENTION_DAYS:-14}
case "$RETENTION_DAYS" in
    ''|*[!0-9]*) fail "Retention days must be a non-negative integer." ;;
esac

validate_backup_dir
umask 077
mkdir -p "$BACKUP_DIR"

timestamp=$(date -u +%Y-%m-%d_%H%M%S)
filename="backup_$timestamp.dump"
final_path="$BACKUP_DIR/$filename"
partial_path="$final_path.partial"
container_path="/tmp/news_radar_backup.dump"
container_id=$(postgres_container_id)

cleanup() {
    compose exec -T postgres rm -f "$container_path" >/dev/null 2>&1 || true
    rm -f "$partial_path"
}
trap cleanup EXIT HUP INT TERM

compose exec -T postgres sh -ec \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/news_radar_backup.dump'
compose exec -T postgres pg_restore --list "$container_path" >/dev/null

docker cp "$container_id:$container_path" "$partial_path"
[ -s "$partial_path" ] || fail "Backup is empty: $partial_path"
mv "$partial_path" "$final_path"

(
    cd "$BACKUP_DIR"
    sha256sum "$filename" > "$filename.sha256"
)
chmod 600 "$final_path" "$final_path.sha256"

find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'backup_*.dump' -o -name 'backup_*.dump.sha256' \) \
    -mtime "+$RETENTION_DAYS" -delete

printf 'Backup created and validated: %s\n' "$final_path"
