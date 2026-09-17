# Sandbox profile: the book, its ids, and the expected results for its structure.
# This one is the author's (Lucky Day by Chuck Tingle: 14 chapters of ~38 min,
# spine items 5-18 = ABS chapters 2-15). Copy it for your own sandbox book and
# recompute the expectations: run each setup against your book once, check the
# result by hand (open the ebook, play the audio), then record it here.
# shellcheck shell=bash

BOOK=463                                         # Calibre id
ABS_ITEM=c7f6855f-907b-4bc5-ab25-708e2273ead1    # ABS library item id
DURATION=30288.247                               # ABS duration, seconds
EXPECTED_REWIND=150                              # the expectations assume this rewind

# A real, resolvable XPointer halfway through spine item 10 (KEPUB).
MID10="/body/DocFragment[10]/body/div/div/section/p[79]/span[4]/text().57"
# Alignment cache entry the aligned tests need (built by the aligner container).
ALIGNED_ENTRY="kepub-sp10-10-ch7-7.json.gz"

# Read-only decision tests: run_test <id> <name> <direction|tier|write> <setup>...
sandbox_read_tests() {
  # One side only: ebook -> audiobook. Synthetic p[1] paths don't resolve, so
  # these land on the chapter start (anchor tier).
  run_test T1 "basic anchor"                     "to_abs|anchor|10143"      "cwa 10 0.40"
  run_test T2 "rewind clamps at zero"            "to_abs|anchor|0"          "cwa 5 0.02"
  run_test T3 "last chapter"                     "to_abs|anchor|28122"      "cwa 18 0.97"
  run_test T4 "front matter falls back"          "to_abs|percentage|1"      "cwa 2 0.005"
  run_test T5 "ebook finished -> abs finished"   "to_abs|finished|finished" "cwa 18 1.0"
  run_test T6 "XPointer beats percentage"        "to_abs|anchor|10143"      "cwa 10 0.70"
  run_test T7 "beyond spine falls back"          "to_abs|percentage|16464"  "cwa 30 0.55"

  # Audiobook side, and both sides at once
  run_test A1 "audiobook only -> cwa"            "to_cwa|interpolated|24@DF8"  "abs_at 7200"
  run_test A2 "audiobook chapters ahead -> cwa"  "to_cwa|interpolated|60@DF13" "cwa 10 0.40" "abs_at 18000"
  run_test A3 "ebook chapters ahead -> abs"      "to_abs|anchor|16898"      "cwa 13 0.60" "abs_at 7200"
  run_test A4 "same chapter -> in sync"          "in_sync|anchor|-"         "cwa 10 0.40" "abs_at 10800"
  run_test A5 "audiobook finished -> cwa"        "to_cwa|finished|finished" "abs_finished"
  run_test A6 "both finished -> in sync"         "in_sync|-|-"              "cwa 18 1.0" "abs_finished"

  # Within-chapter placement from a real XPointer
  run_test T8 "mid-chapter position interpolates" "to_abs|interpolated|11352" "cwa_xp '$MID10' 0.40"
  run_test A7 "ebook ahead within same chapter"   "to_abs|interpolated|11352" "cwa_xp '$MID10' 0.40" "abs_at 10353"
  run_test A8 "same chapter within 120s deadband" "in_sync|interpolated|-"    "cwa_xp '$MID10' 0.40" "abs_at 11442"

  # Forced-alignment cache (opt in to the real cache)
  run_test T9 "aligned ebook position (cache hit)" "to_abs|aligned|11328"     "use_real_cache" "cwa_xp '$MID10' 0.40"
  run_test A9 "aligned audio position -> cwa"      "to_cwa|aligned|38.1@DF10" "use_real_cache" "abs_at 11361.6"
}

# Write tests: where a write must land, as ranges.
W_EBOOK_XP="$MID10"; W_EBOOK_PCT=0.40
W1_ABS_MIN=11351; W1_ABS_MAX=11354               # interpolated ABS write
W2_ABS_AT=18000                                  # audiobook ahead ...
W2_PCT_MIN=0.57; W2_PCT_MAX=0.59                 # ... CWA percentage (60% minus the 2-point margin)
W2_DOCFRAGMENT=13                                # ... in this spine item
W5_ABS_MIN=11327; W5_ABS_MAX=11330               # aligned ABS write
