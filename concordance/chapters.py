"""Split Audiobookshelf "chapters" that really contain several of the book's chapters.

Some audiobook releases mark 70-minute slices of the recording as chapters
("Chapter 1" … "Chapter 10") while the book has dozens. This finds the real
chapter starts in the ebook, locates them in the audio, and replaces only the
ABS chapters that contain two or more of them. Meant for ad hoc use, one book at
a time:

    concordance-chapters check   <calibre id | title>   # instant: is this book affected?
    concordance-chapters propose <book>                 # find the times; writes nothing
    concordance-chapters apply   <book>                 # back up ABS's list, write the proposal
    concordance-chapters restore <book>                 # put the latest backup back

Times come from the alignment cache where Concordance has already aligned the
chapter (exact, free), otherwise from short alignments of each chapter's opening
words in the aligner container (about a minute of CPU per chapter).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .absclient import AbsClient, Chapter
from .cache import AlignmentCache, manifest_fingerprint
from .calibre import load_books, spine_file
from .chapterdetect import ChapterStart, book_positions, detect, load_documents
from .config import Config, ConfigError, ServiceUnavailable, env_number
from .matching import load_calibre_isbns, match_pairs, normalise_title
from .orchestrate import docker_options, state_dir

MIN_STARTS = 2               # an ABS chapter is split only if it holds at least this many real starts
MIN_RATIO = 1.5              # ...and the book has at least this many real chapters per ABS chapter
MAX_GAP = 3                  # unsplit ABS chapters between split ones are rebuilt too, up to this many
# A chapter mark shorter than this is not worth having: Misery's Part Three
# alternates between Paul's narrative and the manuscript he is typing and numbers
# each switch, so starts land as little as four seconds apart. This is a
# usability floor, not a correctness guard - `chapterprobe` rejects a start at or
# before the previous one whatever this is set to. Measured in audio seconds
# rather than characters because characters per second varies by book, and it is
# the duration that decides whether a listener can navigate by the mark.
MIN_CHAPTER_SECONDS = env_number("CONCORDANCE_MIN_CHAPTER_SECONDS", 60.0, float)
SNAP_SECONDS = 20.0          # a real start this close to a region's start replaces it
LEAD_SECONDS = 0.5           # chapter marks sit this far before the first spoken word
# Last-ditch collapse of confirmed marks landing almost on top of each other, applied
# when the new chapter list is built. Distinct from MIN_CHAPTER_SECONDS above, which
# decides which detected starts are worth locating in the first place.
MIN_MARK_GAP_SECONDS = 5.0
OPENING_WORDS = 30
RECENT_LISTENING = timedelta(hours=24)


@dataclass
class Book:
    calibre_id: int
    title: str
    item_id: str
    fmt: str
    book_file: Path
    docs: dict
    starts: list[ChapterStart]
    positions: dict[int, int]
    chapters: list[Chapter]
    manifest: list[tuple[str, float]]

    @property
    def duration(self) -> float:
        return sum(d for _, d in self.manifest)

    def pos(self, start: ChapterStart) -> int:
        return self.positions[start.spine] + start.offset


# --- pure planning -------------------------------------------------------------------


def estimate_times(book: Book) -> list[float]:
    """Proportional first guess at each start's time (good to a few minutes)."""
    total = book.positions[-1] or 1
    return [book.pos(s) / total * book.duration for s in book.starts]


def regions(chapters: list[Chapter], times: list[float]) -> list[tuple[int, int]]:
    """Runs of consecutive ABS chapter indexes (0-based, inclusive) that each hold MIN_STARTS+ starts.

    Only for books with clearly more real chapters than ABS chapters: the estimates
    are proportional, so in a book whose chapters already match, a start near a
    boundary lands in the neighbouring chapter and would look like a split.
    """
    if not chapters or len(times) < MIN_RATIO * len(chapters):
        return []
    counts = [sum(c.start <= t < c.end for t in times) for c in chapters]
    out: list[tuple[int, int]] = []
    for i, n in enumerate(counts):
        if n < MIN_STARTS:
            continue
        # A short run of unsplit chapters between split ones is just as arbitrary
        # (release slices that happened to hold one chapter start); rebuild it too,
        # or its boundaries would cut the real chapters on either side.
        if out and i - out[-1][1] - 1 <= MAX_GAP:
            out[-1] = (out[-1][0], i)
        else:
            out.append((i, i))
    return out


