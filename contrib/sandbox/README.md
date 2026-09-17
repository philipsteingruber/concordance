# Sandbox tests

Live tests against your own CWA and Audiobookshelf, using one dedicated book you
aren't reading. Each test resets the book, sets a position through the real
APIs, runs Concordance, and compares the result with an expected value you
recorded for that book. The book is reset again on exit, including after Ctrl-C.

- `read-tests.sh` checks decisions only and writes nothing except the sandbox
  book's own progress.
- `write-tests.sh` lets Concordance write, then reads CWA and ABS back to check
  where the position landed.
- `reset.py` restores the sandbox book's CWA progress to its untouched state.

## Why the reset touches CWA's database

CWA's Kobo bookmark only moves forward: a lower percentage pushed through
KOSync is silently ignored. So test progress can't be undone through the API.
`reset.py` restores the placeholder rows every Kobo-synced book starts with,
inside one transaction, without touching timestamps (so nothing is pushed to a
device). It refuses to run on a book with KOSync progress from any device not
listed in `CONCORDANCE_SANDBOX_DEVICES`, so it can't wipe a real reading
position.

The ABS side is reset through the API.

## Setting up your own sandbox

1. Pick a book with both an ebook and an audiobook, no real progress on either
   side, and long chapters, so a wrong position is easy to hear. Check that
   `concordance --book-id <id>` reports a `high` chapter match.
2. Copy `profiles/lucky-day.sh` to `profiles/<your-book>.sh`. Set `BOOK`,
   `ABS_ITEM` and `DURATION`, and find a real XPointer mid-chapter: read to that
   spot on the device and take it from CWA's
   `/kosync/syncs/progress/<id>`.
3. Record the expectations. Run each setup once, check the answer by hand (open
   the page, play the timestamp), and write down what Concordance reported.
   The profile's comments describe each value.
4. Configure and run:

```bash
export CONCORDANCE_SANDBOX_PROFILE=contrib/sandbox/profiles/<your-book>.sh
export CWA_APP_DB=/path/to/cwa/config/app.db
contrib/sandbox/read-tests.sh
contrib/sandbox/write-tests.sh
```

Needs `jq` and the same environment as Concordance itself. The aligned tests
(`T9`, `A9`, `W5`) need the profile's chapter aligned first:
`python3 -m concordance.orchestrate --run --book-id <id>`.

The expected values depend on the rewind setting and the percentage margin. If
you change either, record them again.
