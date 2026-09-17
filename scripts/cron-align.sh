#!/usr/bin/env bash
# Nightly alignment (cron, e.g. 00:05). Plans and runs aligner jobs for books in
# progress; see concordance/orchestrate.py for the memory, deadline and CPU guards.
# Example crontab line:
#   5 0 * * * /path/to/concordance/scripts/cron-align.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# Configuration lives in the git-ignored credential file at the repo root.
if [ -f ./.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
STATE="${CONCORDANCE_STATE_DIR:-$HOME/.cache/concordance}"
PYTHON="${CONCORDANCE_PYTHON:-python3}"   # e.g. /path/to/venv/bin/python
mkdir -p "$STATE/runs"
exec 9>"$STATE/runs/align.lock"
flock -n 9 || { echo "$(date -Is) align already running" >> "$STATE/runs/align.log"; exit 0; }

{
  echo "=== $(date -Is) align start"
  "$PYTHON" -m concordance.orchestrate --run
  rc=$?  # capture first: the $(date) below would reset $?
  echo "=== $(date -Is) align end (exit $rc)"
} >> "$STATE/runs/align.log" 2>&1
