#!/usr/bin/env bash
# Frequent sync (cron, every ~15 min). Cheap: cache lookups and a few API calls.
# Writes only for books in CONCORDANCE_WRITE_ALLOWLIST, so enabling writes is a
# matter of extending that list one book at a time.
# Example crontab line:
#   */15 * * * * /path/to/concordance/scripts/cron-sync.sh
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
exec 9>"$STATE/runs/sync.lock"
flock -n 9 || exit 0

{
  echo "=== $(date -Is) sync"
  "$PYTHON" -m concordance.cli --progress-only --apply --no-artifacts --state-dir "$STATE/runs"
  rc=$?  # capture first: the $(date) below would reset $?
  echo "=== $(date -Is) sync end (exit $rc)"
} >> "$STATE/runs/sync-$(date +%Y%m%d).log" 2>&1
