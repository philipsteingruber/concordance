# Concordance

Concordance keeps your place between an ebook and its audiobook. Read a few
chapters on your Kobo, then start the audiobook in Audiobookshelf, and it picks
up just before where you stopped reading. Listen on a walk, open the book again,
and KOReader offers to jump to the page you reached.

It's for a specific self-hosted setup:

- **Ebooks** in [Calibre-Web Automated](https://github.com/crocodilestick/Calibre-Web-Automated)
  (CWA), read in **KOReader** with CWA's KOReader sync plugin
- **Audiobooks** in [Audiobookshelf](https://www.audiobookshelf.org/) (ABS)
- A Linux host with Docker, for the forced-alignment step

It's developed and used against the
[Calibre-Web NextGen](https://github.com/new-usemame/Calibre-Web-NextGen) fork
of CWA. Upstream CWA may work but hasn't been tested.

**Status: alpha.** It runs daily for one library. Expect rough edges, and read
[Enabling writes](#enabling-writes) before letting it touch real progress.

## How it works

A book-wide percentage doesn't work: narration speed varies, and the error adds
up to several minutes by the end of a book. Concordance works in three layers.

1. **Chapter matching.** It matches the ebook's internal files to the
   audiobook's chapters by comparing proportions, which copes with front matter,
   bonus content, credits tracks and chapters split across tracks.
2. **Exact reading position.** KOReader reports an XPointer, a path to a
   specific character. Concordance resolves it against the actual EPUB or KEPUB,
   so it knows how far into the chapter you are.
3. **Forced alignment.** Each night, a
   [ctc-forced-aligner](https://github.com/MahmoudAshraf97/ctc-forced-aligner)
   container times every word of the chapters you're about to reach, so a
   position maps to within a word.

Positions are placed in tiers, and only the precise ones are written:

| Tier | Where the position comes from | Typical error | Written |
| --- | --- | --- | --- |
| `aligned` | Cached word timings | about a word | yes |
| `interpolated` | Proportion within the chapter | 15–40 s | yes |
| `anchor` | Start of the chapter | up to a chapter | no |
| `percentage` | Whole-book proportion | minutes | no |
| `finished` | Either side marked finished | — | yes |

When both sides have a position, the one further along wins, and writes only
ever move forward. Writes toward the audiobook land 2.5 minutes early, because
skipping forward in audio is easy and hunting backwards isn't.
[docs/design.md](docs/design.md) explains these choices and the API behaviour
behind them; [docs/aligner.md](docs/aligner.md) covers alignment cost and
accuracy.

## Requirements

- Python 3.11+ with `requests`, on the host
- Docker, for the aligner image (about 3 GB, plus 1.3 GB of model weights
  downloaded on first use)
- Read access to the Calibre library directory and the audiobook files
- **Memory:** an alignment job peaks around 3 GB, and by default a job only
  starts while 5 GB is free. Long chapters are split automatically to stay
  within a memory budget.
- **CPU:** alignment takes about 22 CPU-minutes per hour of audio. Only the
  chapters just ahead of your position are aligned, so one night usually covers
  it.

## Setup

```bash
git clone <this repository> concordance && cd concordance
pip install -e .
cp .env.example .env    # then fill it in
```

In `.env`, set:

- `CWA_USER` and `CWA_APP_PASSWORD`: create an app password in CWA under your
  profile, *App passwords for OPDS / KOReader Sync*.
- `ABS_API_KEY`: *Settings → API Keys* in Audiobookshelf.
- `CALIBRE_ROOT`: the Calibre library directory.
- `ABS_AUDIO_ROOT_MAP`: ABS reports file paths as its own container sees them,
  e.g. `/audiobooks/...`. Map that prefix to the host path, e.g.
  `/audiobooks=/srv/media/audiobooks`.

Load it into your shell for manual runs:

```bash
set -a; . ./.env; set +a
```

### First look (read-only)

```bash
concordance --progress-only
```

This prints every matched pair with reading progress: how well the chapters
matched, both positions, which way a sync would go, and what it would write.
Nothing is sent. JSON and Markdown copies go to `~/.cache/concordance/reports/`.

### Build the aligner image

```bash
docker build -f docker/aligner.Dockerfile -t concordance-aligner:latest .
python3 -m concordance.orchestrate                  # dry run: what it would align
python3 -m concordance.orchestrate --planned-only   # ...without the skipped and already-aligned books
python3 -m concordance.orchestrate --run --book-id 123
```

Each job logs one JSON line with its word count, mean score and peak memory. A
mean score near zero is good. Below −1 means the text was matched to the wrong
audio, and the job is logged as `aligned-low-score`.

### Schedule it

```cron
5 0 * * *    /path/to/concordance/scripts/cron-align.sh
*/15 * * * * /path/to/concordance/scripts/cron-sync.sh
```

Both wrappers load `.env` from the repository root, hold a lock so runs don't
overlap, and log to `~/.cache/concordance/runs/`. They run `python3`; if you
installed into a virtualenv, set `CONCORDANCE_PYTHON` to its interpreter.

Alignment starts a job only while at least 5 GB of memory is free
(`CONCORDANCE_ALIGN_MIN_FREE_MB`), waiting up to an hour for it. To keep
alignment inside a quiet window, set `CONCORDANCE_ALIGN_DEADLINE` (e.g.
`06:00`): no job starts after it, and none starts that the measured pace says
would still be running at it, so a three-hour group can't begin ten minutes
before. A job already under way is never interrupted.

## Enabling writes

Nothing is written for a book until its Calibre id is in the write allowlist;
until then the sync only reports. Manage it with `concordance-allow`:

```bash
concordance-allow add 561 581   # one or more Calibre ids
concordance-allow remove 561
```

Without `pip install`, run `python3 -m concordance.allowlist` from the
repository instead. Each change prints the resulting list.

The allowlist is a plain, git-ignored file, `allowlist` in the repository root
(`CONCORDANCE_WRITE_ALLOWLIST_FILE` moves it), with one id per line. With
`.env` loaded, `add` looks the book up in Calibre and writes its title as a
comment (`561  # Misery`), and warns if no book has that id. Ids in the
`CONCORDANCE_WRITE_ALLOWLIST` variable are allowed as well, and `remove` warns
when an id is still allowed there.

Enable books one at a time:

1. Pick a test book you aren't reading. Set progress on one side, allowlist it,
   run `concordance --book-id <id> --apply`, and check where you land on the
   other side.
2. Add one real book. Read the next sync's output, then listen or read and check
   the position.
3. Add more.

Values in `.env` can't contain unquoted spaces, because the file is loaded by the
shell. `CONCORDANCE_WRITE_ALLOWLIST=12, 34` makes the shell run `34` as a
command and leaves the variable empty; write `12,34`.

**What you'll see in KOReader.** When you open a book, the CWA plugin fetches the
latest position and asks something like *"Sync to latest location 36% from
device 'concordance'?"*. The percentage is 2 points below the real one on
purpose, so your Kobo's own saves aren't rejected as being behind. Accepting
jumps to the exact spot.

## Fixing chapter marks in Audiobookshelf

Some audiobook releases mark arbitrary slices of the recording as chapters: ten
70-minute "Chapter N" tracks for a book that has 120 chapters. That makes the
player's chapter list useless. `concordance-chapters` rebuilds those slices from
the ebook's real chapters, one book at a time:

```bash
concordance-chapters check 561      # instant: ABS chapters vs real chapters, per ABS chapter
concordance-chapters propose 561    # locate each real chapter in the audio; writes nothing
concordance-chapters apply 561      # back up ABS's chapter list, then write the proposal
concordance-chapters restore 561    # put the latest backup back
```

Without `pip install`, run `python3 -m concordance.chapters <action> <book>` from
the repository. `check` costs nothing; `propose` is the slow step, about two
minutes of CPU per chapter it has to locate.

A book is only touched when it has at least 1.5 times as many real chapters as
ABS chapters, and only the ABS chapters that contain two or more real chapters
are rebuilt, together with any short run of slices between them.

- **Finding chapters in the ebook:** table-of-contents entries that point inside
  files, numbered headings that count upwards (it tells a book's chapters apart
  from, say, a novel-within-the-novel's "CHAPTER 3"), or one chapter per file.
  When numbering restarts in each part, titles become "Part Two, Chapter 4".
- **Finding them in the audio:** times come from the alignment cache where a
  chapter is already aligned. Otherwise each chapter's opening words are aligned
  in a window around an estimate, one chapter after another, each estimate
  anchored on the last chapter found. It takes about two minutes of CPU per
  chapter; confirmed times are remembered, so a second `propose` only looks for
  what's missing. Chapters that can't be confirmed are left out, not guessed.
- **Keeping alignments:** a cache entry's name carries the ABS chapters it covered,
  so `apply` renames affected entries to the new numbering. Without that, the next
  nightly run would align the same audio again.
- **Writing:** `apply` refuses if ABS's chapters changed since the proposal, or if
  the book was listened to in the last 24 hours (`--force` overrides). It uses
  ABS's `POST /api/items/<id>/chapters`, so no restart is needed. ABS stores the
  list in the item's metadata file, which a library rescan applies last, so the
  new chapters survive rescans. Backups go to `~/.cache/concordance/chapters/`.

## Monitoring

Under `~/.cache/concordance/runs/`:

| File | Contents |
| --- | --- |
| `align-YYYYMMDD.jsonl` | One line per planned, skipped or finished alignment job |
| `sync-YYYYMMDD.log` | Each sync's table, with start and end markers and the exit code |
| `last-sync.json` | The latest sync's full report: positions, decisions, block reasons |
| `writes.jsonl` | Every write applied, with its HTTP status |

Two things are worth alerting on: a book written in both directions within a
day, which means the two sides disagree about which is further, and repeated
`aligned-low-score` jobs.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CWA_URL` | `http://localhost:8083` | CWA base URL |
| `CWA_USER`, `CWA_APP_PASSWORD` | — | KOSync credentials |
| `ABS_URL` | `http://localhost:13378` | Audiobookshelf base URL |
| `ABS_API_KEY` | — | Audiobookshelf API key |
| `CALIBRE_ROOT` | — | Calibre library directory |
| `CALIBRE_DB` | `$CALIBRE_ROOT/metadata.db` | Calibre database |
| `ABS_AUDIO_ROOT_MAP` | — | `<path in ABS>=<path on host>`; required for alignment |
| `CONCORDANCE_WRITE_ALLOWLIST_FILE` | `allowlist` in the repository root | Allowlist file edited by `concordance-allow` |
| `CONCORDANCE_WRITE_ALLOWLIST` | empty | More Calibre ids that may be written, comma-separated |
| `CONCORDANCE_WRITE_TIERS` | `aligned,interpolated,finished` | Tiers that may be written |
| `CONCORDANCE_REWIND_SECONDS` | `150` | How early audiobook writes land |
| `CONCORDANCE_CWA_PERCENT_MARGIN` | `2.0` | Points subtracted from written ebook percentages |
| `CONCORDANCE_MIN_ALIGN_SCORE` | `-1.0` | Alignments scoring below this are ignored |
| `CONCORDANCE_ALIGNER_IMAGE` | `concordance-aligner:latest` | Aligner image |
| `CONCORDANCE_ALIGNER_MEMORY` | `4g` | Container memory limit |
| `CONCORDANCE_ALIGN_MIN_FREE_MB` | `5000` | Start a job only while this much memory is available |
| `CONCORDANCE_ALIGN_LOOKAHEAD` | `2` | Chapter groups past your position to align |
| `CONCORDANCE_ALIGN_BUDGET_MB` | `3600` | Groups whose estimated peak exceeds this are aligned in chunks |
| `CONCORDANCE_ALIGN_CHUNK_SECONDS` | `1500` | Narration per chunk |
| `CONCORDANCE_ALIGN_END_MARGIN` | `300` | Extra audio after each chunk's estimated end, in seconds |
| `CONCORDANCE_ALIGN_BATCH_SIZE` | `1` | Emission batch size; higher is a little faster and uses much more memory |
| `CONCORDANCE_ALIGNER_THREADS` | torch default | CPU threads per job |
| `CONCORDANCE_ALIGNER_CPU_SHARES` | `1024` | Raise (e.g. `26192`) to outrank other containers |
| `CONCORDANCE_ALIGN_DEADLINE` | none | `HH:MM`; no alignment job starts after this time, or if it wouldn't finish by it |
| `CONCORDANCE_ALIGN_MEMORY_WAIT` | `60` | Minutes to wait for free memory before stopping the run |
| `CONCORDANCE_STATE_DIR` | `~/.cache/concordance` | Cache, run logs and reports |
| `CONCORDANCE_PYTHON` | `python3` | Interpreter the cron wrappers use |
| `CONCORDANCE_TEST_CASES` | empty | Calibre ids for `concordance --test-cases` |

## Development

```bash
python3 -m unittest discover -s tests -t .
```

The unit tests need nothing but Python. For live tests against your own server,
[contrib/sandbox](contrib/sandbox/README.md) sets progress on a dedicated test
book through the real APIs, checks the decisions and writes, and resets
afterwards. [contrib/experiments](contrib/experiments/README.md) holds the
research scripts behind the numbers in `docs/`.

## Limitations

- KOReader only. The native Kobo reader's sync path isn't supported.
- Chapter matching can be wrong. A book whose structures can't be matched at all
  (`unusable` in the report) falls back to a whole-book percentage, which is
  never written. A weak match (`low`) is still written, so check those books'
  reports before allowlisting them.
- One CWA user and one ABS user per configuration.
- Alignment runs on the CPU only, which is slow for long chapters.

## Licence

MIT, see [LICENSE](LICENSE). The default alignment model,
[MMS-300m](https://huggingface.co/MahmoudAshraf/mms-300m-1130-forced-aligner),
is licensed CC-BY-NC 4.0: fine for personal use, not for commercial use. It
isn't included in this repository or the image; it downloads on first run.
