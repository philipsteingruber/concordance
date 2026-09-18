"""Find when chapter openings are spoken. Runs inside the aligner image.

Input is a JSON file written by `concordance.chapters`:

    {"manifest": [[path, seconds], ...],
     "anchors": [[book_char, audio_seconds], ...],      # measured points only
     "total_chars": 634647,                             # for the fallback rate
     "targets": [{"id": 3, "pos": 81234, "text": "Chapter 3 The next morning..."}, ...],
     "min_score": -0.5}

Targets are handled in book order. Each chapter start is estimated by
interpolating between the nearest known points, and its opening words are
aligned inside an audio window around the estimate. A confirmed start becomes a
known point itself, so estimation error never builds up across a long stretch:
the next chapter is only ever estimated from the last one found. A window that
misses (low score, or the words land against its edge) is doubled once.

Chapter starts must come out strictly increasing, so a confirmed start is also
required to be later than the one before it. That backstop does not depend on
any theory about why a window was wrong, which is the point of it: an earlier
attempt to be clever about which edges count "confirmed" three consecutive
chapters at one timestamp.

One JSON line per target goes to stdout as it's decided.
"""

from __future__ import annotations

import bisect
import json
import statistics
import sys
import time
from pathlib import Path

MIN_HALF_WINDOW = 60.0
MAX_HALF_WINDOW = 480.0
EDGE_SECONDS = 3.0
SCORED_WORDS = 12


def observed_rate(anchors: list[tuple[int, float]], fallback: float) -> float:
    """Characters of book per second of audio: the median gap between anchors.

    The median rather than the most recent gap, which is noisy enough to matter
    when it is extrapolated across the rest of a book. On the book this was built
    for, the last gap alone says 12.31 characters per second and the median says
    14.02, against 14.05 measured across the whole narrated span; the error from
    the last gap reaches six minutes by the end of the tail.

    `fallback` is the whole-book rate, for a book with no measured pair yet -
    one whose very first chapter failed to confirm.
    """
    rates = [(b[0] - a[0]) / (b[1] - a[1])
             for a, b in zip(anchors, anchors[1:]) if b[1] > a[1] and b[0] > a[0]]
    return statistics.median(rates) if rates else fallback


def estimate(anchors: list[tuple[int, float]], pos: int, rate: float | None = None,
             limit: float | None = None) -> tuple[float, float, float]:
    """(estimated seconds, seconds of the known point before, seconds of the known point after).

    Past the last anchor the position is extrapolated at `rate` rather than
    pinned to the last known time. Anchors are measurements - a cached word
    timing, or a chapter already confirmed - and the end of the audio is not one
    of them: an ebook routinely carries matter the audiobook never reads. Misery
    ends with a preview of a different novel, 3.3% of the text and about twenty
    minutes of narration that does not exist, and treating the last character as
    the last second dragged every estimate after the final confirmed chapter far
    enough early that none of them could be found.
    """
    positions = [a[0] for a in anchors]
    i = bisect.bisect_right(positions, pos)
    before = anchors[max(i - 1, 0)]
    if i >= len(anchors):
        if rate and rate > 0:
            seconds = before[1] + (pos - before[0]) / rate
            return (min(seconds, limit) if limit else seconds), before[1], limit or seconds
        return before[1], before[1], limit or before[1]
    after = anchors[i]
    if after[0] == before[0]:
        return before[1], before[1], after[1]
    fraction = (pos - before[0]) / (after[0] - before[0])
    return before[1] + fraction * (after[1] - before[1]), before[1], after[1]


def half_window(est: float, known_before: float) -> float:
    """Wider the further the estimate is from the last known point.

    Was briefly sized from the whole gap being interpolated across, to fix probes
    that kept landing on a window edge. That turned out to be an untokenizable
    first word rather than a narrow window - a controlled test put the same
    passage at the same wrong place with the window at +-300 s and at +-960 s -
    and the wider windows cost about fifteen minutes per chapter instead of two.
    """
    return min(MAX_HALF_WINDOW, max(MIN_HALF_WINDOW, 0.06 * (est - known_before) + 45.0))


def judge(words: list[dict], lo: float, hi: float, min_score: float,
          floor: float | None = None) -> tuple[bool, float, float, str]:
    """(accepted, absolute start, mean score of the opening words, reason if rejected).

    A match against either edge is rejected however well it scores. The aligner
    always returns its best placement inside the span it was given, so text that
    is not in that span comes back pinned to an end - with a good score, because
    the score measures how well those words match wherever they were put, not
    whether they belong there. `floor` is the previous confirmed chapter: a start
    at or before it is impossible whatever the window did.
    """
    if not words:
        return False, lo, -99.0, "no words"
    head = words[:SCORED_WORDS]
    score = sum(w["score"] for w in head) / len(head)
    start = lo + words[0]["start"]
    if score < min_score:
        return False, start, score, "score"
    if start - lo < EDGE_SECONDS:
        return False, start, score, "pinned to the start of the search span"
    if hi - start < EDGE_SECONDS:
        return False, start, score, "pinned to the end of the search span"
    if floor is not None and start <= floor:
        return False, start, score, "at or before the previous chapter"
    return True, start, score, ""


def main(argv: list[str] | None = None) -> int:
    import torch
    from ctc_forced_aligner import load_alignment_model

    from .aligner import align_text, decode_audio, sliced_emissions

    job = json.loads(Path((argv or sys.argv[1:])[0]).read_text())
    manifest = [(Path(p), float(d)) for p, d in job["manifest"]]
    anchors = sorted((int(p), float(t)) for p, t in job["anchors"])
    min_score = float(job.get("min_score", -0.5))
    total = sum(d for _, d in manifest)
    # Whole-book characters per second, for a book with no measured rate yet.
    fallback_rate = (float(job["total_chars"]) / total) if job.get("total_chars") and total else 0.0
    threads = int(job.get("threads") or 0)
    if threads > 0:
        torch.set_num_threads(threads)
    model, tokenizer = load_alignment_model("cpu", dtype=torch.float32)

    last_confirmed: float | None = None
    for target in sorted(job["targets"], key=lambda t: t["pos"]):
        t0 = time.time()
        est, known_before, known_after = estimate(
            anchors, target["pos"], rate=observed_rate(anchors, fallback_rate), limit=total)
        half = half_window(est, known_before)
        result = {"id": target["id"], "estimate": round(est, 1), "status": "unconfirmed"}
        for attempt in range(2):
            lo = max(0.0, known_before, est - half)
            hi = min(total, known_after if known_after > known_before else total, est + half)
            if hi - lo < 10:
                break
            emissions, stride = sliced_emissions(model, decode_audio(manifest, lo, hi), 0, 1)
            words = align_text(emissions, stride, tokenizer, target["text"])
            ok, start, score, why = judge(words, lo, hi, min_score, floor=last_confirmed)
            result.update({"window": [round(lo, 1), round(hi, 1)], "score": round(score, 3),
                           "landed": round(start, 1), "attempts": attempt + 1,
                           "extrapolated": target["pos"] > anchors[-1][0]})
            result["why"] = why
            if ok:
                result.update({"status": "confirmed", "time": round(start, 2)})
                result.pop("why", None)
                bisect.insort(anchors, (int(target["pos"]), start))
                last_confirmed = start
                break
            half = min(MAX_HALF_WINDOW * 2, half * 2)
        result["seconds"] = round(time.time() - t0)
        print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
