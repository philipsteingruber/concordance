#!/usr/bin/env bash
# Read-only regression tests against the sandbox book named in the profile.
#
# Each test resets the sandbox, sets progress through the live CWA/ABS APIs,
# runs the read-only report, and compares its decision to a value computed
# ahead of time from the real translation code. Nothing is written except the
# sandbox's own progress, and the sandbox is reset on exit — even on Ctrl-C.
#
# Expected values are a function of the book's structure and the rewind, so
# they live in the profile.
set -uo pipefail
cd "$(dirname "$0")/../.."

# shellcheck source=contrib/sandbox/lib.sh
. contrib/sandbox/lib.sh

# direction|tier|what-would-be-written, from the JSON report.
signature() {
  python3 -m concordance.cli --book-id "$BOOK" --format json | jq -r --argjson book "$BOOK" '
    .pairs[] | select(.calibre_book_id == $book) |
    [ (.direction // "none"),
      (.tier // "-"),
      ( if .direction == "to_abs" then
          (if .would_write_abs_finished then "finished" else (.would_write_seconds | floor | tostring) end)
        elif .direction == "to_cwa" then
          (if .would_write_cwa_finished then "finished"
           else (((.would_write_cwa_percentage * 10 | round) / 10 | tostring)
                 + "@DF" + (.would_write_cwa_spine_index | tostring)) end)
        else "-" end )
    ] | join("|")'
}

run_test() {  # run_test <id> <name> <expected> <setup command>...
  local id=$1 name=$2 expected=$3
  shift 3
  printf '%-3s %-36s ' "$id" "$name"
  export CONCORDANCE_CACHE_DIR="$EMPTY_CACHE"
  if ! reset_sandbox; then
    echo "ERROR  reset failed"; fail=$((fail + 1)); failed+=("$id"); return
  fi
  local cmd
  for cmd in "$@"; do
    if ! eval "$cmd"; then
      echo "ERROR  setup failed: $cmd"; fail=$((fail + 1)); failed+=("$id"); return
    fi
  done
  local got
  got=$(signature)
  if [ "$got" = "$expected" ]; then
    echo "PASS   $got"; pass=$((pass + 1))
  else
    echo "FAIL   expected $expected"
    printf '%42s got      %s\n' "" "$got"
    fail=$((fail + 1)); failed+=("$id")
  fi
}

trap 'reset_sandbox >/dev/null 2>&1; rm -rf "$EMPTY_CACHE"' EXIT

echo "Concordance sandbox tests — Calibre $BOOK ($(basename "$profile"))"
echo

sandbox_read_tests

echo
echo "$pass passed, $fail failed${failed:+ (${failed[*]})}"
[ "$fail" -eq 0 ]
