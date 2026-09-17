"""Command-line entry point.

Dry run by default: decisions and planned writes are reported, nothing is sent.
`--apply` writes only for books in the write allowlist (allowlist.py), and only
for precise-enough tiers (see writer.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import dataclasses

from .absclient import AbsClient
from .anchor import build_alignment
from .calibre import load_books, read_spine, spine_file
from .config import Config, ConfigError
from .cwa import CwaClient
from .matching import load_calibre_isbns, match_pairs
from .report import PairReport, Report, render_table, write_artifacts
from .cache import AlignmentCache, manifest_fingerprint
from .decide import decide
from .writer import WritePolicy, gate, plan_abs_write, plan_cwa_write
from .xpointer import XPointerError, resolve_any

def test_case_ids() -> set[int]:
    """Calibre ids from CONCORDANCE_TEST_CASES: books picked to span your library's
    structures (few long chapters, many short ones, tracks split across chapters,
    a hand-checked control) so a matcher regression shows up somewhere."""
    raw = os.environ.get("CONCORDANCE_TEST_CASES", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}


def _aligned_positions(cfg, cache, abs_client, pair, located, listening):
    """Look up exact positions in a fresh forced-alignment cache entry, if one exists.

    Returns (audio seconds for the ebook position, (spine, fraction) for the audio
    position); either may be None. Entries are ignored when the book or audio
    files have changed since alignment, or when the aligner's local match score
    is below the configured threshold.
    """
    entries = [e for e in cache.entries_for(pair.calibre.book_id)
               if e.key.library_item_id == pair.audiobook.library_item_id]
    if not entries:
        return None, None
    try:
        abs_root, host_root = cfg.abs_audio_root or ("", "")
        host_paths = [Path(path.replace(abs_root, host_root, 1))
                      for path, _ in abs_client.audio_manifest(pair.audiobook.library_item_id)]
        audio_print = manifest_fingerprint(host_paths)
    except (OSError, RuntimeError, ValueError):
        return None, None

    def fresh(entry):
        book = sorted(pair.calibre.path.glob(f"*.{entry.key.fmt}"))
        return bool(book) and entry.is_fresh(book[0], audio_print, entry.aligner)

    fresh_entries = [e for e in entries if fresh(e)]
    ebook_time = audio_fraction = None
    if located is not None:
        for e in fresh_entries:
            if e.key.fmt == located.fmt and any(i[0] == located.fragment for i in e.items):
                seconds, _ = e.time_for(located.fragment, located.offset)
                score = e.mean_score(seconds)
                if score is not None and score >= cfg.min_align_score:
                    ebook_time = seconds
                break
    if listening is not None:
        for e in fresh_entries:
            if e.audio_start <= listening.current_time < e.audio_end:
                spine, offset, _ = e.position_for(listening.current_time)
                score = e.mean_score(listening.current_time)
                length = next((i[2] for i in e.items if i[0] == spine), 0)
                if score is not None and score >= cfg.min_align_score and length:
                    audio_fraction = (spine, offset / length)
                break
    return ebook_time, audio_fraction


def build_report(cfg: Config, only: set[int] | None, progress_only: bool,
                 policy: WritePolicy | None = None) -> Report:
    policy = policy or WritePolicy()
    cwa = CwaClient(cfg.cwa_url, cfg.cwa_user, cfg.cwa_password)
    if not cwa.check_auth():
        raise RuntimeError("CWA rejected the app password (check CWA_USER / CWA_APP_PASSWORD)")

    abs_client = AbsClient(cfg.abs_url, cfg.abs_api_key)
    calibre_books = load_books(cfg.calibre_db, cfg.calibre_root)
    audiobooks = abs_client.books()
    abs_progress = abs_client.progress()
    cwa_progress = {p.calibre_book_id: p for p in cwa.export()}

    pairs, unmatched = match_pairs(calibre_books, audiobooks, load_calibre_isbns(cfg.calibre_db))

    cache = AlignmentCache()
    report = Report(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dry_run=not policy.apply,
        rewind_seconds=cfg.rewind_seconds,
        unmatched_audiobooks=[a.title for a in unmatched],
    )

    for pair in pairs:
        book_id = pair.calibre.book_id
        if only and book_id not in only:
            continue
        progress = cwa_progress.get(book_id)
        listening = abs_progress.get(pair.audiobook.library_item_id)
        if progress_only and not progress and not listening:
            continue

        entry = PairReport(
            calibre_book_id=book_id,
            library_item_id=pair.audiobook.library_item_id,
            title=pair.calibre.title,
            match_method=pair.method,
            match_score=pair.score,
            author_agrees=pair.author_agrees,
            spine_items=0,
            chapters=0,
            median_chapter_minutes=0.0,
            max_chapter_minutes=0.0,
            confidence="unusable",
            needs_alignment=True,
            worst_discontinuity_minutes=0.0,
            discontinuity_at=None,
        )

        if listening:
            entry.abs_current_time = round(listening.current_time, 1)
            entry.abs_is_finished = listening.is_finished
        if progress:
            # export carries no locator; fetch it for this book only.
            if progress.xpointer is None:
                located = cwa.locator(book_id)
                if located:
                    progress = dataclasses.replace(progress, xpointer=located)
            entry.cwa_percentage = round(progress.percentage, 2)
            entry.cwa_spine_index = progress.spine_index

        # Resolve the full XPointer against the book's files, so the ebook position
        # is a real offset inside its spine item rather than the item's start. The
        # path only fits the format the reader had open (KEPUB paths carry
        # koboSpan wrappers), which also tells us which file the Kobo is reading.
        item_fraction = None
        located = None
        if progress and progress.xpointer:
            candidates = {}
            for ext in ("kepub", "epub"):
                found = sorted(pair.calibre.path.glob(f"*.{ext}"))
                if found:
                    candidates[ext] = found[0]
            try:
                located = resolve_any(progress.xpointer, candidates)
                entry.cwa_format = located.fmt
                if located.item_length > 0:
                    item_fraction = located.offset / located.item_length
                    entry.cwa_item_fraction = round(item_fraction, 4)
            except XPointerError as exc:
                entry.note = f"XPointer not resolved: {exc}"[:200]

        # Map chapters on the spine of the file the Kobo reads: kepubify inserts a
        # title-page dummy at spine 1 in some KEPUBs, which shifts every index by
        # one against the EPUB (about 2% of books in one 500-book library).
        book_file = spine_file(pair.calibre, located.fmt if located else None)
        if book_file is None:
            entry.error = "no EPUB/KEPUB file found in the Calibre folder"
            report.pairs.append(entry)
            continue

        try:
            spine = read_spine(book_file[1])
        except RuntimeError as exc:
            entry.error = str(exc)
            report.pairs.append(entry)
            continue

        # Chapters are absent from the library listing, so load them here —
        # one request per reported book rather than one per library item.
        try:
            chapters = abs_client.chapters(pair.audiobook.library_item_id)
        except RuntimeError as exc:
            entry.error = f"could not load chapters: {exc}"
            report.pairs.append(entry)
            continue
        audiobook = dataclasses.replace(pair.audiobook, chapters=chapters)

        alignment = build_alignment(spine, audiobook.substantive_chapters())
        entry.spine_items = alignment.spine_count
        entry.chapters = alignment.chapter_count
        entry.median_chapter_minutes = round(alignment.median_chapter_minutes, 2)
        entry.max_chapter_minutes = round(alignment.max_chapter_minutes, 2)
        entry.confidence = alignment.confidence
        entry.needs_alignment = alignment.needs_alignment
        entry.worst_discontinuity_minutes = round(alignment.worst_discontinuity_minutes, 2)
        entry.discontinuity_at = alignment.discontinuity_at


        aligned_ebook_time, aligned_audio_fraction = _aligned_positions(
            cfg, cache, abs_client, pair, located, listening)
        if aligned_ebook_time is not None or aligned_audio_fraction is not None:
            entry.note = (entry.note + " | " if entry.note else "") + "aligned cache hit"

        decision = decide(progress, listening, alignment, cfg.rewind_seconds, audiobook.duration,
                          item_fraction, aligned_ebook_time, aligned_audio_fraction)
        entry.direction = decision.direction
        entry.reason = decision.reason
        entry.tier = decision.tier
        if decision.reason and not entry.note:
            entry.note = decision.reason
        if decision.direction == "to_abs":
            entry.would_write_abs_finished = decision.abs_finished
            if decision.abs_seconds is not None:
                entry.would_write_seconds = round(decision.abs_seconds, 1)
                entry.would_write_raw_seconds = round(decision.abs_raw_seconds or 0.0, 1)
                entry.rewind_seconds = round(
                    (decision.abs_raw_seconds or 0.0) - decision.abs_seconds, 1
                )
        elif decision.direction == "to_cwa":
            entry.would_write_cwa_finished = decision.cwa_finished
            entry.would_write_cwa_spine_index = decision.cwa_spine_index
            entry.would_write_cwa_item_fraction = decision.cwa_item_fraction
            if decision.cwa_percentage is not None:
                entry.would_write_cwa_percentage = round(decision.cwa_percentage, 2)

        plan = (plan_abs_write(decision, book_id, audiobook.duration)
                or plan_cwa_write(decision, book_id, pair.calibre.path, entry.cwa_format))
        if plan is not None:
            gate(plan, decision, policy)
            entry.planned_write = f"{plan.target}: {plan.description}"
            entry.write_payload = plan.payload
            entry.write_blocked_by = plan.blocked_by
            if plan.allowed:
                if plan.target == "abs":
                    entry.write_status = abs_client.patch_progress(
                        pair.audiobook.library_item_id, plan.payload)
                else:
                    entry.write_status = cwa.put_progress(plan.payload)

        report.pairs.append(entry)

    return report


def save_state(report: Report, state_dir: Path) -> None:
    """Leave a credential-free record of this run for the health check to read."""
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "last-sync.json.tmp"
    tmp.write_text(report.to_json())
    tmp.replace(state_dir / "last-sync.json")
    with (state_dir / "writes.jsonl").open("a") as fh:
        for p in report.pairs:
            if p.write_status is None:
                continue
            fh.write(json.dumps({"at": report.generated_at, "calibre_book_id": p.calibre_book_id,
                                 "title": p.title, "direction": p.direction, "tier": p.tier,
                                 "write": p.planned_write, "http": p.write_status}) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="concordance",
        description="Compare reading positions between CWA/Kobo ebooks and Audiobookshelf "
                    "audiobooks, and report (or with --apply, write) what should sync.",
    )
    parser.add_argument("--test-cases", action="store_true",
                        help="restrict to the books in CONCORDANCE_TEST_CASES")
    parser.add_argument("--book-id", type=int, action="append", default=[],
                        help="restrict to specific Calibre book ids (repeatable)")
    parser.add_argument("--progress-only", action="store_true",
                        help="only books with reading progress on either side")
    parser.add_argument("--no-artifacts", action="store_true",
                        help="print the table but write no JSON/markdown")
    parser.add_argument("--log-dir", default=None,
                        help="where JSON/markdown reports go (default: <state dir>/reports)")
    parser.add_argument("--apply", action="store_true",
                        help="apply writes for books in the write allowlist "
                             "(default: dry run, nothing is written)")
    parser.add_argument("--state-dir", default=None,
                        help="also save last-sync.json and append applied writes to "
                             "writes.jsonl here, for monitoring")
    parser.add_argument("--format", choices=("table", "json"), default="table",
                        help="json prints the full report to stdout (implies --no-artifacts)")
    args = parser.parse_args(argv)

    only: set[int] | None = set(args.book_id) if args.book_id else None
    if args.test_cases:
        cases = test_case_ids()
        if not cases:
            print("error: --test-cases needs CONCORDANCE_TEST_CASES (comma-separated Calibre ids)",
                  file=sys.stderr)
            return 2
        only = cases | (only or set())

    try:
        cfg = Config.from_env()
        report = build_report(cfg, only, args.progress_only, WritePolicy.from_env(args.apply))
    except (ConfigError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.state_dir:
        save_state(report, Path(args.state_dir))

    if args.format == "json":
        print(report.to_json())
        return 0

    print(render_table(report))
    needs = [p for p in report.pairs if p.needs_alignment]
    print(f"\n{len(report.pairs)} pairs, {len(needs)} flagged for alignment, "
          f"{len(report.unmatched_audiobooks)} audiobooks unmatched")
    planned = [p for p in report.pairs if p.planned_write]
    applied = [p for p in planned if p.write_status is not None]
    mode = "APPLY" if args.apply else "dry run"
    print(f"rewind bias on CWA -> ABS: {cfg.rewind_seconds}s | {mode}: "
          f"{len(planned)} writes planned, {len(applied)} applied")
    for p in applied:
        print(f"  wrote {p.planned_write} for {p.title} -> HTTP {p.write_status}")

    if not args.no_artifacts:
        state = os.environ.get("CONCORDANCE_STATE_DIR") or Path.home() / ".cache" / "concordance"
        json_path, md_path = write_artifacts(report, Path(args.log_dir or Path(state) / "reports"))
        print(f"wrote {json_path}\n      {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
