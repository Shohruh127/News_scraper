"""Rewrite a .env file into commented blocks without touching any value.

Run it on whichever machine owns the file:

    python3 ops/format-env.py            # formats ./.env
    python3 ops/format-env.py --check    # report only, write nothing
    python3 ops/format-env.py /path/.env

Keys are grouped and commented; values are copied verbatim. Anything this file does not
know about is kept under "Other" rather than dropped, and the result is verified key by
key before it replaces the original — on any mismatch the backup is restored.
"""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

#: Keys added when absent. Nothing else is ever invented.
NEW_DEFAULTS = {
    "CLASSIFIER_PROVIDER": "ollama",
    "GATEWAY_BASE_URL": "",
    "GATEWAY_TOKEN": "",
    "GATEWAY_FAST_MODEL": "fast",
    "GATEWAY_SMART_MODEL": "smart",
    "GATEWAY_TIMEOUT": "300",
}

BLOCKS = [
    (
        "Django",
        ["DJANGO_SECRET_KEY", "DJANGO_DEBUG", "DJANGO_ALLOWED_HOSTS"],
        [
            "DJANGO_ALLOWED_HOSTS must name the real host in production: preflight rejects an",
            "empty value, and Django refuses requests for hosts not listed here.",
        ],
    ),
    ("Docker Compose", ["NEWS_RADAR_IMAGE_TAG"], []),
    (
        "PostgreSQL",
        ["POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "DATABASE_URL"],
        [
            "Inside Compose the host is `postgres`. URL-encode reserved characters in the",
            "password component of DATABASE_URL.",
        ],
    ),
    (
        "Redis",
        ["REDIS_URL"],
        ["Celery broker, worker heartbeats, and the pipeline overlap lock."],
    ),
    (
        "Ollama (org LAN)",
        [
            "OLLAMA_BASE_URL",
            "OLLAMA_FAST_MODEL",
            "OLLAMA_DEEP_MODEL",
            "OLLAMA_FAST_TIMEOUT",
            "OLLAMA_DEEP_TIMEOUT",
        ],
        [
            "No trailing slash on the base URL.",
            "The two model tags name the fast and deep tiers for EVERY provider: the gateway",
            "and MiMo branches read them to decide which tier a call belongs to. Keep them",
            "set even when nothing talks to Ollama directly.",
        ],
    ),
    (
        "LLM providers",
        [
            "LLM_PROVIDER",
            "EDITORIAL_EN_PROVIDER",
            "TRANSLATION_PROVIDER",
            "CLASSIFIER_PROVIDER",
        ],
        [
            "Each accepts: ollama | gateway | mimo. Set all four to one value to run the whole",
            "pipeline on one provider; set them separately to mix.",
            "",
            "  LLM_PROVIDER          global default for anything not overridden below",
            "  EDITORIAL_EN_PROVIDER English analysis stage",
            "  TRANSLATION_PROVIDER  Uzbek translation. Measured 2026-08-17: MiMo turned",
            "                        2.4 trillion into 2 trillion here. Ollama is the safe",
            "                        value.",
            "  CLASSIFIER_PROVIDER   triage + classification, several hundred calls a day",
        ],
    ),
    (
        "Internal LLM gateway",
        [
            "GATEWAY_BASE_URL",
            "GATEWAY_TOKEN",
            "GATEWAY_FAST_MODEL",
            "GATEWAY_SMART_MODEL",
            "GATEWAY_TIMEOUT",
        ],
        [
            "OpenAI-compatible front door to the local GPU models. Models are addressed by",
            "tier alias (fast / smart); sending a real model name is a 404 on purpose.",
            "GATEWAY_TIMEOUT covers queue wait (up to 30s) plus generation - keep it >= 300.",
        ],
    ),
    (
        "Xiaomi MiMo",
        [
            "MIMO_BASE_URL",
            "MIMO_API_KEY",
            "MIMO_FAST_MODEL",
            "MIMO_DEEP_MODEL",
            "MIMO_EDITORIAL_MODEL",
            "MIMO_TIMEOUT",
        ],
        [
            "The MiMo Token Plan forbids automated/backend use; this is a testing-phase",
            "arrangement only. Volume matters: editorial is ~10 calls a day, triage is ~265.",
        ],
    ),
    (
        "Telegram",
        [
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHANNEL_ID",
            "TELEGRAM_GROUP_ID",
            "TELEGRAM_ADMIN_CHAT_ID",
            "TELEGRAM_FORWARD_TTL",
            "TELEGRAM_LINK_PREVIEW",
        ],
        [],
    ),
    (
        "Publishing and feature gates",
        [
            "PUBLISHING_ENABLED",
            "POST_FORMAT_V2_ENABLED",
            "POST_MAX_CHARS",
            "POST_MAX_SENTENCES",
            "BENCHMARK_VERIFICATION_ENABLED",
            "ARTIFACT_VERIFICATION_ENABLED",
            "ARTIFACT_TIMEOUT",
        ],
        [
            "PUBLISHING_ENABLED is the kill switch: false composes and stores but sends",
            "nothing. preflight.sh refuses to deploy while it is true unless you pass",
            "--allow-publishing.",
        ],
    ),
    (
        "Digest selection",
        [
            "DIGEST_MAX_ITEMS",
            "DIGEST_MAX_PER_TOPIC",
            "DIGEST_MAX_PER_SUBJECT",
            "SKIP_PAPER_DOMAINS",
        ],
        [],
    ),
    ("Pipeline", ["EVENING_LOCK_TTL", "TRANSLATION_NUM_PREDICT"], []),
    ("Runtime", ["TIME_ZONE", "LOG_LEVEL"], []),
]

HEADER = [
    "# Local environment. Never commit this file - .gitignore and .dockerignore both",
    "# exclude it, and preflight.sh checks it never reached the image.",
    "",
]


def parse(text):
    """Return {key: raw_value} for assignment lines, ignoring comments and blanks."""
    found = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        found[key.strip()] = value
    return found


def render(values):
    out = list(HEADER)
    placed = set()
    for title, keys, notes in BLOCKS:
        body = [f"{k}={values[k]}" for k in keys if k in values]
        if not body:
            continue
        placed.update(k for k in keys if k in values)
        out.append(f"# --- {title} " + "-" * max(4, 74 - len(title)))
        out.extend(f"# {n}".rstrip() for n in notes)
        out.extend(body)
        out.append("")

    leftover = [k for k in values if k not in placed]
    if leftover:
        out.append("# --- Other " + "-" * 68)
        out.append("# Not known to ops/format-env.py. Kept as-is; move it into a block above.")
        out.extend(f"{k}={values[k]}" for k in leftover)
        out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="Group a .env file into commented blocks.")
    parser.add_argument("path", nargs="?", default=".env")
    parser.add_argument("--check", action="store_true", help="report only, write nothing")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.is_file():
        sys.exit(f"No such file: {path}")

    # open() rather than Path.read_text(newline=...): that keyword only exists on 3.13, and
    # this runs on whatever Python the host happens to have. newline="" keeps CRLF intact,
    # without it Python translates line endings on read and the check below never fires.
    with path.open(encoding="utf-8", newline="") as handle:
        original_text = handle.read()
    original = parse(original_text)
    if not original:
        sys.exit(f"{path} holds no KEY=VALUE lines; refusing to rewrite it.")

    values = dict(original)
    added = [k for k in NEW_DEFAULTS if k not in values]
    for key in added:
        values[key] = NEW_DEFAULTS[key]

    rendered = render(values)

    # Verify before touching anything: every original pair must survive unchanged.
    lost = sorted(k for k, v in original.items() if parse(rendered).get(k) != v)
    if lost:
        sys.exit(f"Refusing to write: these keys would be lost or altered: {lost}")

    if args.check:
        print(f"{path}: {len(original)} keys present, would add {len(added)}: {added}")
        print("Nothing written (--check).")
        return

    backup = path.with_name(f"{path.name}.backup.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)

    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(rendered)
    os.chmod(path, 0o600)

    with path.open(encoding="utf-8", newline="") as handle:
        written = parse(handle.read())
    broken = sorted(k for k, v in original.items() if written.get(k) != v)
    if broken:
        shutil.copy2(backup, path)
        sys.exit(f"Verification failed after write; restored from {backup}. Broken: {broken}")

    print(f"{path}: {len(original)} keys kept unchanged, {len(added)} added: {added}")
    if "\r\n" in original_text:
        print("Line endings normalised CRLF -> LF (a trailing CR corrupts Compose env values).")
    print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
