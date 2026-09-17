"""Translate a reading position between ebook and audiobook coordinates.

Tiers produced here, most precise first:

1. `interpolated` — DocFragment[N] identifies the chapter, and the resolved
                    XPointer offset places the position proportionally inside it.
2. `anchor`       — the chapter is known but not the offset: its start.
3. `percentage`   — scale the whole-book percentage. Accumulates error; used only
                    when the chapter map is unusable or there is no XPointer.
4. `none`         — no position available at all.

The `aligned` tier comes from the forced-alignment cache (see cli.py), which
takes precedence over these estimates when an entry exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from .anchor import Alignment, AnchorPoint
from .cwa import CwaProgress


@dataclass(frozen=True)
class Translation:
    """A position translated into audiobook seconds."""

    seconds: float           # after rewind
    raw_seconds: float       # before rewind, for diagnostics
    tier: str                # "anchor" | "percentage" | "none"
    chapter_index: int | None
    fraction_in_chapter: float | None
    note: str = ""

    @property
    def rewind_applied(self) -> float:
        return max(0.0, self.raw_seconds - self.seconds)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ebook_to_audio(
    progress: CwaProgress,
    alignment: Alignment,
    rewind_seconds: int = 0,
    book_duration: float = 0.0,
    item_fraction: float | None = None,
) -> Translation:
    """Map a CWA/KOReader ebook position onto an audiobook timestamp.

    `rewind_seconds` biases the result backwards. Error is asymmetric here:
    landing ahead spoils plot and forces you to hunt backwards through audio,
    which is far harder than skipping forward. Landing behind costs a little
    re-listening.

    `book_duration` is the audiobook's own length, used for the percentage tier
    when anchoring produced no usable span. Without it, a book whose chapters
    could not be aligned would fall through to "no position" even though a
    perfectly serviceable percentage was available.
    """
    total = alignment.total_seconds or book_duration
    spine_index = progress.spine_index

    if alignment.usable and spine_index is not None:
        point = next((p for p in alignment.points if p.spine_index == spine_index), None)
        if point is not None:
            if item_fraction is not None:
                # The full XPointer resolved to a character offset inside the
                # spine item, so place the position proportionally within the
                # item's audio span instead of at its start.
                fraction = _clamp(item_fraction, 0.0, 1.0)
                raw = point.audio_start + fraction * (point.audio_end - point.audio_start)
                return _finish(raw, total, rewind_seconds, "interpolated",
                               point.chapter_index, fraction,
                               f"DocFragment[{spine_index}] {100 * fraction:.0f}% -> chapter {point.chapter_index}")
            # Only the spine item is known (the path didn't resolve against the
            # book's files): assume its start, a small backwards bias in the safe
            # direction.
            raw = point.audio_start
            return _finish(raw, total, rewind_seconds, "anchor",
                           point.chapter_index, 0.0,
                           f"DocFragment[{spine_index}] -> chapter {point.chapter_index}")
        note = f"DocFragment[{spine_index}] outside aligned range"
    elif spine_index is None:
        note = "no XPointer in KOSync progress"
    else:
        note = f"anchor confidence {alignment.confidence}"

    if progress.percentage > 0 and total > 0:
        raw = (progress.percentage / 100.0) * total
        return _finish(raw, total, rewind_seconds, "percentage", None, None, note)

    return Translation(0.0, 0.0, "none", None, None, note or "no position available")


def _finish(
    raw: float,
    total: float,
    rewind: int,
    tier: str,
    chapter: int | None,
    fraction: float | None,
    note: str,
) -> Translation:
    return Translation(
        seconds=_clamp(raw - rewind, 0.0, total),
        raw_seconds=_clamp(raw, 0.0, total),
        tier=tier,
        chapter_index=chapter,
        fraction_in_chapter=fraction,
        note=note,
    )


def audio_item_fraction(current_time: float, alignment: Alignment) -> tuple[int, float] | None:
    """(spine index, fraction through that spine item) for an audio timestamp, if anchored.

    This is what a CWA write needs to build a real XPointer: the spine item plus
    how far through its canonical text the position falls.
    """
    point = chapter_point_at(current_time, alignment)
    if point is None:
        return None
    span = max(1e-9, point.audio_end - point.audio_start)
    return point.spine_index, _clamp((current_time - point.audio_start) / span, 0.0, 1.0)


def audio_to_ebook(current_time: float, alignment: Alignment) -> tuple[float, int | None, str]:
    """Map an audiobook timestamp to (ebook percentage 0-100, spine index, tier).

    The spine index lets a CWA write carry `DocFragment[N]`, so KOReader lands in
    the right chapter even though its own percentage scale (screen pagination)
    differs from ours by about a point.

    No rewind is applied in this direction: scanning backwards in text is cheap,
    so the asymmetry that motivates the rewind does not exist here.
    """
    point = chapter_point_at(current_time, alignment)
    if point is not None and alignment.total_chars > 0:
        span = max(1e-9, point.audio_end - point.audio_start)
        fraction = _clamp((current_time - point.audio_start) / span, 0.0, 1.0)
        chars = point.text_start + fraction * point.text_chars
        # Time is interpolated within the chapter's span, so this is as precise as
        # the ebook->audio "interpolated" tier, and named the same so the write
        # gate treats both directions alike.
        return _clamp(100.0 * chars / alignment.total_chars, 0.0, 100.0), point.spine_index, "interpolated"
    if alignment.total_seconds > 0:
        return _clamp(100.0 * current_time / alignment.total_seconds, 0.0, 100.0), None, "percentage"
    return 0.0, None, "none"


def chapter_point_at(current_time: float, alignment: Alignment) -> AnchorPoint | None:
    """The anchor point whose audio chapter contains `current_time`, if trustworthy."""
    if not alignment.usable:
        return None
    for point in alignment.points:
        if point.audio_start <= current_time < point.audio_end:
            return point
    return None


def ebook_point(progress: CwaProgress, alignment: Alignment) -> AnchorPoint | None:
    """The anchor point for the ebook's current spine item, if trustworthy."""
    if not alignment.usable or progress.spine_index is None:
        return None
    return next((p for p in alignment.points if p.spine_index == progress.spine_index), None)
