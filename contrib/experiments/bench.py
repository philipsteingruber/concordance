"""ctc-forced-aligner benchmark for Concordance. Run inside a scratch container with
the work dir mounted at /work (chapter.wav, chapter.txt, passage.txt, wrong.txt).
Setup, inputs and results: docs/aligner.md.

  python bench.py emissions          # model load + emissions for chapter.wav
  python bench.py align <txt> <tag>  # alignment against cached emissions
"""
import json, sys, time
import torch
from ctc_forced_aligner import (load_audio, load_alignment_model, generate_emissions,
                                preprocess_text, get_alignments, get_spans, postprocess_results)

def mem(field):  # VmRSS = current, VmHWM = peak, in MB
    for line in open("/proc/self/status"):
        if line.startswith(field):
            return int(line.split()[1]) // 1024

torch.set_num_threads(int(sys.argv[-1]) if sys.argv[-1].isdigit() else 8)
t0 = time.time()
model, tokenizer = load_alignment_model("cpu", dtype=torch.float32)
out = {"model_load_s": round(time.time() - t0, 1), "rss_after_model_mb": mem("VmRSS")}

if sys.argv[1] == "emissions":
    t = time.time()
    wav = load_audio("/work/chapter.wav", model.dtype, model.device)
    out["audio_load_s"] = round(time.time() - t, 1)
    t = time.time()
    emissions, stride = generate_emissions(model, wav, batch_size=4)
    out["emissions_s"] = round(time.time() - t, 1)
    out["audio_seconds"] = round(wav.shape[-1] / 16000, 1)
    torch.save({"emissions": emissions, "stride": stride}, "/work/emissions.pt")
    out["peak_rss_mb"] = mem("VmHWM")
    print(json.dumps(out)); json.dump(out, open("/work/result_emissions.json", "w"), indent=2)
    sys.exit(0)

txt_path, tag = sys.argv[2], sys.argv[3]
cached = torch.load("/work/emissions.pt")
text = open(txt_path).read().replace("\n", " ").strip()
t = time.time()
tokens_starred, text_starred = preprocess_text(text, romanize=True, language="eng",
                                               split_size="word", star_frequency="edges")
out["preprocess_s"] = round(time.time() - t, 1)
rss_before = mem("VmRSS")
t = time.time()
segments, scores, blank = get_alignments(cached["emissions"], tokens_starred, tokenizer)
spans = get_spans(tokens_starred, segments, blank)
words = postprocess_results(text_starred, spans, cached["stride"], scores)
out["align_s"] = round(time.time() - t, 1)
out["align_peak_extra_mb"] = mem("VmHWM") - rss_before
out["words"] = len(words)
out["mean_score"] = round(sum(w["score"] for w in words) / max(1, len(words)), 4)
out["first"] = words[0] if words else None
out["last"] = words[-1] if words else None
json.dump({"summary": out, "words": words}, open(f"/work/result_{tag}.json", "w"))
print(json.dumps(out))
