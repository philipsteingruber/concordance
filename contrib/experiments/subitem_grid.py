"""Step 0 for sub-item anchoring: would a finer text grid actually help, and where?

The boundary matcher's text atom is the spine *file*. Misery's content spine is
9 items, so the dynamic program can never return more than 9 boundary pairs no
matter how good the audio chapter list gets - and `coverage` divides by
min(spine_count, chapter_count), so it reports a contented 1.0 while being as
coarse as it has ever been. `chapterdetect` already finds chapter starts inside
a spine item; this measures what feeding those to the matcher would buy.

Three grids are compared so the change can be attributed:

  coarse  today's live path: read_spine(), items with is_content
  medium  chapterdetect's item set (book_positions), no sub-cuts
  fine    that item set, cut at every detected chapter start

`medium` exists because the two modules disagree about which items count and in
what character space, and lumping that in with the sub-cuts would make the
result unreadable.

Accuracy is measured against forced-alignment cache entries, which give a real
audio time for a character offset. For books the chapters tool never touched
(463, 522, 581) that ground truth is independent of the grid being tested. For
Misery it is NOT: its ABS chapters were written from those same measurements,
so a good `fine` score there shows the matcher recovers the mapping, not that
the mapping is right. Both are reported, labelled.

Read-only. Needs CALIBRE_ROOT and ABS_DB; no credentials, no network.

  python3 contrib/experiments/subitem_grid.py --only 463,522,561,581
  python3 contrib/experiments/subitem_grid.py --in-progress
  python3 contrib/experiments/subitem_grid.py --all
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from concordance.absclient import AbsBook, Chapter
from concordance.anchor import build_alignment
from concordance.cache import AlignmentCache
from concordance.calibre import load_books, read_spine, spine_file
from concordance.chapterdetect import book_positions, detect, load_documents
from concordance.matching import normalise_title as norm

# Ground-truth words are only trusted where the aligner was confident, the same
# positional gate the live lookup uses. Misery's spine 14 opens with ~1500 words
# crammed at four times narration speed; without this the "truth" is fiction.
MIN_TRUTH_SCORE = -0.5
# Offsets sampled per cache entry. Enough for a stable p90, cheap enough to run
# over the whole library.
SAMPLES_PER_ENTRY = 200
# A book with more sub-items than this is reported and skipped: the matcher is
# O(n*m) with a 30x30 predecessor scan, and a 700-item grid takes minutes.
MAX_SUB_ITEMS = 400


@dataclass(frozen=True)
class SubItem:
    """A slice of a spine item, shaped so build_alignment can consume it.

    The matcher reads only `index`, `chars` and `is_content`, so a dataclass
    carrying provenance alongside them substitutes for SpineItem without the
    matcher needing to know. `index` is a synthetic sequential id, not a
    DocFragment number - `spine` keeps the real one.
    """

    index: int
    spine: int
    offset: int          # start offset within the spine item, canonical text space
    chars: int

    @property
    def is_content(self) -> bool:
        # The grid is already the content decision: every slice here came from
        # book_positions (narrated) or a detected chapter start.
        return True


def grids(book_file: Path):
    """(medium, fine, docs, positions) for a book, or None if it can't be read."""
    try:
        docs = load_documents(book_file)
    except Exception as exc:                       # noqa: BLE001 - research script
        return None, None, None, f"documents: {type(exc).__name__}: {exc}"
    positions = book_positions(docs)
    order = [i for i in sorted(positions) if i != -1]
    if not order:
        return None, None, None, "no narrated items"

    medium, n = [], 0
    for spine in order:
        length = len(docs[spine].text)
        if length <= 0:
            continue
        n += 1
        medium.append(SubItem(n, spine, 0, length))

    try:
        starts = detect(book_file, docs)
    except Exception as exc:                       # noqa: BLE001
        return medium, None, docs, f"detect: {type(exc).__name__}: {exc}"

    by_spine: dict[int, set[int]] = {}
    for s in starts:
        by_spine.setdefault(s.spine, set()).add(s.offset)

    fine, n = [], 0
    for spine in order:
        length = len(docs[spine].text)
        if length <= 0:
            continue
        cuts = sorted({0} | {o for o in by_spine.get(spine, set()) if 0 < o < length})
        for k, start in enumerate(cuts):
            end = cuts[k + 1] if k + 1 < len(cuts) else length
            if end <= start:
                continue
            n += 1
            fine.append(SubItem(n, spine, start, end - start))
    return medium, fine, docs, None


