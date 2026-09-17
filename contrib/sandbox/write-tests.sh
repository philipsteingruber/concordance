#!/usr/bin/env bash
# Live write tests against the sandbox book named in the profile.
#
# Unlike sandbox-tests.sh, these let Concordance itself write, then read the
# target system back to confirm the write landed where it should. The
# allowlist is set to the sandbox book inside this script only; the sandbox is
# reset on exit.
set -uo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=contrib/sandbox/lib.sh
. contrib/sandbox/lib.sh

concordance() {  # concordance [extra args...] -> JSON row for the sandbox book
  python3 -m concordance.cli --book-id "$BOOK" --format json "$@" \
    | jq -c --argjson book "$BOOK" '.pairs[] | select(.calibre_book_id == $book)'
}

abs_current_time() {
  curl -s -H "Authorization: Bearer $ABS_API_KEY" "$ABS_URL/api/me/progress" \
    | jq -r --arg item "$ABS_ITEM" '.mediaProgress[] | select(.libraryItemId == $item) | .currentTime // empty'
}

cwa_progress() {  # -> "device|percentage|progress" of the latest KOSync row for the book
  curl -s -u "$CWA_USER:$CWA_APP_PASSWORD" -H 'accept: application/vnd.koreader.v1+json' \
    "$CWA_URL/kosync/syncs/progress/$BOOK" | jq -r '"\(.device)|\(.percentage)|\(.progress)"'
}

check() {  # check <id> <name> <condition result 0/1> <detail>
  if [ "$3" = 0 ]; then echo "PASS  $1 $2 — $4"; pass=$((pass + 1))
  else echo "FAIL  $1 $2 — $4"; fail=$((fail + 1)); failed+=("$1"); fi
}

trap 'reset_sandbox >/dev/null 2>&1; rm -rf "$EMPTY_CACHE"' EXIT
echo "Concordance sandbox WRITE tests — Calibre $BOOK ($(basename "$profile"))"
echo

# W1: ebook ahead -> Concordance writes the audiobook position.
reset_sandbox; cwa_xp "$W_EBOOK_XP" "$W_EBOOK_PCT"
row=$(CONCORDANCE_WRITE_ALLOWLIST=$BOOK concordance --apply)
status=$(jq -r '.write_status // "none"' <<<"$row")
t=$(abs_current_time)
ok=$(awk -v t="${t:-0}" -v lo="$W1_ABS_MIN" -v hi="$W1_ABS_MAX" 'BEGIN{print (t >= lo && t <= hi) ? 0 : 1}')
[ "$status" = 200 ] || ok=1
check W1 "ebook ahead writes ABS position" "$ok" "HTTP $status, ABS currentTime=${t:-none} (expect $W1_ABS_MIN-$W1_ABS_MAX)"

# W2: audiobook ahead -> Concordance writes a real XPointer to CWA.
reset_sandbox; abs_at "$W2_ABS_AT"
row=$(CONCORDANCE_WRITE_ALLOWLIST=$BOOK concordance --apply)
status=$(jq -r '.write_status // "none"' <<<"$row")
IFS='|' read -r device pct xp <<<"$(cwa_progress)"
ok=$(awk -v p="${pct:-0}" -v lo="$W2_PCT_MIN" -v hi="$W2_PCT_MAX" 'BEGIN{print (p >= lo && p <= hi) ? 0 : 1}')
[ "$status" = 200 ] && [ "$device" = concordance ] && [[ "$xp" == "/body/DocFragment[$W2_DOCFRAGMENT]/"* ]] || ok=1
check W2 "audio ahead writes CWA XPointer" "$ok" "HTTP $status, device=$device pct=$pct xp=$xp"

# W3: without --apply nothing is written, even when allowlisted.
reset_sandbox; cwa_xp "$W_EBOOK_XP" "$W_EBOOK_PCT"
row=$(CONCORDANCE_WRITE_ALLOWLIST=$BOOK concordance)
t=$(abs_current_time)
[ -z "$t" ] && [ "$(jq -r '.write_status // "none"' <<<"$row")" = none ]; ok=$?
check W3 "dry run writes nothing" "$ok" "ABS currentTime=${t:-none}, blocked_by=$(jq -c '.write_blocked_by' <<<"$row")"

# W4: --apply without the book in the allowlist writes nothing.
reset_sandbox; cwa_xp "$W_EBOOK_XP" "$W_EBOOK_PCT"
row=$(CONCORDANCE_WRITE_ALLOWLIST= concordance --apply)
t=$(abs_current_time)
[ -z "$t" ] && [ "$(jq -r '.write_status // "none"' <<<"$row")" = none ]; ok=$?
check W4 "not allowlisted writes nothing" "$ok" "ABS currentTime=${t:-none}, blocked_by=$(jq -c '.write_blocked_by' <<<"$row")"

# W5: with the alignment cache, the ABS write uses the aligned time.
reset_sandbox; cwa_xp "$W_EBOOK_XP" "$W_EBOOK_PCT"
if use_real_cache; then
  row=$(CONCORDANCE_WRITE_ALLOWLIST=$BOOK concordance --apply)
  status=$(jq -r '.write_status // "none"' <<<"$row")
  t=$(abs_current_time)
  ok=$(awk -v t="${t:-0}" -v lo="$W5_ABS_MIN" -v hi="$W5_ABS_MAX" 'BEGIN{print (t >= lo && t <= hi) ? 0 : 1}')
  [ "$status" = 200 ] && [ "$(jq -r .tier <<<"$row")" = aligned ] || ok=1
  check W5 "aligned write lands on the aligned time" "$ok" "HTTP $status, tier $(jq -r .tier <<<"$row"), ABS currentTime=${t:-none} (expect $W5_ABS_MIN-$W5_ABS_MAX)"
  export CONCORDANCE_CACHE_DIR="$EMPTY_CACHE"
else
  check W5 "aligned write lands on the aligned time" 1 "cache entry missing"
fi

echo
echo "$pass passed, $fail failed${failed:+ (${failed[*]})}"
[ "$fail" -eq 0 ]
