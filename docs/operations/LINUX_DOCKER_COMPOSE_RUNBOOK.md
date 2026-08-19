# Linux Docker Compose runbook

Production baseline: Ubuntu 24.04 LTS amd64, Docker Engine with the Compose plugin,
project checkout at `/opt/news-radar`, PostgreSQL and Redis in Compose, and private
admin access through an SSH tunnel.

## Safety rules

- Deploy only an audited Git commit or release tag from a clean checkout.
- Never copy `.env`, PostgreSQL dumps, private keys, or tokens into Git or an image.
- Keep `PUBLISHING_ENABLED=false` through import, migration, and dry-run validation.
- Store backups under `/var/backups/news-radar/db`, outside the repository.
- Do not use `backup_2026-08-19_task0.dump`; it is a corrupted UTF-16 archive.

## One-time host setup

Install Docker Engine and the Docker Compose plugin from Docker's supported Ubuntu
packages. Create the checkout and backup directories:

```sh
sudo install -d -m 755 /opt/news-radar
sudo install -d -m 700 /var/backups/news-radar/db
```

Clone the audited release into `/opt/news-radar`. Copy `.env.example` to `.env`,
fill every required value on the server, and restrict it:

```sh
sudo chmod 600 /opt/news-radar/.env
```

Required production values include a fresh `DJANGO_SECRET_KEY`,
`DJANGO_DEBUG=false`, PostgreSQL credentials, a container-network
`DATABASE_URL` using host `postgres`, Ollama model tags and endpoint, and Telegram
credentials. Leave all feature/publishing gates false initially.

## Import the existing database

Create and validate a fresh custom-format dump on the current machine. Transfer only
that validated dump to the server, then run:

```sh
cd /opt/news-radar
sudo ops/linux/import-db.sh /absolute/path/to/news_radar.dump
```

The importer validates the archive and refuses to write into a non-empty target
database. It runs current Django migrations after restore.

## First deployment

```sh
cd /opt/news-radar
sudo chmod 755 ops/linux/*.sh
sudo ops/linux/deploy.sh --skip-backup
sudo ops/linux/install-systemd.shsudo systemctl start news-radar
```

For later releases, omit `--skip-backup`. The deploy script refuses a dirty checkout,
takes a pre-deploy backup, builds the runtime image, migrates, starts the stack, and
waits for health endpoints.

## Private admin access

Keep port 8000 bound to loopback. From the operator's computer:

```sh
ssh -L 8000:127.0.0.1:8000 news-radar
```

Open `http://127.0.0.1:8000/admin/` locally. PostgreSQL and Redis must not be exposed
to the public internet.

## Acceptance checks

```sh
cd /opt/news-radar
sudo docker compose ps
sudo ops/linux/health-check.sh
sudo ops/linux/backup.sh
sudo ops/linux/restore-drill.sh
sudo systemctl list-timers 'news-radar-*'
```

Also verify that the restored database row counts match the source, the runtime image
contains no `.env` or `*.dump`, and the stack returns after a controlled reboot.

After dry-run verification, perform one controlled Telegram publish. Enable
`PUBLISHING_ENABLED=true` only after that owner-visible result is accepted.

## Rollback

Record the deployed commit before every release. For a backward-compatible code
rollback, check out the previous audited tag and run `ops/linux/deploy.sh`. Never
reverse a schema migration or overwrite the production database automatically.
For a data rollback, stop writers, preserve the failed-state database, and restore a
validated backup according to the incident plan.
