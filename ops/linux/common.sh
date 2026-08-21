#!/bin/sh
set -eu

# Default to the checkout this script lives in. A fixed path is wrong on any host that
# put the project somewhere else, and it fails every ops script at once when it is.
PROJECT_DIR=${NEWS_RADAR_PROJECT_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)}
BACKUP_DIR=${NEWS_RADAR_BACKUP_DIR:-/var/backups/news-radar/db}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[ -d "$PROJECT_DIR" ] || fail "Project directory not found: $PROJECT_DIR"
cd "$PROJECT_DIR"
[ -f .env ] || fail "Missing production environment file: $PROJECT_DIR/.env"

compose() {
    docker compose "$@"
}

postgres_container_id() {
    container_id=$(compose ps -q postgres)
    [ -n "$container_id" ] || fail "PostgreSQL container is not running."
    printf '%s' "$container_id"
}

validate_backup_dir() {
    case "$BACKUP_DIR" in
        /*) ;;
        *) fail "Backup directory must be absolute: $BACKUP_DIR" ;;
    esac
    [ "$BACKUP_DIR" != "/" ] || fail "Backup directory must not be filesystem root."
}
