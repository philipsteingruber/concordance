"""Render the sync report: CLI table, JSON artifact, and markdown."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class PairReport:
    calibre_book_id: int
    library_item_id: str
    title: str
    match_method: str
    match_score: float
    author_agrees: bool
    spine_items: int
    chapters: int
    median_chapter_minutes: float
    max_chapter_minutes: float
    confidence: str
    needs_alignment: bool
    worst_discontinuity_minutes: float
    discontinuity_at: int | None
    # Populated only when the book has reading progress on either side.
    cwa_percentage: float | None = None
    cwa_spine_index: int | None = None
    cwa_format: str | None = None           # "kepub" | "epub": the file the Kobo has open
    cwa_item_fraction: float | None = None  # how far through the spine item (resolved XPointer)
    abs_current_time: float | None = None
    abs_is_finished: bool = False
    direction: str | None = None            # "to_abs" | "to_cwa" | "in_sync" | "none"
    reason: str = ""
    tier: str | None = None
    # to_abs
    would_write_seconds: float | None = None
    would_write_raw_seconds: float | None = None
    rewind_seconds: float | None = None
    would_write_abs_finished: bool = False
    # to_cwa
    would_write_cwa_percentage: float | None = None
    would_write_cwa_spine_index: int | None = None
    would_write_cwa_finished: bool = False
    would_write_cwa_item_fraction: float | None = None
    note: str = ""
    planned_write: str | None = None        # human description, e.g. "abs: currentTime 3h09m12s"
    write_payload: dict | None = None
    write_blocked_by: list[str] | None = None
    write_status: int | None = None         # HTTP status when actually applied
    error: str | None = None

    @property
    def decision_label(self) -> str:
        return {"to_abs": "->abs", "to_cwa": "->cwa", "in_sync": "in sync"}.get(
            self.direction or "", "-"
        )

    @property
    def writes_label(self) -> str:
        if self.direction == "to_abs":
            return "finished" if self.would_write_abs_finished else _hms(self.would_write_seconds)
        if self.direction == "to_cwa":
            if self.would_write_cwa_finished:
                return "finished"
            frag = f" DF{self.would_write_cwa_spine_index}" if self.would_write_cwa_spine_index else ""
            return f"{self.would_write_cwa_percentage:.1f}%{frag}"
        return "-"


@dataclass
class Report:
    generated_at: str
    dry_run: bool
    rewind_seconds: int
    pairs: list[PairReport] = field(default_factory=list)
    unmatched_audiobooks: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)


def _hms(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    return f"{total // 3600}h{total % 3600 // 60:02d}m"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(report: Report, only_with_progress: bool = False) -> str:
    """Fixed-width table for eyeballing in a terminal."""
    rows = [p for p in report.pairs if not only_with_progress or p.direction not in (None, "none")]
    if not rows:
        return "No pairs to show."

    header = (
        f"{'book':<30} {'ch':>4} {'med':>6} {'conf':<8} "
        f"{'ebook':>7} {'abs now':>8} {'decision':<8} {'writes':>11} {'tier':<10} align"
    )
    lines = [header, "-" * len(header)]
    for p in rows:
        pct = f"{p.cwa_percentage:.1f}%" if p.cwa_percentage is not None else "-"
        abs_now = "done" if p.abs_is_finished else _hms(p.abs_current_time)
        lines.append(
            f"{_truncate(p.title, 29):<30} {p.chapters:>4} "
            f"{p.median_chapter_minutes:>5.1f}m {p.confidence:<8} "
            f"{pct:>7} {abs_now:>8} {p.decision_label:<8} {p.writes_label:>11} "
            f"{(p.tier or '-'):<10} {'YES' if p.needs_alignment else ''}"
        )
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    """Markdown mirror of the table, following the fuzzy-duplicates log convention."""
    out = [
        "# Concordance report",
        "",
        f"Generated {report.generated_at} — **read-only, nothing was written**.",
        "",
        f"Rewind bias for CWA → ABS writes: **{report.rewind_seconds}s**.",
        "",
        "## Pairs with reading progress",
        "",
        "| Book | Chapters | Median chapter | Confidence | Ebook | ABS now | Decision | Writes | Tier | Why |",
        "| --- | ---: | ---: | --- | ---: | ---: | --- | ---: | --- | --- |",
    ]
    active = [p for p in report.pairs if p.direction not in (None, "none")]
    for p in sorted(active, key=lambda r: -r.median_chapter_minutes):
        pct = f"{p.cwa_percentage:.1f}%" if p.cwa_percentage is not None else "-"
        abs_now = "finished" if p.abs_is_finished else _hms(p.abs_current_time)
        out.append(
            f"| {p.title} | {p.chapters} | {p.median_chapter_minutes:.1f} min | "
            f"{p.confidence} | {pct} | {abs_now} | {p.decision_label} | "
            f"{p.writes_label} | {p.tier or '-'} | {p.reason} |"
        )

    worklist = sorted(
        (p for p in report.pairs if p.needs_alignment),
        key=lambda r: -r.median_chapter_minutes,
    )
    out += [
        "",
        "## Alignment worklist",
        "",
        "Ranked by median chapter length, because post-anchor error scales with it.",
        "A long chapter leaves you far out even when the anchor is correct.",
        "",
        "| Book | Median chapter | Longest | Confidence | Why |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for p in worklist:
        why = "long chapters" if p.median_chapter_minutes > 20 else f"anchor {p.confidence}"
        out.append(
            f"| {p.title} | {p.median_chapter_minutes:.1f} min | "
            f"{p.max_chapter_minutes:.1f} min | {p.confidence} | {why} |"
        )

    failed = [p for p in report.pairs if p.error]
    if failed:
        out += ["", "## Errors", ""]
        out += [f"- **{p.title}** — {p.error}" for p in failed]

    if report.unmatched_audiobooks:
        out += [
            "",
            "## Audiobooks with no ebook counterpart",
            "",
            *[f"- {t}" for t in sorted(report.unmatched_audiobooks)],
        ]
    return "\n".join(out) + "\n"


def write_artifacts(report: Report, log_dir: Path) -> tuple[Path, Path]:
    """Write a timestamped JSON + markdown pair, and refresh `latest.md`."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = log_dir / f"{stamp}.json"
    md_path = log_dir / f"{stamp}.md"
    json_path.write_text(report.to_json(), encoding="utf-8")
    markdown = render_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    (log_dir / "latest.md").write_text(markdown, encoding="utf-8")
    return json_path, md_path
