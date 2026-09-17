"""Nightly alignment orchestrator: decide which chapter groups to align, then run them.

For every in-progress pair it finds the chapter group the reader is in (the
further of the ebook and audiobook positions) and queues that group plus the
next `--lookahead` groups, skipping any with a fresh cache entry. Jobs run one
at a time in the aligner container, and each is guarded:

* **memory:** a job starts only if MemAvailable >= `--min-free-mb` (default
  5,000). Otherwise it rechecks every `--recheck-minutes` for up to
  `CONCORDANCE_ALIGN_MEMORY_WAIT` minutes (default 60, and never past the
  deadline), then stops the run. Books without a cache entry still sync by
  estimation.
* **deadline (optional):** with `CONCORDANCE_ALIGN_DEADLINE=HH:MM` (or
  `--deadline`), no new job starts after that time, and no job starts that the
  measured pace says would still be running at it. A job already under way
  finishes. Unset, the run continues until the queue is empty.
* **CPU:** the container runs at `CONCORDANCE_ALIGNER_CPU_SHARES` (Docker's
  default 1024). Raising it lets alignment outrank other containers competing for
  the CPU overnight; on cgroup v2 the shares map non-linearly to `cpu.weight`
  (1024 -> 100, 26192 -> 1393).

Every outcome is appended to `<state dir>/runs/align-YYYYMMDD.jsonl`
(`CONCORDANCE_STATE_DIR`, default `~/.cache/concordance`) for monitoring.
Dry run by default: `--run` actually starts containers.

Runs on the host (no torch). The worker code is mounted read-only from this
checkout, so the image doesn't need rebuilding after code changes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .absclient import AbsClient, AbsProgress
from .anchor import Alignment, build_alignment
from .cache import AlignmentCache, GroupKey, manifest_fingerprint
from .calibre import load_books, read_spine, spine_file
from .config import Config, ConfigError, env_number
from .cwa import CwaClient, CwaProgress
from .matching import load_calibre_isbns, match_pairs
from .xpointer import XPointerError, resolve_any

ALIGNER_COMMIT = "64293cc6d711e57666c4a8b098e9fd93b381fd88"
ESTIMATED_PACE = 0.5         # wall-clock seconds per second of audio, measured on 8 CPU threads
LIBRARY_CONTAINER = "/library"
# Worker tuning read by concordance/aligner.py inside the container; forwarded when set.
WORKER_SETTINGS = ("CONCORDANCE_ALIGN_BUDGET_MB", "CONCORDANCE_ALIGN_CHUNK_SECONDS",
                   "CONCORDANCE_ALIGN_END_MARGIN", "CONCORDANCE_ALIGN_BATCH_SIZE",
                   "CONCORDANCE_ALIGNER_THREADS")
REPO = Path(__file__).resolve().parent.parent


def state_dir() -> Path:
    """Run logs, manifests and (by default) the alignment cache live under here."""
    return Path(os.environ.get("CONCORDANCE_STATE_DIR", "") or Path.home() / ".cache" / "concordance")


@dataclass
class Job:
    title: str
    key: GroupKey
    book_file: str          # host path
    start: float
    end: float
    manifest: list[tuple[str, float]]   # container-side paths, as ABS reports them
    reason: str


def group_span(alignment: Alignment, index: int) -> tuple[float, float]:
    group = alignment.groups[index]
    points = [p for p in alignment.points if group.first_spine <= p.spine_index <= group.last_spine]
    return min(p.audio_start for p in points), max(p.audio_end for p in points)


def current_group_index(alignment: Alignment, ebook_spine: int | None,
                        audio_time: float | None) -> int | None:
    """Index of the group holding the further-along of the two positions."""
    candidates = []
    if ebook_spine is not None:
        for i, g in enumerate(alignment.groups):
            if g.first_spine <= ebook_spine <= g.last_spine:
                candidates.append(i)
    if audio_time is not None:
        for i in range(len(alignment.groups)):
            start, end = group_span(alignment, i)
            if start <= audio_time < end:
                candidates.append(i)
    return max(candidates) if candidates else None


def select_groups(alignment: Alignment, ebook_spine: int | None, audio_time: float | None,
                  lookahead: int) -> list[int]:
    current = current_group_index(alignment, ebook_spine, audio_time)
    if current is None:
        return []
    return list(range(current, min(current + 1 + lookahead, len(alignment.groups))))


def mem_available_mb() -> int:
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def in_progress(cwa: CwaProgress | None, listening: AbsProgress | None) -> bool:
    ebook = cwa is not None and 0 < cwa.percentage < 99.0
    audio = listening is not None and listening.current_time > 0 and not listening.is_finished
    return ebook or audio


def plan_jobs(cfg: Config, lookahead: int, only: set[int] | None, log) -> list[Job]:
    cwa = CwaClient(cfg.cwa_url, cfg.cwa_user, cfg.cwa_password)
    abs_client = AbsClient(cfg.abs_url, cfg.abs_api_key)
    books = load_books(cfg.calibre_db, cfg.calibre_root)
    pairs, _ = match_pairs(books, abs_client.books(), load_calibre_isbns(cfg.calibre_db))
    listening = abs_client.progress()
    ebook = {p.calibre_book_id: p for p in cwa.export()}
    cache = AlignmentCache()
    jobs: list[Job] = []

    for pair in pairs:
        book_id = pair.calibre.book_id
        if only and book_id not in only:
            continue
        progress, audio = ebook.get(book_id), listening.get(pair.audiobook.library_item_id)
        if not in_progress(progress, audio):
            continue
        # The Kobo's format and spine come from its XPointer. Without a position, assume
        # KEPUB: KOReader's OPDS download from CWA delivers the KEPUB when one exists.
        # The chapter map must use that same file's spine: kepubify's title-page
        # dummy shifts KEPUB indices one past the EPUB's in some books.
        candidates = {ext: f[0] for ext in ("kepub", "epub")
                      if (f := sorted(pair.calibre.path.glob(f"*.{ext}")))}
        fmt, spine = None, None
        if progress is not None:
            locator = cwa.locator(book_id)
            if locator:
                try:
                    located = resolve_any(locator, candidates)
                    fmt, spine = located.fmt, located.fragment
                except XPointerError:
                    pass
        chosen = spine_file(pair.calibre, fmt)
        if chosen is None:
            log({"book": pair.calibre.title, "status": "skipped", "reason": "no EPUB/KEPUB"})
            continue
        fmt, book_file = chosen
        chapters = abs_client.chapters(pair.audiobook.library_item_id)
        audiobook = pair.audiobook.__class__(**{**pair.audiobook.__dict__, "chapters": chapters})
        alignment = build_alignment(read_spine(book_file), audiobook.substantive_chapters())
        if not alignment.groups:
            log({"book": pair.calibre.title, "status": "skipped", "reason": "no boundary alignment"})
            continue

        indices = select_groups(alignment, spine, audio.current_time if audio else None, lookahead)
        if not indices:
            log({"book": pair.calibre.title, "status": "skipped", "reason": "position not in any group"})
            continue
        manifest = abs_client.audio_manifest(pair.audiobook.library_item_id)
        abs_root, host_root = cfg.abs_audio_root or ("", "")
        try:
            audio_print = manifest_fingerprint([Path(p.replace(abs_root, host_root, 1))
                                                for p, _ in manifest])
        except OSError:
            audio_print = None
        for i in indices:
            g = alignment.groups[i]
            key = GroupKey(book_id, pair.audiobook.library_item_id, fmt,
                           g.first_spine, g.last_spine, g.first_chapter, g.last_chapter)
            existing = cache.load(key)
            if existing and audio_print and existing.is_fresh(book_file, audio_print, existing.aligner):
                log({"book": pair.calibre.title, "group": key.filename(), "status": "fresh"})
                continue
            start, end = group_span(alignment, i)
            jobs.append(Job(pair.calibre.title, key, str(book_file), start, end, manifest,
                            "current" if i == indices[0] else "lookahead"))
    return jobs


def aligner_stats(stdout: str) -> dict:
    """The worker's final JSON stats line (words, mean_score, chunks...), or {}."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "mean_score" in data:
            return data
    return {}


