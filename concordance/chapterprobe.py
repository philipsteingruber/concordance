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


def half_window(est: float, known_before: float) -> float:
    """Wider the further the estimate is from the last known point."""
    return min(MAX_HALF_WINDOW, max(MIN_HALF_WINDOW, 0.06 * (est - known_before) + 45.0))


def judge(words: list[dict], lo: float, hi: float, min_score: float) -> tuple[bool, float, float]:
    """(accepted, absolute start, mean score of the opening words)."""
    if not words:
        return False, lo, -99.0
    head = words[:SCORED_WORDS]
    score = sum(w["score"] for w in head) / len(head)
    start = lo + words[0]["start"]
    inside = start - lo >= EDGE_SECONDS and hi - start >= EDGE_SECONDS
    return score >= min_score and inside, start, score


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
        half = half_window(est, known_before)
        result = {"id": target["id"], "estimate": round(est, 1), "status": "unconfirmed"}
        for attempt in range(2):
            lo = max(0.0, known_before, est - half)
            hi = min(total, known_after if known_after > known_before else total, est + half)
            if hi - lo < 10:
                break
            emissions, stride = sliced_emissions(model, decode_audio(manifest, lo, hi), 0, 1)
            words = align_text(emissions, stride, tokenizer, target["text"])
            ok, start, score = judge(words, lo, hi, min_score)
            result.update({"window": [round(lo, 1), round(hi, 1)], "score": round(score, 3),
                           "attempts": attempt + 1})
            if ok:
                result.update({"status": "confirmed", "time": round(start, 2)})
                bisect.insort(anchors, (int(target["pos"]), start))
                break
            half = min(MAX_HALF_WINDOW * 2, half * 2)
        result["seconds"] = round(time.time() - t0)
        print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
