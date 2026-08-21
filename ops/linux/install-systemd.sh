#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || {
    printf 'Run as root: sudo %s\n' "$0" >&2
    exit 1
}

# Default to the checkout this script lives in. A fixed path is wrong on any host that
# put the project somewhere else, and it fails every ops script at once when it is.
PROJECT_DIR=${NEWS_RADAR_PROJECT_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)}
[ -d "$PROJECT_DIR/ops/linux/systemd" ] || {
    printf 'Missing systemd units under %s\n' "$PROJECT_DIR" >&2
    exit 1
}

install -d -m 700 /var/backups/news-radar/db
# The unit files carry /opt/news-radar as a placeholder. Substitute the real checkout on
# the way in, otherwise a host with the project elsewhere gets units that silently start
# nothing after a reboot.
for unit in "$PROJECT_DIR"/ops/linux/systemd/*.service "$PROJECT_DIR"/ops/linux/systemd/*.timer; do
    target=/etc/systemd/system/$(basename "$unit")
    sed "s|/opt/news-radar|$PROJECT_DIR|g" "$unit" > "$target"
    chmod 644 "$target"
done
chmod 755 "$PROJECT_DIR"/ops/linux/*.sh

systemctl daemon-reload
systemctl enable news-radar.service
systemctl enable --now news-radar-backup.timer
systemctl enable --now news-radar-health.timer
systemctl enable --now news-radar-restore-drill.timer

printf 'Systemd units installed. Start the app with: systemctl start news-radar\n'