def build_chapters(chapters: list[Chapter], spans: list[tuple[int, int]],
                   found: list[tuple[float, str]], carried: dict[int, str] | None = None) -> list[dict]:
    """The new ABS chapter list: untouched chapters as they are, split regions rebuilt.

    `found` is (time, title) for confirmed real starts. Inside a region, the stretch
    before its first real start keeps the title of the chapter already in progress
    (`carried[region index]`), or the original ABS title.
    """
    out: list[dict] = []
    span_at = {first: (first, last) for first, last in spans}
    i = 0
    while i < len(chapters):
        if i not in span_at:
            c = chapters[i]
            out.append({"title": c.title, "start": c.start, "end": c.end})
            i += 1
            continue
        first, last = span_at[i]
        start, end = chapters[first].start, chapters[last].end
        inside = sorted((t, title) for t, title in found if start <= t < end)
        marks: list[tuple[float, str]] = []
        for t, title in inside:
            t = max(start, t - LEAD_SECONDS)
            if marks and t - marks[-1][0] < MIN_MARK_GAP_SECONDS:
                continue
            marks.append((t, title))
        if not marks:
            out.extend({"title": c.title, "start": c.start, "end": c.end} for c in chapters[first:last + 1])
        else:
            if marks[0][0] - start <= SNAP_SECONDS:
                marks[0] = (start, marks[0][1])
            else:
                lead = (carried or {}).get(first) or chapters[first].title
                marks.insert(0, (start, lead))
            for k, (t, title) in enumerate(marks):
                out.append({"title": title, "start": round(t, 3),
                            "end": round(marks[k + 1][0], 3) if k + 1 < len(marks) else end})
        i = last + 1
    return out


# --- loading -------------------------------------------------------------------------


def find_book(cfg: Config, abs_client: AbsClient, query: str) -> Book:
    books = load_books(cfg.calibre_db, cfg.calibre_root)
    pairs, _ = match_pairs(books, abs_client.books(), load_calibre_isbns(cfg.calibre_db))
    if query.isdigit():
        hits = [p for p in pairs if p.calibre.book_id == int(query)]
    else:
        key = normalise_title(query)
        hits = [p for p in pairs if key and key in normalise_title(p.calibre.title)]
    if not hits:
        raise ConfigError(f"no ebook+audiobook pair matches {query!r}")
    if len(hits) > 1:
        names = ", ".join(f"{p.calibre.title} ({p.calibre.book_id})" for p in hits[:8])
        raise ConfigError(f"{query!r} matches several books: {names}. Use the Calibre id.")
    pair = hits[0]
    chosen = spine_file(pair.calibre, None)
    if chosen is None:
        raise ConfigError(f"{pair.calibre.title} has no EPUB or KEPUB")
    fmt, book_file = chosen
    docs = load_documents(book_file)
    item = pair.audiobook.library_item_id
    positions = book_positions(docs)
    manifest = abs_client.audio_manifest(item)
    duration = sum(d for _, d in manifest)
    return Book(pair.calibre.book_id, pair.calibre.title, item, fmt, book_file, docs,
                drop_crowded(detect(book_file, docs), positions, positions[-1], duration),
                positions, abs_client.chapters(item), manifest)


def drop_crowded(starts: list[ChapterStart], positions: dict[int, int],
                 total_chars: int, duration: float) -> list[ChapterStart]:
    """Keep the first of any run of starts less than MIN_CHAPTER_SECONDS apart.

    The book's own average characters per second converts the floor, so a densely
    set book and an airy one get the same treatment in the units a listener cares
    about.
    """
    if total_chars <= 0 or duration <= 0:
        return list(starts)
    floor = MIN_CHAPTER_SECONDS * total_chars / duration
    kept: list[ChapterStart] = []
    last: int | None = None
    for start in starts:
        if start.spine not in positions:
            continue
        here = positions[start.spine] + start.offset
        if last is None or here - last >= floor:
            kept.append(start)
            last = here
    return kept


def _fresh_entries(cfg: Config, book: Book) -> list:
    """This book's cache entries that still match the files on disk."""
    abs_root, host_root = cfg.abs_audio_root or ("", "")
    try:
        audio_print = manifest_fingerprint([Path(p.replace(abs_root, host_root, 1)) for p, _ in book.manifest])
    except OSError:
        return []
    out = []
    for entry in AlignmentCache().entries_for(book.calibre_id):
        if entry.key.fmt != book.fmt or entry.key.library_item_id != book.item_id:
            continue
        if entry.is_fresh(book.book_file, audio_print, entry.aligner):
            out.append(entry)
    return out


