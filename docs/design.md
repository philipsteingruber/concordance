# Design notes

Why Concordance works the way it does. The README covers setup; this covers the
reasoning and the facts about CWA, KOReader and Audiobookshelf that the code
depends on. Numbers come from one library of about 500 ebooks and 115
audiobooks, so treat them as indications, not guarantees.

## The problem

Audiobookshelf can hold ebooks, but here the ebooks live in Calibre-Web
Automated (CWA) and are read on a Kobo running KOReader. So this isn't
ebook-to-ebook sync. It's the Whispersync problem: turning a place in a text
into a timestamp in a narration, and back.

A percentage doesn't do that well. Text length and narration time aren't
proportional, and the error builds up across the book. On one 10.7-hour book,
mapping by percentage was off by 6.4 minutes on average and 12.6 minutes near
the end.

## Where the positions come from

**CWA.** KOReader's CWA sync plugin pushes positions to CWA's built-in KOSync
server:

- `progress` is a KOReader XPointer such as
  `/body/DocFragment[28]/body/div/p[14]/text().57`. `DocFragment[N]` is the Nth
  spine item of the file KOReader has open.
- `percentage` comes from KOReader's own page layout, so it depends on font and
  screen, and can sit a point away from a character-based percentage for the
  same spot. Concordance trusts the XPointer.
- `GET /kosync/export` lists every book's percentage in one call but has no
  XPointer. That needs `GET /kosync/syncs/progress/<document>`, one call per book.
- The single-document route returns the percentage as a 0–1 fraction, while
  export and the database use 0–100. Mixing them up fails silently.
- A numeric Calibre book id works as the KOSync document key, so Concordance
  never has to reproduce KOReader's partial-MD5 file hash.

Never write CWA's SQLite database directly. The state CWA pushes to a Kobo is
driven by an ORM listener (`kobo_reading_state.last_modified`), not a database
trigger, so a raw SQL write changes the value without ever reaching the device.
Writes go through the KOSync API.

**Audiobookshelf.** Progress is `PATCH /api/me/progress/<libraryItemId>`. It
doesn't create a listening session or inflate listening stats. There's no stale
write guard, and sending `isFinished: false` resets `currentTime` to 0 as a side
effect, so Concordance never sends it. Chapters only come from
`GET /api/items/<id>?expanded=1`; the library listing carries a count, not the
chapters.

## Matching chapters

Pairs are matched by ISBN, then by normalised title with an author check.

Inside a pair, spine items (text) and audio chapters don't line up one to one.
Ebooks carry title pages, tables of contents and bonus material. Audiobooks
carry credits and sometimes split one chapter over several tracks. Pairing item
N with chapter N broke on a large share of books.

The boundary matcher (`anchor.py`) aligns boundaries instead of counts. A
dynamic program walks both sides' cumulative proportions and scores each
candidate segment by how well its share of the text matches its share of the
audio, so one spine file can cover several chapters and the other way round. Two
refinements came from real failures:

- **Unmatched edges.** Up to 10 items at the start or end may be skipped for a
  small penalty. One ebook had 17% of its text after the epilogue (a bonus
  story, an interview, a preview), which otherwise tilted every segment.
- **Scale correction.** A first pass estimates the overall text/audio offset and
  a second pass matches with it, since extra material on one side compresses
  every proportion on that side.

Across 102 pairs this took confident mappings from 48 to 91 and unusable ones
from 13 to 4.

**KEPUB and EPUB spines differ.** kepubify inserts a title-page dummy at spine
position 1 in some KEPUBs, which puts every KEPUB index one past the EPUB's.
Concordance builds the chapter map from the file the Kobo reads (taken from the
XPointer, defaulting to the KEPUB). Getting this wrong aligns every chapter
against the audio of the one before it.

## Placing a position

Positions come in tiers, most precise first:

| Tier | How | Written? |
| --- | --- | --- |
| `aligned` | Forced alignment of the chapter, cached | yes |
| `interpolated` | Proportional within the chapter, from the XPointer's exact character offset | yes |
| `anchor` | Start of the chapter | no |
| `percentage` | Whole-book proportion | no |
| `finished` | Either side's own definition of finished | yes |

Interpolation within a chapter was measured at 17 s mean and 37 s worst error on
a 40-minute chapter, always late. Alignment brings that to about a word. Only
the precise tiers are written, because a chapter start can be tens of minutes
from where you are.

## Deciding direction

Furthest position wins, and writes only move forward.

- CWA's Kobo bookmark refuses a lower percentage at the SQL level, so a
  backwards write to CWA would be dropped anyway.
- KOSync stamps a push with the time the server received it. A Kobo that syncs
  late looks newer while carrying an old position, so "most recent wins" would
  trust stale data.
- Reading trackers that import from both systems read a moved position as
  reading.

Positions are compared chapter first. Inside one chapter, a deadband stops
back-and-forth over estimation noise: 120 s for interpolated positions, 20 s for
aligned ones.

**The rewind.** Writes toward Audiobookshelf land 150 s early
(`CONCORDANCE_REWIND_SECONDS`). Landing late spoils what comes next and forces you
to scan backwards through audio. Landing early costs a little repetition. Writes
toward the ebook aren't rewound, because scanning back through text is easy.

**The percentage margin.** KOSync keeps the highest percentage across devices.
KOReader's page-based percentage runs up to about a point below a
character-based one for the same place, so if Concordance wrote its own
percentage, the Kobo's next real pushes would be rejected as behind until you
read past the gap. Concordance writes the percentage 2 points low
(`CONCORDANCE_CWA_PERCENT_MARGIN`). The jump itself uses the XPointer, so the
margin only changes the number in KOReader's prompt.

**What the reader sees.** When KOReader opens a book, the CWA plugin fetches the
latest position and asks "sync to latest location 38% from device
'concordance'?". Accepting jumps to the written XPointer. Concordance writes
under its own device name, so KOSync's same-device rewind rule can never move the
Kobo's own record backwards.

## Prior art

- **bookbridge** is the closest mature project and worth reading for its conflict
  handling. It depends on Whisper transcription for books without Storyteller or
  SMIL data, has no read-only mode, and polls every few minutes.
- **open-whispersync** targets this stack but assumes Kobo's native sync
  (koboSpan anchors) rather than KOReader, and patches CWA's source, which a
  container update silently reverts.
- **Storyteller** bakes alignment into EPUB 3 media overlays, a different shape
  of solution. Its CTC search step was a useful reference.

Forced alignment is covered in [aligner.md](aligner.md).
