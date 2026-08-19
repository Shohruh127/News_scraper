#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || {
    printf 'Run as root: sudo %s\n' "$0" >&2
    exit 1
}

PROJECT_DIR=${NEWS_RADAR_PROJECT_DIR:-/opt/news-radar}
[ -d "$PROJECT_DIR/ops/linux/systemd" ] || {
    printf 'Missing systemd units under %s\n' "$PROJECT_DIR" >&2
    exit 1
}

install -d -m 700 /var/backups/news-radar/db
install -m 644 "$PROJECT_DIR"/ops/linux/systemd/*.service /etc/systemd/system/
install -m 644 "$PROJECT_DIR"/ops/linux/systemd/*.timer /etc/systemd/system/
chmod 755 "$PROJECT_DIR"/ops/linux/*.sh

systemctl daemon-reload
systemctl enable news-radar.service
systemctl enable --now news-radar-backup.timer
systemctl enable --now news-radar-health.timer
systemctl enable --now news-radar-restore-drill.timer

printf 'Systemd units installed. Start the app with: systemctl start news-radar\n'
