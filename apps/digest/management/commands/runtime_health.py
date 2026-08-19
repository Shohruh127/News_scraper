"""Management command to check News Radar runtime health.

Used by container healthchecks and the Windows host watchdog.
Supports --strict mode and structured JSON output.
"""

import json
import sys

from django.core.management.base import BaseCommand

from apps.digest import health


class Command(BaseCommand):
    help = "Check runtime health of News Radar (DB, Redis, workers, bot, pipeline)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Fail if any worker or bot heartbeat is missing or stale",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output structured JSON instead of key=value",
        )

    def handle(self, *args, **options):
        strict = options["strict"]
        as_json = options["json"]

        is_healthy, data = health.check_runtime_health(strict=strict)

        if as_json:
            self.stdout.write(json.dumps(data, indent=2))
        else:
            self.stdout.write(f"status={data['status']}")
            self.stdout.write(f"database={data['readiness']['database']['ok']}")
            self.stdout.write(f"redis={data['readiness']['redis']['ok']}")
            self.stdout.write(f"migrations_ok={data['readiness']['migrations']['ok']}")
            if data["readiness"]["migrations"]["unapplied"]:
                self.stdout.write(
                    f"unapplied_migrations={','.join(data['readiness']['migrations']['unapplied'])}"
                )

            self.stdout.write("--- heartbeats ---")
            for svc, h in data["heartbeats"].items():
                status_str = h.get("status", "unknown")
                age = h.get("age_seconds")
                age_str = f" ({age}s ago)" if age is not None else ""
                self.stdout.write(f"service:{svc}={status_str}{age_str}")

            if data["degraded_services"]:
                self.stdout.write(f"degraded_services={','.join(data['degraded_services'])}")

            pipeline = data.get("pipeline_last_run", {})
            if pipeline.get("status") != "none_recorded":
                self.stdout.write(
                    f"pipeline_last_run={pipeline.get('completed_at', 'none')} "
                    f"(status={pipeline.get('status')})"
                )

        if not is_healthy:
            sys.exit(1)