def cached_times(cfg: Config, book: Book) -> dict[int, float]:
    """Start index -> time, for starts inside a fresh, trusted alignment cache entry."""
    out: dict[int, float] = {}
    for entry in _fresh_entries(cfg, book):
        # Whether the entry aligned at all is a property of the entry. Asking
        # instead for the mean score in a window around the looked-up time
        # rejected correct answers at every item boundary: a chapter starting at
        # offset 0 scores its one-sided opening window, and a heading the
        # narrator doesn't read sinks it. Misery's "Part Two, Chapter 15" sat at
        # spine 14 offset 0 with a true time in the cache and scored -8.99 there,
        # so it was sent to the aligner, which then searched the wrong place.
        overall = entry.overall_score()
        if overall is None or overall < cfg.min_align_score:
            continue
        spines = {item[0]: item[2] for item in entry.items}
        for k, start in enumerate(book.starts):
            if start.spine in spines and start.offset < spines[start.spine]:
                out[k] = entry.time_for(start.spine, start.offset)[0]
    return out


def entry_anchors(cfg: Config, book: Book) -> list[tuple[int, float]]:
    """Both ends of every aligned spine item, as (book character, seconds).

    Estimates interpolate the character-to-time rate between anchors, which is
    only valid where the narration runs continuously. Between two spine items it
    often does not: Misery has 416 seconds of part announcement between spine 13
    and spine 14 that carry no book text, and interpolating across it put the
    estimate for the chapter at the start of spine 14 392 seconds early - far
    enough that the search window never contained the answer. Pinning both ends
    of each aligned item keeps every interpolation inside continuous narration.
    """
    out: list[tuple[int, float]] = []
    for entry in _fresh_entries(cfg, book):
        for spine, first, first_t, last, last_t in entry.item_bounds():
            if spine in book.positions:
                out.append((book.positions[spine] + first, first_t))
                out.append((book.positions[spine] + last, last_t))
    return out


def _found_file(book: Book) -> Path:
    return chapter_dir() / f"{book.item_id}-found.json"


def remembered_times(book: Book) -> dict[int, float]:
    """Start index -> time confirmed by an earlier `propose` of the same ebook file."""
    path = _found_file(book)
    if not path.exists():
        return {}
    saved = json.loads(path.read_text())
    stat = book.book_file.stat()
    if saved.get("book_size") != stat.st_size or saved.get("book_mtime") != int(stat.st_mtime):
        return {}
    times = {int(pos): t for pos, t in saved.get("times", {}).items()}
    return {k: times[book.pos(s)] for k, s in enumerate(book.starts) if book.pos(s) in times}


def remember_times(book: Book, found: dict[int, float]) -> None:
    stat = book.book_file.stat()
    path = _found_file(book)
    old = json.loads(path.read_text()).get("times", {}) if path.exists() else {}
    old.update({str(book.pos(book.starts[k])): t for k, t in found.items()})
    path.write_text(json.dumps({"book_size": stat.st_size, "book_mtime": int(stat.st_mtime), "times": old}))


def opening_text(book: Book, start: ChapterStart) -> str:
    return " ".join(book.docs[start.spine].text[start.offset:].split()[:OPENING_WORDS])


def chapter_dir() -> Path:
    path = state_dir() / "chapters"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- commands ------------------------------------------------------------------------