def alignment_status(code: int, stats: dict, min_score: float) -> str:
    """Outcome label. A clean exit with a poor mean score usually means the text
    was matched to the wrong audio (a spine off-by-one scored -5.9, against -0.07
    for a correct chapter), so it is flagged rather than trusted."""
    if code == 137:
        return "killed-oom"
    if code != 0:
        return "failed"
    score = stats.get("mean_score")
    if score is not None and score < min_score:
        return "aligned-low-score"
    return "aligned"


def docker_options(cfg: Config, cache_root: Path) -> list[str]:
    """`docker run` options shared by every job in the aligner image: user, limits, mounts.

    Audio is mounted at the path ABS reports, so manifest paths work unchanged inside.
    """
    if cfg.abs_audio_root is None:
        raise ConfigError("ABS_AUDIO_ROOT_MAP is required for alignment (e.g. /audiobooks=/srv/audiobooks)")
    abs_root, host_root = cfg.abs_audio_root
    passthrough = [arg for name in WORKER_SETTINGS if os.environ.get(name, "").strip()
                   for arg in ("-e", f"{name}={os.environ[name].strip()}")]
    return ["run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/cache",
            *passthrough,
            "--memory", cfg.aligner_memory, "--memory-swap", cfg.aligner_memory,
            "--cpu-shares", str(cfg.aligner_cpu_shares),
            "-v", f"{REPO / 'concordance'}:/app/concordance:ro",
            "-v", f"{cache_root}:/cache",
            "-v", f"{host_root}:{abs_root}:ro",
            "-v", f"{cfg.calibre_root}:{LIBRARY_CONTAINER}:ro"]


