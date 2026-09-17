"""Align EPUB spine items to audiobook chapters, and score how much to trust it.

Chapters are landmarks both formats agree on, so a reading position can be
placed relative to the nearest boundary instead of by scaling a percentage
across the whole book. Error then resets at every matched boundary rather than
accumulating.

Matching lines up **boundaries**, not item counts. Publishers routinely pack
several chapters into one EPUB file (Don't Call Me Hero: 9 files, 23 audio
chapters) or split one chapter across several audio tracks (Hitchhiker's Guide:
35 files, 66 tracks). Pairing item N with chapter N breaks on both. Instead we
walk the two lists together and match a text boundary to an audio boundary when
the stretches between consecutive matches have consistent proportions. One
text item may therefore cover several chapters, and vice versa.

Concretely this is a dynamic program over candidate boundary pairs (i, j): the
cumulative text fraction after item i is close to the cumulative audio fraction
after chapter j. A path from (0, 0) to (n, m) scores each step by how well the
text and audio segment lengths agree, so the best path prefers many boundaries
with consistent local pace. Global pace drift doesn't break it because the
agreement is judged per segment, not against the book's overall percentage.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .absclient import Chapter
from .calibre import SpineItem

# A boundary pair is a candidate only if the (scale-corrected) cumulative
# positions are this close. Per-segment agreement does the real discrimination;
# this only bounds the search. Measured: 0.04 missed correct pairs, 0.10 and 0.20
# gave identical results across the library.
CANDIDATE_TOLERANCE = 0.10
# Items either side may leave unmatched at the start or end of the book, and
# the score cost per skipped item. A well-matched boundary gains about 1.0, so
# skipping is chosen only when absorbing the material would distort a segment.
# Needed because ebooks routinely end with material the audiobook lacks: Legends
# & Lattes carries ~17% of its text after the epilogue (a bonus story, an
# interview, a sequel preview).
MAX_EDGE_SKIP = 10
EDGE_SKIP_PENALTY = 0.5
# Largest number of items one group may span on either side.
MAX_GROUP_ITEMS = 30
# A segment pair whose lengths differ by this ratio contributes nothing; worse
# ratios are penalised. 1.5 = one side is 50% longer than the other.
SEGMENT_RATIO_LIMIT = math.log(1.5)
# Smoothing so tiny segments don't produce wild ratios.
SEGMENT_EPS = 0.002
# Segments shorter than this share of the book are ignored for confidence.
CONFIDENCE_MIN_SEGMENT = 0.005


@dataclass(frozen=True)
class AnchorPoint:
    """One spine item and the audio span it corresponds to."""

    spine_index: int      # 1-based, matches DocFragment[N]
    chapter_index: int    # ABS chapter containing the item's audio start
    text_start: int       # cumulative chars before this item
    text_chars: int
    audio_start: float    # seconds
    audio_end: float


@dataclass(frozen=True)
class Group:
    """A run of consecutive spine items matched to a run of consecutive chapters."""

    first_spine: int
    last_spine: int
    first_chapter: int
    last_chapter: int
    text_fraction: float     # share of the book's text
    audio_fraction: float    # share of the book's audio
    audio_seconds: float

    @property
    def ratio(self) -> float:
        a, t = max(self.audio_fraction, 1e-9), max(self.text_fraction, 1e-9)
        return max(a / t, t / a)


@dataclass(frozen=True)
class Alignment:
    """A spine<->chapter mapping plus its quality assessment."""

    points: list[AnchorPoint]
    total_chars: int
    total_seconds: float
    spine_count: int
    chapter_count: int
    max_drift_minutes: float
    mean_drift_minutes: float
    worst_discontinuity_minutes: float
    discontinuity_at: int | None
    median_chapter_minutes: float
    max_chapter_minutes: float
    groups: list[Group] = field(default_factory=list)

    @property
    def count_delta(self) -> int:
        return abs(self.spine_count - self.chapter_count)

    @property
    def worst_group_ratio(self) -> float:
        ratios = [
            g.ratio for g in self.groups
            if g.text_fraction >= CONFIDENCE_MIN_SEGMENT and g.audio_fraction >= CONFIDENCE_MIN_SEGMENT
        ]
        return max(ratios, default=float("inf") if not self.groups else 1.0)

    @property
    def coverage(self) -> float:
        """Matched boundaries as a share of the shorter list. 1.0 = every boundary found."""
        shorter = min(self.spine_count, self.chapter_count)
        return len(self.groups) / shorter if shorter else 0.0

    @property
    def max_group_minutes(self) -> float:
        return max((g.audio_seconds / 60.0 for g in self.groups), default=0.0)

    @property
    def usable(self) -> bool:
        """Whether anchoring should be trusted over a plain percentage."""
        return bool(self.points) and self.confidence != "unusable"

    @property
    def confidence(self) -> str:
        """`high` | `medium` | `low` | `unusable`.

        Judged on whether matched segments have consistent proportions, and on
        how many boundaries were matched. Group *size* is deliberately not part
        of this: a correct 3-hour group is still correct, and the search-window
        cap is what decides whether it is too coarse to use.
        """
        if not self.points or not self.groups:
            return "unusable"
        ratio, coverage = self.worst_group_ratio, self.coverage
        if ratio <= 1.25 and coverage >= 0.6:
            return "high"
        if ratio <= 1.5 and coverage >= 0.4:
            return "medium"
        if ratio <= 2.0:
            return "low"
        return "unusable"

    @property
    def needs_alignment(self) -> bool:
        """Kept for the report: long chapters or a weak anchor mean anchoring alone is far off."""
        return self.median_chapter_minutes > 20.0 or self.confidence in {"low", "unusable"}


def _cumulative(values: list[float]) -> list[float]:
    total = sum(values) or 1.0
    out, running = [0.0], 0.0
    for v in values:
        running += v
        out.append(running / total)
    return out


def _match_boundaries(
    text: list[float], audio: list[float], log_scale: float = 0.0
) -> tuple[list[tuple[int, int]], float] | None:
    """Best monotone path of matched boundaries, or None.

    `log_scale` corrects for a constant text/audio proportion offset: when one
    side carries extra material, every cumulative fraction on that side is
    compressed by the same factor. The path may start after skipping up to
    MAX_EDGE_SKIP leading items on either side and end before the same number of
    trailing items, each skip costing EDGE_SKIP_PENALTY.
    """
    n, m = len(text) - 1, len(audio) - 1
    scale = math.exp(log_scale)

    score: dict[tuple[int, int], float] = {}
    back: dict[tuple[int, int], tuple[int, int] | None] = {}
    for k in range(0, min(MAX_EDGE_SKIP, n) + 1):
        score[(k, 0)] = -EDGE_SKIP_PENALTY * k
        back[(k, 0)] = None
    for k in range(1, min(MAX_EDGE_SKIP, m) + 1):
        score[(0, k)] = -EDGE_SKIP_PENALTY * k
        back[(0, k)] = None

    starts_by_i: dict[int, list[int]] = {}
    for (i, j) in score:
        starts_by_i.setdefault(i, []).append(j)

    def shifted_audio(j: int, origin_j: int) -> float:
        return audio[j]

    reachable_by_i: dict[int, list[int]] = {i: list(js) for i, js in starts_by_i.items()}
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if abs(text[i] - scale * audio[j]) > CANDIDATE_TOLERANCE and not (
                n - i <= MAX_EDGE_SKIP and m - j <= MAX_EDGE_SKIP
            ):
                continue
            best, best_prev = -math.inf, None
            for pi in range(max(0, i - MAX_GROUP_ITEMS), i):
                for pj in reachable_by_i.get(pi, []):
                    if pj >= j or j - pj > MAX_GROUP_ITEMS:
                        continue
                    t_seg = text[i] - text[pi]
                    a_seg = scale * (audio[j] - audio[pj])
                    misfit = abs(math.log((t_seg + SEGMENT_EPS) / (a_seg + SEGMENT_EPS)))
                    total = score[(pi, pj)] + 1.0 - misfit / SEGMENT_RATIO_LIMIT
                    if total > best:
                        best, best_prev = total, (pi, pj)
            if best_prev is not None:
                if (i, j) in score and score[(i, j)] >= best:
                    continue
                score[(i, j)] = best
                back[(i, j)] = best_prev
                reachable_by_i.setdefault(i, []).append(j)

    ends = [
        (score[(i, j)] - EDGE_SKIP_PENALTY * ((n - i) + (m - j)), (i, j))
        for (i, j) in score
        if back.get((i, j)) is not None and n - i <= MAX_EDGE_SKIP and m - j <= MAX_EDGE_SKIP
    ]
    if not ends:
        return None
    best_total, node = max(ends)
    path = [node]
    while back[node] is not None:
        node = back[node]
        path.append(node)
    return list(reversed(path)), best_total


def _estimate_log_scale(text: list[float], audio: list[float], path: list[tuple[int, int]]) -> float:
    """Median log(text share / audio share) over the matched groups."""
    ratios = []
    for (pi, pj), (i, j) in zip(path, path[1:]):
        t_seg, a_seg = text[i] - text[pi], audio[j] - audio[pj]
        if t_seg >= CONFIDENCE_MIN_SEGMENT and a_seg >= CONFIDENCE_MIN_SEGMENT:
            ratios.append(math.log(t_seg / a_seg))
    return statistics.median(ratios) if ratios else 0.0


def build_alignment(spine: list[SpineItem], chapters: list[Chapter]) -> Alignment:
    """Align content spine items to substantive chapters by matching boundaries."""
    content = [item for item in spine if item.is_content]
    chapter_mins = [c.duration / 60.0 for c in chapters] or [0.0]
    median_ch = statistics.median(chapter_mins)
    max_ch = max(chapter_mins)

    def empty() -> Alignment:
        return Alignment(
            points=[], total_chars=0, total_seconds=0.0,
            spine_count=len(content), chapter_count=len(chapters),
            max_drift_minutes=0.0, mean_drift_minutes=0.0,
            worst_discontinuity_minutes=0.0, discontinuity_at=None,
            median_chapter_minutes=median_ch, max_chapter_minutes=max_ch,
        )

    if not content or not chapters:
        return empty()

    text_cum = _cumulative([float(item.chars) for item in content])
    audio_cum = _cumulative([c.duration for c in chapters])
    # Two passes: match with no scale correction, estimate the text/audio scale
    # from what matched, then re-match with it. Extra material on one side
    # compresses every cumulative fraction on that side by the same factor.
    first = _match_boundaries(text_cum, audio_cum)
    if first is None:
        return empty()
    log_scale = _estimate_log_scale(text_cum, audio_cum, first[0])
    second = _match_boundaries(text_cum, audio_cum, log_scale) if abs(log_scale) > 0.01 else None
    path = (second or first)[0]

    total_chars = sum(item.chars for item in content)
    total_seconds = chapters[-1].end - chapters[0].start
    char_offsets = [0]
    for item in content:
        char_offsets.append(char_offsets[-1] + item.chars)

    points: list[AnchorPoint] = []
    groups: list[Group] = []
    misfits: list[tuple[float, int]] = []
    for (pi, pj), (i, j) in zip(path, path[1:]):
        items = content[pi:i]
        chs = chapters[pj:j]
        span_start, span_end = chs[0].start, chs[-1].end
        group_chars = sum(item.chars for item in items) or 1
        groups.append(Group(
            first_spine=items[0].index, last_spine=items[-1].index,
            first_chapter=chs[0].index, last_chapter=chs[-1].index,
            text_fraction=text_cum[i] - text_cum[pi],
            audio_fraction=audio_cum[j] - audio_cum[pj],
            audio_seconds=span_end - span_start,
        ))
        misfits.append((
            abs((text_cum[i] - text_cum[pi]) - (audio_cum[j] - audio_cum[pj])) * total_seconds / 60.0,
            items[0].index,
        ))
        # Within a group, place items proportionally to their text length.
        running = 0
        for k, item in enumerate(items):
            start = span_start + (running / group_chars) * (span_end - span_start)
            running += item.chars
            end = span_start + (running / group_chars) * (span_end - span_start)
            chapter = next((c for c in chs if c.start <= start < c.end), chs[0])
            points.append(AnchorPoint(
                spine_index=item.index, chapter_index=chapter.index,
                text_start=char_offsets[pi + k], text_chars=item.chars,
                audio_start=start, audio_end=end,
            ))

    drifts = [
        abs((p.text_start + p.text_chars) / total_chars - (p.audio_end - chapters[0].start) / total_seconds)
        * total_seconds / 60.0
        for p in points
    ] or [0.0]
    worst_misfit, worst_at = max(misfits, default=(0.0, None))
    return Alignment(
        points=points, total_chars=total_chars, total_seconds=total_seconds,
        spine_count=len(content), chapter_count=len(chapters),
        max_drift_minutes=max(drifts), mean_drift_minutes=statistics.mean(drifts),
        worst_discontinuity_minutes=worst_misfit, discontinuity_at=worst_at,
        median_chapter_minutes=median_ch, max_chapter_minutes=max_ch,
        groups=groups,
    )
