# Forced alignment

Concordance uses [ctc-forced-aligner](https://github.com/MahmoudAshraf97/ctc-forced-aligner)
with its default MMS-300m model to find the exact timestamp of every word in a
chapter. This page covers why that tool, what it costs, and how the nightly run
keeps it within a small server's memory.

## Choosing an aligner

The requirement that decided it was **partial alignment**: placing known text
inside audio that also contains material the text doesn't cover (credits, the
end of the previous chapter). ctc-forced-aligner handles that by wrapping the
text in `<star>` tokens that absorb unrelated audio at either edge.

| Tool | Partial alignment | Verdict |
| --- | --- | --- |
| ctc-forced-aligner (MMS-300m) | Yes, with edge stars | Chosen |
| torchaudio `forced_align` + MMS_FA | Yes, but chunking and glue are yours | Viable, more work |
| aeneas | No, strict; last release 2017 | Ruled out |
| echogarden | No (DTW around the proportional diagonal) | Ruled out |
| WhisperX, Montreal Forced Aligner | No, strict per segment | Ruled out |

Two installation traps: the PyPI package named `ctc-forced-aligner` is a
different fork, so the image installs from git at a pinned commit. And the Python
function `preprocess_text` defaults to `star_frequency="segment"`; only the CLI
defaults to `edges`, which is what partial alignment needs.

**Licence.** The code is BSD-2-Clause. The MMS-300m model weights are CC-BY-NC
4.0, so non-commercial use only. The image doesn't include the weights; they
download on first use.

## What it costs

Measured on a 4-core/8-thread Intel i5-10500T, CPU only:

| Stage | 40-minute chapter |
| --- | --- |
| Emissions (the acoustic model over the audio) | about 15 min, ≈22 CPU-minutes per hour of audio |
| Alignment of 5,700 words | 20 s |
| Peak memory, batch size 1 | 3.0 GB |

Emissions are about 98% of the time, so alignment runs overnight and results are
cached per chapter group.

**Memory.** Batch size matters far more than audio length. A 6-minute chapter
peaked at 4.2 GB at batch 4 and 2.4 GB at batch 1, with identical word timings
and 13% more time. The Viterbi step adds backpointers of about
2 bits × (2L+2) × (T−L), where L is the number of letters and digits and T is 50
frames per second. That grows with the square of chapter length: about 1.2 GB at
40 minutes, 4 GB at 75, 35 GB at 220.

Slicing the audio for emissions doesn't reduce the peak and shifts frames by
one, so it's off by default.

## Accuracy

On the 40-minute chapter:

- Word timings showed no drift. Words at each tenth of the text landed evenly
  spaced, and the last word was 4 s before the end of the audio.
- A 500-word passage located inside the chapter agreed with the whole-chapter
  alignment to 0.02 s on every word.
- The mean word score separates right from wrong: about −0.08 for the correct
  audio and −3.2 for a passage from another chapter. `CONCORDANCE_MIN_ALIGN_SCORE`
  (default −1.0) sits between them. An entry that falls below it is ignored by
  sync and logged as `aligned-low-score`. A low score is not proof of a wrong
  mapping, though: a truncated audio window produces one too (see *Long
  chapters*), so check where in the entry the score collapses.

### How wrong the fallback is

Every cached alignment holds both the true word time and the proportional
estimate the `interpolated` tier would have produced, so the fallback can be
scored against itself. Five single-chapter segments across three books,
45,000 words:

| Segment span | RMS error | as % of span | Worst | as % of span |
| ---: | ---: | ---: | ---: | ---: |
| 5.6 min | 6.5 s | 1.93% | 13.3 s | 3.95% |
| 11.3 min | 3.6 s | 0.54% | 10.1 s | 1.49% |
| 11.8 min | 6.4 s | 0.90% | 14.8 s | 2.08% |
| 40.3 min | 19.7 s | 0.82% | 36.6 s | 1.52% |
| 219.4 min | 42.3 s | 0.32% | 97.4 s | 0.74% |

