"""Does slicing change alignment, and what actually drives emission memory?

Runs inside concordance-aligner:dev. One mode per process so peak RSS is clean:

  python contrib/experiments/slicing_test.py run  <slice_seconds> <batch_size> <tag>
  python contrib/experiments/slicing_test.py compare <tag> <tag> ...

Inputs come from /work/params.json (manifest, range, book file, spines).
"""
import json
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, "/app")
from concordance.aligner import align_text, decode_audio, peak_rss_mb, sliced_emissions  # noqa: E402
from concordance.xpointer import parse_document, spine_documents  # noqa: E402

WORK = Path("/work")


def run(slice_seconds: int, batch_size: int, tag: str) -> None:
    from ctc_forced_aligner import load_alignment_model

    params = json.loads((WORK / "params.json").read_text())
    torch.set_num_threads(8)
    model, tokenizer = load_alignment_model("cpu", dtype=torch.float32)
    rss_model = peak_rss_mb()
    manifest = [(Path(f["path"]), float(f["duration"])) for f in params["manifest"]]
    wav = decode_audio(manifest, params["start"], params["end"])
    items = spine_documents(Path(params["book_file"]))
    text = " ".join(parse_document(items[i]).text for i in range(params["spines"][0], params["spines"][1] + 1))
    t = time.time()
    emissions, stride = sliced_emissions(model, wav, slice_seconds, batch_size)
    emit_s = time.time() - t
    peak_emit = peak_rss_mb()
    words = align_text(emissions, stride, tokenizer, text)
    out = {"tag": tag, "slice_seconds": slice_seconds, "batch_size": batch_size,
           "audio_seconds": round(wav.size(0) / 16000, 1), "frames": emissions.size(0),
           "emissions_s": round(emit_s, 1), "rss_after_model_mb": rss_model,
           "peak_rss_emissions_mb": peak_emit, "peak_rss_total_mb": peak_rss_mb(),
           "words": len(words), "mean_score": round(sum(w["score"] for w in words) / len(words), 4)}
    (WORK / f"words_{tag}.json").write_text(json.dumps(words))
    torch.save(emissions, WORK / f"emissions_{tag}.pt")
    print(json.dumps(out))


def compare(tags: list[str]) -> None:
    base = json.loads((WORK / f"words_{tags[0]}.json").read_text())
    base_em = torch.load(WORK / f"emissions_{tags[0]}.pt")
    for tag in tags[1:]:
        other = json.loads((WORK / f"words_{tag}.json").read_text())
        em = torch.load(WORK / f"emissions_{tag}.pt")
        n = min(len(base), len(other))
        diffs = sorted(abs(base[i]["start"] - other[i]["start"]) for i in range(n))
        frames = min(base_em.size(0), em.size(0))
        frame_diff = (base_em[:frames] - em[:frames]).abs().max(dim=1).values
        print(json.dumps({
            "vs": f"{tags[0]} vs {tag}", "frames": [base_em.size(0), em.size(0)],
            "max_logprob_diff": round(float(frame_diff.max()), 4),
            "frames_differing_over_0.1": int((frame_diff > 0.1).sum()),
            "word_start_diff_median_s": round(diffs[len(diffs) // 2], 3),
            "word_start_diff_max_s": round(diffs[-1], 3),
            "words_over_0.1s": sum(d > 0.1 for d in diffs),
        }))


if __name__ == "__main__":
    if sys.argv[1] == "run":
        run(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    else:
        compare(sys.argv[2:])