def char_space_divergence(spine, docs) -> tuple[float, float] | None:
    """(median, max) |read_spine chars - len(Document.text)| / Document chars, per item.

    read_spine strips tags and collapses whitespace; Document.text is the
    canonical text the XPointer parser and chapterdetect use. A chapter start's
    offset is in the second space and a SpineItem's length in the first, so
    cutting one at the other's offsets is only sound if these agree.
    """
    rel = []
    for item in spine:
        doc = docs.get(item.index)
        if doc is None or not doc.text:
            continue
        rel.append(abs(item.chars - len(doc.text)) / len(doc.text))
    if not rel:
        return None
    return statistics.median(rel), max(rel)


def locate(grid: list[SubItem], spine: int, offset: int) -> SubItem | None:
    """The slice of `spine` containing `offset`."""
    best = None
    for item in grid:
        if item.spine == spine and item.offset <= offset < item.offset + item.chars:
            return item
        if item.spine == spine and best is None:
            best = item
    return best


def audio_time(alignment, index: int, fraction: float) -> float | None:
    """Where an alignment puts a position, interpolating inside its matched span.

    This is translate.ebook_to_audio's interpolated tier, reduced to the part
    under test: pick the point for the item, then place proportionally in its
    audio span.
    """
    point = next((p for p in alignment.points if p.spine_index == index), None)
    if point is None:
        return None
    return point.audio_start + fraction * (point.audio_end - point.audio_start)


