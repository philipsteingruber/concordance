"""Find when chapter openings are spoken. Runs inside the aligner image.

Input is a JSON file written by `concordance.chapters`:

    {"manifest": [[path, seconds], ...],
     "anchors": [[book_char, audio_seconds], ...],      # known points, at least start and end
     "targets": [{"id": 3, "pos": 81234, "text": "Chapter 3 The next morning..."}, ...],
     "min_score": -1.0}

Targets are handled in book order. Each chapter start is estimated by
interpolating between the nearest known points, and its opening words are
aligned inside an audio window around the estimate. A confirmed start becomes a
known point itself, so estimation error never builds up across a long stretch:
the next chapter is only ever estimated from the last one found. A window that
misses (low score, or the words land against its edge) is doubled once.

The edge test applies only to edges the *window* created. A window is also
clamped to the known points either side, and a chapter start may legitimately
sit right on one of those - a cached anchor is often itself a chapter start -
so rejecting a match for touching a clamp would throw away the correct answer,
and doubling the window could never recover it.

One JSON line per target goes to stdout as it's decided.
"""

from __future__ import annotations

import bisect
import json
import sys
import time
from pathlib import Path

MIN_HALF_WINDOW = 60.0
MAX_HALF_WINDOW = 480.0
# The estimate interpolates proportionally across the gap between the two known
# points either side, and that fallback's error is linear in the gap: measured at
# worst ~2% of it (see docs/aligner.md). The window is built from the same rule
# with room to spare, because a window that is too narrow costs a whole retry.
SPAN_FRACTION = 0.05
EDGE_SECONDS = 3.0
SCORED_WORDS = 12


def estimate(anchors: list[tuple[int, float]], pos: int) -> tuple[float, float, float]:
    """(estimated seconds, seconds of the known point before, seconds of the known point after)."""
    positions = [a[0] for a in anchors]
    i = bisect.bisect_right(positions, pos)
    before = anchors[max(i - 1, 0)]
    after = anchors[min(i, len(anchors) - 1)]
    if after[0] == before[0]:
        return before[1], before[1], after[1]
    fraction = (pos - before[0]) / (after[0] - before[0])
    return before[1] + fraction * (after[1] - before[1]), before[1], after[1]


def half_window(known_before: float, known_after: float) -> float:
    """Sized from the gap being interpolated across, not from where in it the estimate falls.

    A target just past a known point still inherits the whole gap's error, so
    distance from that point is the wrong measure: it produced 60 s windows inside
    multi-hour gaps, and every target in them failed.
    """
    return min(MAX_HALF_WINDOW, max(MIN_HALF_WINDOW, SPAN_FRACTION * (known_after - known_before)))


def judge(words: list[dict], lo: float, hi: float, min_score: float,
          soft_lo: bool = True, soft_hi: bool = True) -> tuple[bool, float, float, str]:
    """(accepted, absolute start, mean score of the opening words, reason if rejected).

    `soft_lo`/`soft_hi` say whether that edge was made by the search window rather
    than by a known point. Only a soft edge means "the answer may be outside", so
    only a soft edge rejects.
    """
    if not words:
        return False, lo, -99.0, "no words"
    head = words[:SCORED_WORDS]
    score = sum(w["score"] for w in head) / len(head)
    start = lo + words[0]["start"]
    if score < min_score:
        return False, start, score, "score"
    if soft_lo and start - lo < EDGE_SECONDS:
        return False, start, score, "against the start of the window"
    if soft_hi and hi - start < EDGE_SECONDS:
        return False, start, score, "against the end of the window"
    return True, start, score, ""


def main(argv: list[str] | None = None) -> int:
    import torch
    from ctc_forced_aligner import load_alignment_model

    from .aligner import align_text, decode_audio, sliced_emissions

    job = json.loads(Path((argv or sys.argv[1:])[0]).read_text())
    manifest = [(Path(p), float(d)) for p, d in job["manifest"]]
    anchors = sorted((int(p), float(t)) for p, t in job["anchors"])
    min_score = float(job.get("min_score", -1.0))
    total = sum(d for _, d in manifest)
    threads = int(job.get("threads") or 0)
    if threads > 0:
        torch.set_num_threads(threads)
    model, tokenizer = load_alignment_model("cpu", dtype=torch.float32)

    for target in sorted(job["targets"], key=lambda t: t["pos"]):
        t0 = time.time()
        est, known_before, known_after = estimate(anchors, target["pos"])
        half = half_window(known_before, known_after if known_after > known_before else est)
        result = {"id": target["id"], "estimate": round(est, 1), "status": "unconfirmed"}
        for attempt in range(2):
            hard_lo = max(0.0, known_before)
            hard_hi = min(total, known_after if known_after > known_before else total)
            lo, hi = max(hard_lo, est - half), min(hard_hi, est + half)
            if hi - lo < 10:
                break
            emissions, stride = sliced_emissions(model, decode_audio(manifest, lo, hi), 0, 1)
            words = align_text(emissions, stride, tokenizer, target["text"])
            ok, start, score, why = judge(words, lo, hi, min_score,
                                          soft_lo=lo > hard_lo, soft_hi=hi < hard_hi)
            result.update({"window": [round(lo, 1), round(hi, 1)], "score": round(score, 3),
                           "landed": round(start, 1), "attempts": attempt + 1})
            if why:
                result["why"] = why
            if ok:
                result.update({"status": "confirmed", "time": round(start, 2)})
                result.pop("why", None)
                bisect.insort(anchors, (int(target["pos"]), start))
                break
            half = min(MAX_HALF_WINDOW * 2, half * 2)
        result["seconds"] = round(time.time() - t0)
        print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
