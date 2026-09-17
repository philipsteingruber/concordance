# Shared helpers for the sandbox scripts. Source this; don't run it.
# The sandbox book and its expectations come from a profile file named by
# CONCORDANCE_SANDBOX_PROFILE (see profiles/lucky-day.sh and contrib/sandbox/README.md).
# shellcheck shell=bash

profile="${CONCORDANCE_SANDBOX_PROFILE:-}"
if [ -z "$profile" ] || [ ! -f "$profile" ]; then
  echo "error: set CONCORDANCE_SANDBOX_PROFILE to a sandbox profile file (see contrib/sandbox/README.md)" >&2
  exit 2
fi
# shellcheck source=profiles/lucky-day.sh
. "$profile"

for var in CWA_USER CWA_APP_PASSWORD ABS_API_KEY CWA_APP_DB CALIBRE_ROOT; do
  if [ -z "${!var:-}" ]; then
    echo "error: $var is not set — source your credential file into the environment first (see README)" >&2
    exit 2
  fi
done
command -v jq >/dev/null || { echo "error: jq is required" >&2; exit 2; }
if [ "${CONCORDANCE_REWIND_SECONDS:-150}" != "$EXPECTED_REWIND" ]; then
  echo "error: the profile's expectations assume a ${EXPECTED_REWIND}s rewind" >&2
  exit 2
fi
export CONCORDANCE_SANDBOX_BOOK_ID="$BOOK"

CWA_URL="${CWA_URL:-http://localhost:8083}"
ABS_URL="${ABS_URL:-http://localhost:13378}"

pass=0
fail=0
failed=()

cwa() {  # cwa <DocFragment> <fraction 0-1>
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" -u "$CWA_USER:$CWA_APP_PASSWORD" -X PUT \
    -H 'accept: application/vnd.koreader.v1+json' -H 'content-type: application/json' \
    -d "{\"document\":\"$BOOK\",\"progress\":\"/body/DocFragment[$1]/body/p[1]/text().0\",\"percentage\":$2,\"device\":\"concordance-sandbox\",\"device_id\":\"sandbox\"}" \
    "$CWA_URL/kosync/syncs/progress")
  [ "$code" = 200 ] || { echo "CWA push returned HTTP $code" >&2; return 1; }
}

cwa_xp() {  # cwa_xp <full XPointer> <fraction 0-1>
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" -u "$CWA_USER:$CWA_APP_PASSWORD" -X PUT \
    -H 'accept: application/vnd.koreader.v1+json' -H 'content-type: application/json' \
    -d "{\"document\":\"$BOOK\",\"progress\":\"$1\",\"percentage\":$2,\"device\":\"concordance-sandbox\",\"device_id\":\"sandbox\"}" \
    "$CWA_URL/kosync/syncs/progress")
  [ "$code" = 200 ] || { echo "CWA push returned HTTP $code" >&2; return 1; }
}

abs_patch() {  # abs_patch <json>
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" -X PATCH \
    -H "Authorization: Bearer $ABS_API_KEY" -H 'content-type: application/json' \
    -d "$1" "$ABS_URL/api/me/progress/$ABS_ITEM")
  [ "$code" = 200 ] || { echo "ABS patch returned HTTP $code" >&2; return 1; }
}

abs_at() {  # abs_at <seconds>
  abs_patch "{\"currentTime\":$1,\"duration\":$DURATION,\"progress\":$(awk "BEGIN{print $1/$DURATION}")}"
}

abs_finished() {
  abs_patch "{\"isFinished\":true,\"currentTime\":$DURATION,\"duration\":$DURATION,\"progress\":1}"
}

reset_sandbox() {
  python3 contrib/sandbox/reset.py --apply >/dev/null || { echo "CWA reset failed" >&2; return 1; }
  local id
  id=$(curl -s -H "Authorization: Bearer $ABS_API_KEY" "$ABS_URL/api/me/progress" \
    | jq -r --arg item "$ABS_ITEM" '.mediaProgress[] | select(.libraryItemId == $item) | .id')
  if [ -n "$id" ]; then
    curl -s -o /dev/null -X DELETE -H "Authorization: Bearer $ABS_API_KEY" \
      "$ABS_URL/api/me/progress/$id" || { echo "ABS reset failed" >&2; return 1; }
  fi
}


# Tests run against an empty alignment cache unless they opt in with use_real_cache,
# so a cache entry built later can't silently change what an existing test checks.
EMPTY_CACHE=$(mktemp -d)
export CONCORDANCE_CACHE_DIR="$EMPTY_CACHE"
# Each test sets the allowlist itself through CONCORDANCE_WRITE_ALLOWLIST, so the
# real allowlist file mustn't leak in (it would break the "not allowlisted" test).
export CONCORDANCE_WRITE_ALLOWLIST_FILE=/dev/null
REAL_CACHE="${CONCORDANCE_STATE_DIR:-$HOME/.cache/concordance}/alignments"

use_real_cache() {  # opt a test in to the real alignment cache (the profile's entry must exist)
  if [ ! -f "$REAL_CACHE/$BOOK/$ALIGNED_ENTRY" ]; then
    echo "no cache entry $BOOK/$ALIGNED_ENTRY — align it first (python3 -m concordance.orchestrate --run --book-id $BOOK)" >&2
    return 1
  fi
  export CONCORDANCE_CACHE_DIR="$REAL_CACHE"
}