def _fmt(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def check(cfg: Config, abs_client: AbsClient, query: str) -> int:
    book = find_book(cfg, abs_client, query)
    times = estimate_times(book)
    sources = {s.source for s in book.starts}
    print(f"{book.title} (Calibre {book.calibre_id}, ABS {book.item_id})")
    print(f"ABS: {len(book.chapters)} chapters over {_fmt(book.duration)}; "
          f"ebook: {len(book.starts)} chapter starts (from {', '.join(sorted(sources))})")
    spans = regions(book.chapters, times)
    split = {i for first, last in spans for i in range(first, last + 1)}
    for i, c in enumerate(book.chapters):
        n = sum(c.start <= t < c.end for t in times)
        flag = "  <- split" if i in split else ""
        print(f"  {c.title[:40]:40} {_fmt(c.start)}–{_fmt(c.end)}  ~{n} real chapter starts{flag}")
    if not spans:
        print(f"Nothing to split: the book needs at least {MIN_RATIO:g}x as many real chapters as ABS "
              f"chapters, with two or more inside one ABS chapter.")
    else:
        print(f"{len(split)} ABS chapters would be split. Estimates are proportional; "
              f"`propose` finds the exact times.")
    return 0


def propose(cfg: Config, abs_client: AbsClient, query: str) -> int:
    book = find_book(cfg, abs_client, query)
    times = estimate_times(book)
    spans = regions(book.chapters, times)
    if not spans:
        print(f"{book.title}: no ABS chapter holds several real chapters; nothing to propose.")
        return 0
    lo = {first: book.chapters[first].start for first, _ in spans}
    in_scope = [k for k, t in enumerate(times)
                if any(book.chapters[f].start - 300 <= t < book.chapters[l].end + 300 for f, l in spans)]
    remembered = remembered_times(book)
    cached = {**remembered, **cached_times(cfg, book)}
    anchors = [(0, 0.0), (book.positions[-1], book.duration)]
    anchors += [(book.pos(book.starts[k]), t) for k, t in cached.items()]
    anchors += entry_anchors(cfg, book)
    probe = [k for k in in_scope if k not in cached]
    print(f"{book.title}: {len(in_scope)} chapter starts in {len(spans)} region(s); "
          f"{len(in_scope) - len(probe)} already known (alignment cache or an earlier propose), "
          f"{len(probe)} to locate.", flush=True)

    results: dict[int, dict] = {k: {"status": "cached", "time": cached[k]} for k in in_scope if k in cached}
    if probe:
        job = {"manifest": book.manifest, "anchors": sorted(anchors), "min_score": cfg.min_align_score,
               "threads": int(os.environ.get("CONCORDANCE_ALIGNER_THREADS", "0") or 0),
               "targets": [{"id": k, "pos": book.pos(book.starts[k]), "text": opening_text(book, book.starts[k])}
                           for k in probe]}
        work = state_dir() / "work" / "chapters"
        work.mkdir(parents=True, exist_ok=True)
        job_file = work / f"{book.item_id}.json"
        job_file.write_text(json.dumps(job))
        cmd = ["docker", *docker_options(cfg, state_dir()), "--entrypoint", "python", cfg.aligner_image,
               "-m", "concordance.chapterprobe", f"/cache/work/chapters/{job_file.name}"]
        started = time.time()
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            for line in proc.stdout:
                if not line.startswith("{"):
                    continue
                r = json.loads(line)
                results[r["id"]] = r
                s = book.starts[r["id"]]
                where = _fmt(r["time"]) if r.get("time") is not None else "-"
                print(f"  {len(results):3}/{len(in_scope)}  {s.title[:36]:36} {r['status']:11} {where}  "
                      f"score {r.get('score', 0):6.2f}  ({r.get('seconds', 0)} s)", flush=True)
            err = proc.stderr.read()
        if proc.returncode != 0:
            print(f"error: chapter probe failed (exit {proc.returncode}): {err.strip()[-500:]}", file=sys.stderr)
            return 1
        print(f"Located in {_fmt(time.time() - started)}.")
        remember_times(book, {k: r["time"] for k, r in results.items()
                              if r.get("status") == "confirmed" and r.get("time") is not None})

    found = [(r["time"], book.starts[k].title) for k, r in results.items() if r.get("time") is not None]
    carried = {}
    for first, _ in spans:
        before = [book.starts[k].title for k, t in enumerate(times) if t < lo[first]]
        carried[first] = f"{before[-1]} (continued)" if before else None
    after = build_chapters(book.chapters, spans, found, carried)
    unconfirmed = [book.starts[k].title for k, r in results.items() if r["status"] == "unconfirmed"]
    proposal = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "calibre_id": book.calibre_id, "title": book.title, "item_id": book.item_id,
                "before": [dataclasses.asdict(c) for c in book.chapters], "after": after,
                "unconfirmed": unconfirmed}
    path = chapter_dir() / f"{book.item_id}-proposal.json"
    path.write_text(json.dumps(proposal, indent=1))

    print(f"\nProposed: {len(book.chapters)} -> {len(after)} chapters")
    for c in after:
        print(f"  {_fmt(c['start'])}  {c['title']}")
    if unconfirmed:
        print(f"Not found, left out ({len(unconfirmed)}): {', '.join(unconfirmed[:10])}")
    print(f"Saved to {path}. Review, then `concordance-chapters apply {book.calibre_id}`.")
    return 0


def renumber_cache(cfg: Config, book: Book, new_chapters: list[dict]) -> list[str]:
    """Rename alignment cache entries to the new ABS chapter numbers.

    A cache entry's filename carries the ABS chapters its audio span covered
    (`kepub-sp10-10-ch1-3.json.gz`). Splitting a book's chapters renumbers them, so
    the nightly planner would look for a name that no longer exists and align the
    same audio again. The audio span and spine range are unchanged, so the entry is
    still valid: only its name needs to follow. Returns the renamed files.
    """
    numbered = [(i, float(c["start"]), float(c["end"]))
                for i, c in enumerate(new_chapters, start=1)
                if float(c["end"]) - float(c["start"]) >= 60.0]

    def chapter_at(t: float) -> int | None:
        return next((i for i, start, end in numbered if start <= t < end), None)

    cache = AlignmentCache()
    renamed = []
    for entry in cache.entries_for(book.calibre_id):
        if entry.key.library_item_id != book.item_id:
            continue
        first = chapter_at(entry.audio_start + 0.5)
        last = chapter_at(max(entry.audio_start, entry.audio_end - 0.5))
        if first is None or last is None:
            continue
        new_key = dataclasses.replace(entry.key, first_chapter=first, last_chapter=last)
        if new_key.filename() == entry.key.filename():
            continue
        old_path = cache.path(entry.key)
        cache.save(dataclasses.replace(entry, key=new_key))
        old_path.unlink(missing_ok=True)
        renamed.append(f"{entry.key.filename()} -> {new_key.filename()}")
    return renamed


