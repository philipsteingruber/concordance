# Snapshot of concordance/anchor.py at commit 0c54330 (ordinal pairing), kept
# only as the baseline for evaluate.py. Regenerate with:
#   git show 0c54330:concordance/anchor.py
"""Align EPUB spine items to audiobook chapters, and score how much to trust it.

The idea in one line: chapters are landmarks both formats agree on, so instead
of scaling a percentage across the whole book, find the landmark and estimate
only the short distance from there. Error resets to zero at every boundary
rather than accumulating, so it is bounded by one chapter's length.

Quality scoring exists because ordinal alignment can silently slip. Three
signals, in decreasing order of usefulness:

1. Drift discontinuity — if item k maps to the wrong chapter, cumulative text
   position and cumulative audio position diverge abruptly at k and stay
   diverged. A smooth curve means the mapping holds; a step means it broke,
   and says where.
2. Count agreement — |spine items - chapters| near zero implies 1:1.
3. Residual magnitude — bounded, slowly varying drift is healthy; erratic
   drift means the structures genuinely disagree (omnibus, abridgement).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from concordance.absclient import Chapter
from concordance.calibre import SpineItem


@dataclass(frozen=True)
class AnchorPoint:
    """One aligned pair: a spine item and the audio chapter it corresponds to."""

    spine_index: int      # 1-based, matches DocFragment[N]
    chapter_index: int    # 1-based within substantive chapters
    text_start: int       # cumulative chars before this item
    text_chars: int
    audio_start: float    # seconds
    audio_end: float


@dataclass(frozen=True)
class Alignment:
    """A full spine<->chapter mapping plus its quality assessment."""

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

    @property
    def count_delta(self) -> int:
        return abs(self.spine_count - self.chapter_count)

    @property
    def usable(self) -> bool:
        """Whether anchoring should be trusted over a plain percentage."""
        return bool(self.points) and self.confidence != "unusable"

    @property
    def confidence(self) -> str:
        """`high` | `medium` | `low` | `unusable`.

        Thresholds are deliberately conservative: a wrong anchor is worse than
        no anchor, because it lands you confidently in the wrong chapter.
        """
        if not self.points:
            return "unusable"
        # A large count mismatch means we cannot say which chapter is which.
        ratio = self.count_delta / max(self.spine_count, self.chapter_count)
        if ratio > 0.25:
            return "unusable"
        # A discontinuity bigger than a typical chapter means the map slipped.
        if self.worst_discontinuity_minutes > self.median_chapter_minutes:
            return "low"
        if ratio > 0.1 or self.worst_discontinuity_minutes > self.median_chapter_minutes / 2:
            return "medium"
        return "high"

    @property
    def needs_alignment(self) -> bool:
        """Whether this pair should be prioritised for aeneas refinement.

        Post-anchor error scales with chapter duration, so long chapters are
        where anchoring alone is weakest — regardless of how confident the
        mapping is. A 74-minute chapter can leave you half an hour out even
        when the anchor is perfectly correct.
        """
        return self.median_chapter_minutes > 20.0 or self.confidence in {"low", "unusable"}


def build_alignment(spine: list[SpineItem], chapters: list[Chapter]) -> Alignment:
    """Align content spine items to substantive chapters by ordinal position."""
    content = [item for item in spine if item.is_content]
    n = min(len(content), len(chapters))

    chapter_mins = [c.duration / 60.0 for c in chapters] or [0.0]
    median_ch = statistics.median(chapter_mins)
    max_ch = max(chapter_mins)

    if n == 0:
        return Alignment(
            points=[], total_chars=0, total_seconds=0.0,
            spine_count=len(content), chapter_count=len(chapters),
            max_drift_minutes=0.0, mean_drift_minutes=0.0,
            worst_discontinuity_minutes=0.0, discontinuity_at=None,
            median_chapter_minutes=median_ch, max_chapter_minutes=max_ch,
        )

    total_chars = sum(item.chars for item in content[:n])
    origin = chapters[0].start
    total_seconds = chapters[n - 1].end - origin

    points: list[AnchorPoint] = []
    drifts: list[float] = []
    cumulative = 0
    for k in range(n):
        item, chapter = content[k], chapters[k]
        points.append(
            AnchorPoint(
                spine_index=item.index,
                chapter_index=chapter.index,
                text_start=cumulative,
                text_chars=item.chars,
                audio_start=chapter.start,
                audio_end=chapter.end,
            )
        )
        cumulative += item.chars
        if total_chars > 0 and total_seconds > 0:
            text_fraction = cumulative / total_chars
            audio_fraction = (chapter.end - origin) / total_seconds
            drifts.append((text_fraction - audio_fraction) * total_seconds / 60.0)

    # A slipped mapping shows up as a sudden jump between consecutive drifts,
    # not as a large drift on its own (steady drift is just pace mismatch).
    worst_step, worst_at = 0.0, None
    for i in range(1, len(drifts)):
        step = abs(drifts[i] - drifts[i - 1])
        if step > worst_step:
            worst_step, worst_at = step, i + 1

    abs_drifts = [abs(d) for d in drifts] or [0.0]
    return Alignment(
        points=points,
        total_chars=total_chars,
        total_seconds=total_seconds,
        spine_count=len(content),
        chapter_count=len(chapters),
        max_drift_minutes=max(abs_drifts),
        mean_drift_minutes=statistics.mean(abs_drifts),
        worst_discontinuity_minutes=worst_step,
        discontinuity_at=worst_at,
        median_chapter_minutes=median_ch,
        max_chapter_minutes=max_ch,
    )
