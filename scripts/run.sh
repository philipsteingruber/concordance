#!/usr/bin/env bash
# Run a Concordance command with the credential file already loaded.
#
#   scripts/run.sh chapters propose 561
#   scripts/run.sh cli --format table
#   scripts/run.sh orchestrate --planned-only
#
# The first argument is a module under `concordance.`, the rest are its own
# arguments. Everything is passed through untouched.
#
# Why this exists: the credential file holds the CWA app password and the ABS
# and Calibre settings, and sourcing it by hand before every command is both a
# nuisance and a good way to leak a password into a shell history or a terminal
# transcript. Sourcing happens inside this script instead, the same way the cron
# wrappers do it, so the values never have to be typed, echoed or pasted.
#
# Nothing here prints the environment. Keep it that way: this script is expected
# to be safe to run with its output going somewhere it will be read.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if [ "$#" -eq 0 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

MODULE="$1"
shift
case "$MODULE" in
  *[!a-z_]*)
    echo "module name may only contain lowercase letters and underscores: $MODULE" >&2
    exit 2
    ;;
esac

if [ ! -f ./.env ]; then
  echo "no credential file in $(pwd). Copy the example file and fill it in." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

PYTHON="${CONCORDANCE_PYTHON:-python3}"   # e.g. /path/to/venv/bin/python
exec "$PYTHON" -m "concordance.$MODULE" "$@"