def _same(chapters: list[Chapter], saved: list[dict]) -> bool:
    return len(chapters) == len(saved) and all(
        c.title == s["title"] and abs(c.start - s["start"]) < 0.01 and abs(c.end - s["end"]) < 0.01
        for c, s in zip(chapters, saved))


def _refuse_if_listening(abs_client: AbsClient, item_id: str, force: bool) -> bool:
    progress = abs_client.progress().get(item_id)
    if progress and progress.last_update and not force:
        if datetime.now(timezone.utc) - progress.last_update < RECENT_LISTENING:
            print("error: this book was listened to in the last 24 hours; changing its chapters under an "
                  "open player is avoidable risk. Try later, or pass --force.", file=sys.stderr)
            return True
    return False


def _write(abs_client: AbsClient, item_id: str, current: list[Chapter], new: list[dict], label: str,
           renamed: list[str] | None = None) -> int:
    backup = chapter_dir() / f"{item_id}-backup-{datetime.now():%Y%m%dT%H%M%S}.json"
    backup.write_text(json.dumps([dataclasses.asdict(c) for c in current], indent=1))
    reply = abs_client.update_chapters(item_id, new)
    written = abs_client.chapters(item_id)
    for line in renamed or []:
        print(f"  cache entry renamed: {line}")
    if len(written) != len(new):
        print(f"error: ABS now reports {len(written)} chapters, expected {len(new)}. "
              f"Backup: {backup}", file=sys.stderr)
        return 1
    print(f"{label}: {len(current)} -> {len(written)} chapters (ABS updated={reply.get('updated')}). "
          f"Previous list backed up to {backup}")
    return 0


def apply(cfg: Config, abs_client: AbsClient, query: str, force: bool) -> int:
    book = find_book(cfg, abs_client, query)
    path = chapter_dir() / f"{book.item_id}-proposal.json"
    if not path.exists():
        print(f"error: no proposal for {book.title}; run `propose` first", file=sys.stderr)
        return 1
    proposal = json.loads(path.read_text())
    if not _same(book.chapters, proposal["before"]):
        print("error: ABS's chapters changed since the proposal was made; run `propose` again", file=sys.stderr)
        return 1
    if _refuse_if_listening(abs_client, book.item_id, force):
        return 1
    renamed = renumber_cache(cfg, book, proposal["after"])
    return _write(abs_client, book.item_id, book.chapters, proposal["after"], book.title, renamed)


def restore(cfg: Config, abs_client: AbsClient, query: str, force: bool) -> int:
    book = find_book(cfg, abs_client, query)
    backups = sorted(chapter_dir().glob(f"{book.item_id}-backup-*.json"))
    if not backups:
        print(f"error: no backup for {book.title}", file=sys.stderr)
        return 1
    if _refuse_if_listening(abs_client, book.item_id, force):
        return 1
    saved = json.loads(backups[-1].read_text())
    print(f"Restoring {backups[-1].name}")
    renamed = renumber_cache(cfg, book, saved)
    return _write(abs_client, book.item_id, book.chapters, saved, f"{book.title} restored", renamed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="concordance-chapters",
                                 description="Split ABS chapters that contain several of the book's chapters.")
    ap.add_argument("action", choices=("check", "propose", "apply", "restore"))
    ap.add_argument("book", help="Calibre id or part of the title")
    ap.add_argument("--force", action="store_true", help="apply/restore even if listened to in the last 24 h")
    args = ap.parse_args(argv)
    try:
        cfg = Config.from_env()
        client = AbsClient(cfg.abs_url, cfg.abs_api_key)
        if args.action == "check":
            return check(cfg, client, args.book)
        if args.action == "propose":
            return propose(cfg, client, args.book)
        if args.action == "apply":
            return apply(cfg, client, args.book, args.force)
        return restore(cfg, client, args.book, args.force)
    except ServiceUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (ConfigError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
