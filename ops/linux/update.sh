#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

# The whole release in one command: pull, then the full deploy. Two commands is one
# command that eventually does not happen - the same reason deploy.sh absorbed the
# manual PeriodicTask delete and the beat restart.
#
# The pull is --ff-only on purpose: a checkout that diverged from its upstream stops
# here with git's own message instead of quietly merging on a production box. Every
# flag is passed through to deploy.sh (--skip-backup).
git pull --ff-only
exec sh "$PROJECT_DIR/ops/linux/deploy.sh" "$@"