def truth_samples(entry, limit: int = SAMPLES_PER_ENTRY):
    """[(spine, offset in item, true seconds)] from a cache entry's confident words."""
    if not entry.words:
        return []
    starts = {spine: start for spine, start, _ in entry.items}
    lengths = {spine: length for spine, _, length in entry.items}
    step = max(1, len(entry.words) // limit)
    out = []
    for w in entry.words[::step]:
        char, t = w[0], w[2]
        score = entry.mean_score(t)
        if score is None or score < MIN_TRUTH_SCORE:
            continue
        spine = None
        for s, start, length in reversed(entry.items):
            if char >= start:
                spine = s
                break
        if spine is None:
            continue
        offset = char - starts[spine]
        if not (0 <= offset < lengths[spine]):
            continue
        out.append((spine, offset, t))
    return out


def errors(alignment, grid, docs, samples, use_grid: bool) -> list[float]:
    """|estimated - true| seconds at each sample, under one grid."""
    out = []
    for spine, offset, truth in samples:
        if use_grid:
            item = locate(grid, spine, offset)
            if item is None or item.chars <= 0:
                continue
            fraction = min(1.0, max(0.0, (offset - item.offset) / item.chars))
            est = audio_time(alignment, item.index, fraction)
        else:
            doc = docs.get(spine)
            if doc is None or not doc.text:
                continue
            fraction = min(1.0, max(0.0, offset / len(doc.text)))
            est = audio_time(alignment, spine, fraction)
        if est is not None:
            out.append(abs(est - truth))
    return out


def summarise(values: list[float]) -> str:
    if not values:
        return "     n/a"
    values = sorted(values)
    p90 = values[int(0.9 * (len(values) - 1))]
    return f"{statistics.median(values):6.0f}s p90 {p90:5.0f}s max {values[-1]:5.0f}s"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated Calibre ids")
    ap.add_argument("--in-progress", action="store_true", help="books ABS has in progress")
    ap.add_argument("--all", action="store_true", help="every paired book")
    ap.add_argument("--calibre-root", default=os.environ.get("CALIBRE_ROOT"))
    ap.add_argument("--abs-db", default=os.environ.get("ABS_DB"))
    args = ap.parse_args(argv)

    if not args.calibre_root or not args.abs_db:
        print("need --calibre-root and --abs-db (or CALIBRE_ROOT / ABS_DB)", file=sys.stderr)
        return 2
    only = {int(x) for x in (args.only or "").replace(" ", "").split(",") if x.isdigit()}
    if not only and not args.in_progress and not args.all:
        print("pick a scope: --only, --in-progress or --all", file=sys.stderr)
        return 2

    root = args.calibre_root.rstrip("/")
    books = load_books(os.environ.get("CALIBRE_DB") or f"{root}/metadata.db", root)
    by_title = {}
    for b in books.values():
        by_title.setdefault(norm(b.title), b)

    db = sqlite3.connect(f"file:{args.abs_db}?mode=ro", uri=True)
    in_progress = {r[0] for r in db.execute(
        "SELECT mediaItemId FROM mediaProgresses WHERE currentTime>0 AND isFinished=0")}
    cache = AlignmentCache()

    print(f"{'book':<30} {'spine':>5} {'chap':>5} "
          f"{'coarse grp/conf':>18} {'medium':>13} {'fine grp/conf':>18}  chars")
    rows, accuracy = [], []
    for media_id, title, chapters_json, duration in db.execute(
            "SELECT b.id,b.title,b.chapters,b.duration FROM books b "
            "JOIN libraryItems li ON li.mediaId=b.id WHERE li.mediaType='book'"):
        book = by_title.get(norm(title))
        if not book:
            continue
        if only and book.book_id not in only:
            continue
        if args.in_progress and not only and media_id not in in_progress:
            continue

        entries = cache.entries_for(book.book_id)
        fmt = entries[0].key.fmt if entries else None
        found = spine_file(book, fmt)
        if not found:
            continue
        book_file = found[1]

        try:
            spine = read_spine(book_file)
        except RuntimeError:
            continue
        chapters = AbsBook("x", title, "", duration or 0, [
            Chapter(i, c.get("title", ""), float(c["start"]), float(c["end"]))
            for i, c in enumerate(json.loads(chapters_json or "[]"), 1)
        ]).substantive_chapters()
        if not chapters:
            continue

        medium, fine, docs, err = grids(book_file)
        if docs is None:
            print(f"{title[:30]:<30} {err}")
            continue

        coarse_al = build_alignment(spine, chapters)
        started = time.monotonic()
        medium_al = build_alignment(medium, chapters) if medium else None
        fine_al = (build_alignment(fine, chapters)
                   if fine and len(fine) <= MAX_SUB_ITEMS else None)
        elapsed = time.monotonic() - started

        div = char_space_divergence([s for s in spine if s.is_content], docs)
        div_s = f"{100 * div[0]:.2f}%/{100 * div[1]:.1f}%" if div else "n/a"
        note = ""
        if fine and len(fine) > MAX_SUB_ITEMS:
            note = f"  (fine grid {len(fine)} items, skipped)"
        if err:
            note += f"  ({err})"

        def cell(al):
            return f"{len(al.groups):>3}/{al.confidence:<8}" if al else "   -/-      "

        print(f"{title[:30]:<30} {coarse_al.spine_count:>5} {len(chapters):>5} "
              f"{cell(coarse_al):>18} {cell(medium_al):>13} {cell(fine_al):>18}  {div_s}{note}")
        rows.append((title, coarse_al, medium_al, fine_al, elapsed))

        # Accuracy, where a cache entry gives a real time for a character offset.
        samples = []
        for e in entries:
            if e.key.fmt != (fmt or e.key.fmt):
                continue
            samples.extend(truth_samples(e))
        if samples and fine_al:
            accuracy.append((
                title, len(samples),
                errors(coarse_al, None, docs, samples, use_grid=False),
                errors(fine_al, fine, docs, samples, use_grid=True),
            ))

    if rows:
        slowest = max(rows, key=lambda r: r[4])
        print(f"\n{len(rows)} books. Slowest match: {slowest[0][:40]} at {slowest[4]:.1f}s.")

    if accuracy:
        print("\nAccuracy against forced-alignment ground truth "
              f"(words scoring >= {MIN_TRUTH_SCORE}):")
        print(f"  {'book':<30} {'n':>5}  {'coarse (live today)':<28} fine")
        for title, n, coarse_err, fine_err in accuracy:
            print(f"  {title[:30]:<30} {n:>5}  {summarise(coarse_err):<28} {summarise(fine_err)}")
        print("\n  Independent for books the chapters tool never split. For Misery and"
              "\n  The Burn Palace the ABS chapters were written from these same"
              "\n  measurements, so `fine` there shows the matcher recovers the mapping,"
              "\n  not that the mapping is correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