The error scales with the segment, not with the book: **worst case is about 2%
of the segment's span**, so 12 s for a 10-minute chapter and 100 s for a
3-hour one.

Two things follow. The 2.5-minute rewind on writes toward the audiobook
comfortably exceeds the worst case for any segment under two hours, which is
what makes an interpolated write safe in that direction. And shortening the
segment is the only lever that scales, because the relationship is linear —
which is why `concordance-chapters` is worth running on a badly chaptered book
even if you never look at the chapter list.

Correcting for the error does not work. The direction is not consistent between
books (signed means ranged from −10 s to +17 s), so a fixed offset tuned on one
book makes another worse. A per-segment linear fit halves the error on short
segments but barely moves a 3-hour one, because inside a long segment the error
wanders with each chapter's pace rather than drifting one way.

## Long chapters

Before running, the worker estimates its peak memory. If a group won't fit
`CONCORDANCE_ALIGN_BUDGET_MB` (default 3,600, under a 4 GB container limit), it
aligns in pieces of about 25 minutes (`CONCORDANCE_ALIGN_CHUNK_SECONDS`). Each piece is cut at a word boundary using
the chapter's estimated speaking rate, aligned in an audio window with 5 minutes
of slack at the far end (`CONCORDANCE_ALIGN_END_MARGIN`), and the next piece starts where the previous one's last
word actually ended. If a piece's last word hits the end of its window, the
window was too short and the piece is retried with double the slack.

The group's own end gets the same treatment, because it comes from the chapter
list rather than from a measurement, and on some books it lands minutes before
the narration stops. A piece may reach up to `CONCORDANCE_ALIGN_END_SLACK`
(default 2,400 s) past it. Without that the last piece is the only one that
can't recover from a short window, since there's no following piece to push
into, so its text ends up crammed against the boundary at several times
narration speed and the resulting mean score rejects an entry that was correct
for most of its length. Measured on one book, three groups went from −0.47,
−2.79 and −1.16 to −0.09, −0.10 and −0.10 once they could reach 8 to 10 minutes
past the ends they were given.

A retry re-decodes and re-aligns its piece, so a book whose chapter list is
accurate pays nothing and one with badly placed boundaries pays 15% to 45% more
wall-clock. The stored entry's end widens to cover whatever it found, so the
extra words are actually reachable when a position is looked up.

Forcing a 40-minute chapter into 10-minute pieces gave the same 5,724 words as a
single pass: median difference 0.00 s, largest 0.46 s, four words over 0.1 s.

## Nightly scheduling

`python3 -m concordance.orchestrate --run` (see `scripts/cron-align.sh`):

- Plans each in-progress book's current chapter group, then following groups
  until they cover 120 minutes of audio (`CONCORDANCE_ALIGN_LOOKAHEAD_MINUTES`).
  Counting audio rather than groups keeps the lookahead the same across books
  whose groups run from ten minutes to several hours. Every book's current group
  is queued before any lookahead. A dry run lists the plan; `--planned-only`
  hides books that are skipped or already aligned, skipping groups with a fresh cache entry. An entry goes stale
  when the book file or any audio file changes size or modification time.
- Starts a job only when at least 5,000 MB is available (`CONCORDANCE_ALIGN_MIN_FREE_MB`),
  rechecking every 5 minutes for up to `CONCORDANCE_ALIGN_MEMORY_WAIT` minutes.
- With `CONCORDANCE_ALIGN_DEADLINE` set, starts nothing after that time, and
  nothing that wouldn't finish by it at the measured pace (about half of real
  time). A running job is never interrupted.
- Runs one container at a time as your user, so the cache isn't root-owned,
  with `CONCORDANCE_ALIGNER_MEMORY` and `CONCORDANCE_ALIGNER_CPU_SHARES`.

**CPU priority.** Under cgroup v2, Docker's `--cpu-shares` maps non-linearly to
`cpu.weight`: 256 → 35, 1024 → 100 (the default), 26192 → 1393. If a transcoder
shares the night, 26192 gives alignment roughly a 14:1 share when both are busy.
The transcoder slows down but isn't paused, and it gets the whole CPU back once
alignment is done.