def container_path(cfg: Config, host_path: str) -> str:
    """A path under the Calibre library as the aligner container sees it."""
    return LIBRARY_CONTAINER + host_path[len(cfg.calibre_root):]


def docker_command(job: Job, cfg: Config, cache_root: Path, manifest_name: str) -> list[str]:
    """The `docker run` for one alignment job."""
    return ["docker", *docker_options(cfg, cache_root), cfg.aligner_image,
            "--book-file", container_path(cfg, job.book_file), "--fmt", job.key.fmt,
            "--spines", f"{job.key.first_spine}-{job.key.last_spine}",
            "--audio-manifest", f"/cache/work/manifests/{manifest_name}",
            "--start", f"{job.start:.3f}", "--end", f"{job.end:.3f}",
            "--calibre-id", str(job.key.calibre_book_id), "--library-item-id", job.key.library_item_id,
            "--chapters", f"{job.key.first_chapter}-{job.key.last_chapter}",
            "--aligner-commit", ALIGNER_COMMIT]


def run_job(job: Job, cfg: Config, cache_root: Path) -> tuple[int, dict]:
    work = cache_root / "work" / "manifests"
    work.mkdir(parents=True, exist_ok=True)
    manifest_file = work / f"{job.key.library_item_id}.json"
    manifest_file.write_text(json.dumps([{"path": p, "duration": d} for p, d in job.manifest]))
    cmd = docker_command(job, cfg, cache_root, manifest_file.name)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, aligner_stats(result.stdout)


def fits_before_deadline(audio_seconds: float, now: datetime, deadline: datetime | None,
                        pace: float = ESTIMATED_PACE) -> bool:
    """Whether a group this long can finish before the deadline, at the measured pace.

    The deadline only stops new jobs, so without this a 3-hour group starting five
    minutes before it would run deep into the morning.
    """
    if deadline is None:
        return True
    return now + timedelta(seconds=audio_seconds * pace) <= deadline


def parse_deadline(value: str | None, now: datetime) -> datetime | None:
    """The next occurrence of HH:MM after `now`, or None when no deadline is set."""
    if not value:
        return None
    try:
        hh, mm = (int(x) for x in value.strip().split(":"))
        deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError as exc:
        raise ConfigError(f"deadline must be HH:MM, got {value!r}") from exc
    return deadline if deadline > now else deadline + timedelta(days=1)


