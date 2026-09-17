"""Decide which side of a pair leads, and what would be written to the other.

The rule is "furthest wins, writes only move forward", and it is forced rather
than chosen:

* CWA's Kobo bookmark rejects any lower percentage at the SQL level, so a write
  that moves the ebook backwards would be silently dropped anyway.
* Reading trackers that import from both systems treat a moved position or
  timestamp as reading, so a backwards write risks corrupting their history.
* "Most recent wins" is unsafe with KOSync: a push is stamped with the time
  the *server* received it, so a Kobo that syncs late looks newer while
  carrying an old position.

Positions are compared chapter-first. Anchoring resolves an ebook position
only to the start of its chapter, so two positions in the same chapter cannot
honestly be ordered — that counts as in sync until forced alignment adds
within-chapter precision. Books that cannot be anchored are compared in
percentage space with a small deadband instead.

"Finished" follows each system's own definition, so Concordance never
disagrees with either: CWA's FINISHED_PERCENT_THRESHOLD (99.0) and ABS's
`isFinished` flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from .absclient import AbsProgress
from .anchor import Alignment
from .cwa import CwaProgress
from .translate import (
    audio_item_fraction,
    audio_to_ebook,
    chapter_point_at,
    ebook_point,
    ebook_to_audio,
)

CWA_FINISHED_PERCENT = 99.0     # cps/progress_syncing/protocols/kosync.py:508
PERCENT_DEADBAND = 1.0          # KOReader's % and ours disagree by about a point
# Within one chapter, an interpolated ebook position is only estimated to be
# accurate to ~0.5-2 min (proportional placement, no alignment yet). Closer
# than this counts as in sync rather than a write.
SAME_CHAPTER_DEADBAND_SECONDS = 120.0
# With a forced alignment the positions are accurate to seconds, so a much
# tighter deadband avoids writing trivially different positions back and forth.
ALIGNED_DEADBAND_SECONDS = 20.0


@dataclass(frozen=True)
class Decision:
    direction: str                          # "to_abs" | "to_cwa" | "in_sync" | "none"
    reason: str
    tier: str | None = None
    abs_seconds: float | None = None        # to_abs: position to write
    abs_raw_seconds: float | None = None    # to_abs: before rewind
    abs_finished: bool = False              # to_abs: mark finished instead
    cwa_percentage: float | None = None     # to_cwa: 0-100
    cwa_spine_index: int | None = None      # to_cwa: DocFragment[N] to write
    cwa_finished: bool = False              # to_cwa: mark finished
    cwa_item_fraction: float | None = None  # to_cwa: fraction through the spine item


def decide(
    cwa: CwaProgress | None,
    listening: AbsProgress | None,
    alignment: Alignment,
    rewind_seconds: int,
    book_duration: float,
    item_fraction: float | None = None,
    aligned_ebook_time: float | None = None,
    aligned_audio_fraction: tuple[int, float] | None = None,
) -> Decision:
    """`item_fraction` is how far through its spine item the ebook position is,
    from a resolved XPointer. Without it the ebook resolves only to chapter start.

    `aligned_ebook_time` (audio seconds for the ebook position) and
    `aligned_audio_fraction` ((spine index, fraction through it) for the audio
    position) come from a fresh forced-alignment cache entry. When present they
    replace the proportional estimates and the tier becomes `aligned`.
    """
    has_cwa = cwa is not None and cwa.percentage > 0
    has_abs = listening is not None and (listening.current_time > 0 or listening.is_finished)
    cwa_done = has_cwa and cwa.percentage >= CWA_FINISHED_PERCENT
    abs_done = has_abs and listening.is_finished

    if not has_cwa and not has_abs:
        return Decision("none", "no progress on either side")

    if cwa_done and abs_done:
        return Decision("in_sync", "finished on both sides")
    if cwa_done:
        return Decision("to_abs", "ebook finished", tier="finished", abs_finished=True)
    if abs_done:
        last = alignment.points[-1].spine_index if alignment.usable and alignment.points else None
        return Decision("to_cwa", "audiobook finished", tier="finished",
                        cwa_percentage=100.0, cwa_spine_index=last, cwa_finished=True)

    if has_cwa and not has_abs:
        return _to_abs(cwa, alignment, rewind_seconds, book_duration, "only the ebook has progress",
                       item_fraction=item_fraction, aligned_time=aligned_ebook_time)
    if has_abs and not has_cwa:
        return _to_cwa(listening, alignment, "only the audiobook has progress",
                       aligned=aligned_audio_fraction)

    # Both sides moved and both are aligned: compare exact audio times directly.
    if aligned_ebook_time is not None:
        gap = aligned_ebook_time - listening.current_time
        if abs(gap) <= ALIGNED_DEADBAND_SECONDS:
            return Decision("in_sync", f"aligned positions within {ALIGNED_DEADBAND_SECONDS:.0f}s",
                            tier="aligned")
        if gap > 0:
            return _to_abs(cwa, alignment, rewind_seconds, book_duration, "ebook is ahead (aligned)",
                           floor=listening.current_time, aligned_time=aligned_ebook_time)
        return _to_cwa(listening, alignment, "audiobook is ahead (aligned)", floor=cwa.percentage,
                       aligned=aligned_audio_fraction)

    # Both sides have moved. Chapter first, when both can be anchored.
    ebook_at = ebook_point(cwa, alignment)
    audio_at = chapter_point_at(listening.current_time, alignment)
    if ebook_at is not None and audio_at is not None:
        order = {p.spine_index: i for i, p in enumerate(alignment.points)}
        e, a = order[ebook_at.spine_index], order[audio_at.spine_index]
        if e == a:
            if item_fraction is None:
                return Decision("in_sync", "same chapter (within-chapter order needs alignment)",
                                tier="anchor")
            ebook_time = ebook_at.audio_start + item_fraction * (ebook_at.audio_end - ebook_at.audio_start)
            gap = ebook_time - listening.current_time
            if abs(gap) <= SAME_CHAPTER_DEADBAND_SECONDS:
                return Decision("in_sync", f"same chapter, within {SAME_CHAPTER_DEADBAND_SECONDS:.0f}s",
                                tier="interpolated")
            if gap > 0:
                return _to_abs(cwa, alignment, rewind_seconds, book_duration,
                               "ebook is ahead within the chapter", floor=listening.current_time,
                               item_fraction=item_fraction)
            return _to_cwa(listening, alignment, "audiobook is ahead within the chapter",
                           floor=cwa.percentage)
        if e > a:
            return _to_abs(cwa, alignment, rewind_seconds, book_duration,
                           "ebook is chapters ahead", floor=listening.current_time,
                           item_fraction=item_fraction)
        return _to_cwa(listening, alignment, "audiobook is chapters ahead",
                       floor=cwa.percentage)

    # Otherwise compare in percentage space.
    audio_pct, _, _ = audio_to_ebook(listening.current_time, alignment)
    if abs(cwa.percentage - audio_pct) <= PERCENT_DEADBAND:
        return Decision("in_sync", f"within {PERCENT_DEADBAND:g} point of each other",
                        tier="percentage")
    if cwa.percentage > audio_pct:
        return _to_abs(cwa, alignment, rewind_seconds, book_duration,
                       "ebook is ahead", floor=listening.current_time)
    return _to_cwa(listening, alignment, "audiobook is ahead", floor=cwa.percentage)


def _to_abs(cwa: CwaProgress, alignment: Alignment, rewind: int, duration: float,
            reason: str, floor: float | None = None,
            item_fraction: float | None = None, aligned_time: float | None = None) -> Decision:
    if aligned_time is not None:
        raw = max(0.0, min(aligned_time, duration or aligned_time))
        seconds = max(0.0, raw - rewind)
        if floor is not None and seconds <= floor:
            return Decision("in_sync", f"{reason}, but the rewound position is not past the audiobook's",
                            tier="aligned")
        return Decision("to_abs", reason, tier="aligned", abs_seconds=seconds, abs_raw_seconds=raw)
    t = ebook_to_audio(cwa, alignment, rewind, duration, item_fraction)
    if t.tier == "none":
        return Decision("none", t.note or "no translatable ebook position")
    # The rewind must never drag the audiobook backwards past where it already is.
    if floor is not None and t.seconds <= floor:
        return Decision("in_sync", f"{reason}, but the rewound position is not past the audiobook's",
                        tier=t.tier)
    return Decision("to_abs", reason, tier=t.tier,
                    abs_seconds=t.seconds, abs_raw_seconds=t.raw_seconds)


def _to_cwa(listening: AbsProgress, alignment: Alignment, reason: str,
            floor: float | None = None, aligned: tuple[int, float] | None = None) -> Decision:
    if aligned is not None:
        spine, fraction = aligned
        point = next((p for p in alignment.points if p.spine_index == spine), None)
        if point is not None and alignment.total_chars > 0:
            pct = 100.0 * (point.text_start + fraction * point.text_chars) / alignment.total_chars
            if floor is not None and pct <= floor:
                return Decision("in_sync", f"{reason}, but the ebook is already further by percentage",
                                tier="aligned")
            return Decision("to_cwa", reason, tier="aligned", cwa_percentage=pct,
                            cwa_spine_index=spine, cwa_item_fraction=fraction)
    pct, spine, tier = audio_to_ebook(listening.current_time, alignment)
    if tier == "none":
        return Decision("none", "no translatable audiobook position")
    if floor is not None and pct <= floor:
        return Decision("in_sync", f"{reason}, but the ebook is already further by percentage",
                        tier=tier)
    located = audio_item_fraction(listening.current_time, alignment)
    return Decision("to_cwa", reason, tier=tier, cwa_percentage=pct, cwa_spine_index=spine,
                    cwa_item_fraction=located[1] if located else None)