def memory_wait_until(now: datetime, wait_minutes: float, deadline: datetime | None) -> datetime:
    """How long a job may wait for memory: the wait limit, capped by the deadline."""
    limit = now + timedelta(minutes=wait_minutes)
    return min(limit, deadline) if deadline else limit


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="concordance.orchestrate",
                                 description="Plan (and with --run, execute) nightly alignment jobs.")
    ap.add_argument("--run", action="store_true", help="actually start aligner containers")
    ap.add_argument("--lookahead", type=int, default=int(env_number("CONCORDANCE_ALIGN_LOOKAHEAD", 2, int)),
                    help="chapter groups past the current one to align")
    ap.add_argument("--book-id", type=int, action="append", default=[])
    ap.add_argument("--planned-only", action="store_true",
                    help="print only the groups that will run, not skipped or already-fresh ones "
                         "(the run log keeps everything)")
    ap.add_argument("--min-free-mb", type=int, default=int(env_number("CONCORDANCE_ALIGN_MIN_FREE_MB", 5000, int)),
                    help="start a job only while this much memory is available")
    ap.add_argument("--recheck-minutes", type=float, default=5.0,
                    help="how often to recheck memory while waiting")
    ap.add_argument("--deadline", default=os.environ.get("CONCORDANCE_ALIGN_DEADLINE", ""),
                    help="HH:MM; no new job starts after this (default: CONCORDANCE_ALIGN_DEADLINE, "
                         "or no deadline)")
    ap.add_argument("--memory-wait-minutes", type=float,
                    default=env_number("CONCORDANCE_ALIGN_MEMORY_WAIT", 60),
                    help="how long to wait for free memory before stopping the run")
    args = ap.parse_args(argv)
    if args.min_free_mb < 0 or args.lookahead < 0:
        ap.error("--min-free-mb and --lookahead must not be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    cache_root = state_dir()
    runs = cache_root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    log_file = runs / f"align-{datetime.now():%Y%m%d}.jsonl"

    quiet = {"skipped", "fresh"}       # expected outcomes, hidden by --planned-only

    def log(record: dict) -> None:
        record = {"at": datetime.now().isoformat(timespec="seconds"), **record}
        with log_file.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        if not (args.planned_only and record.get("status") in quiet):
            print(json.dumps(record))

    try:
        deadline = parse_deadline(args.deadline, datetime.now())
        cfg = Config.from_env()
        jobs = plan_jobs(cfg, args.lookahead, set(args.book_id) or None, log)
    except (ConfigError, RuntimeError) as exc:
        log({"status": "error", "reason": str(exc)})
        return 1

    if args.planned_only or not args.run:
        minutes = sum(job.end - job.start for job in jobs) / 60
        finish = datetime.now() + timedelta(seconds=minutes * 60 * ESTIMATED_PACE)
        limit = f"deadline {deadline:%H:%M}" if deadline else "no deadline"
        print(f"{len(jobs)} group(s) to align, {minutes:.0f} audio minutes "
              f"(~{minutes * ESTIMATED_PACE / 60:.1f} h at the measured pace, done by about "
              f"{finish:%H:%M} if started now); {limit}")

    for job in jobs:
        base = {"book": job.title, "group": job.key.filename(), "reason": job.reason,
                "audio_minutes": round((job.end - job.start) / 60, 1)}
        if not args.run:
            log({**base, "status": "planned"})
            continue
        wait_until = memory_wait_until(datetime.now(), args.memory_wait_minutes, deadline)
        while mem_available_mb() < args.min_free_mb:
            if datetime.now() + timedelta(minutes=args.recheck_minutes) >= wait_until:
                log({**base, "status": "skipped-memory", "mem_available_mb": mem_available_mb()})
                break
            time.sleep(args.recheck_minutes * 60)
        else:
            if deadline and datetime.now() >= deadline:
                log({**base, "status": "skipped-deadline"})
                continue
            if not fits_before_deadline(job.end - job.start, datetime.now(), deadline):
                log({**base, "status": "skipped-deadline", "reason": "would not finish before the deadline",
                     "estimated_minutes": round((job.end - job.start) * ESTIMATED_PACE / 60)})
                continue
            started = time.time()
            code, stats = run_job(job, cfg, cache_root)
            log({**base, "status": alignment_status(code, stats, cfg.min_align_score), "exit": code,
                 "seconds": round(time.time() - started), "words": stats.get("words"),
                 "mean_score": stats.get("mean_score"), "chunks": len(stats.get("chunks") or []) or None,
                 "peak_rss_mb": stats.get("peak_rss_mb")})
            continue
        # memory didn't free up in time: stop this run
        break
    return 0


if __name__ == "__main__":
    sys.exit(main())
