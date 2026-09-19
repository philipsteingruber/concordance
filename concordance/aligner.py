"""Forced-alignment worker: emissions in slices, align a chapter group, write the cache.

Runs inside the aligner container (`docker/aligner.Dockerfile`), which provides
torch, ctc-forced-aligner and ffmpeg. Nothing else in `concordance` imports this
module, so sync and reporting work on a host without torch.

Memory is set by batch size, not audio length (measured 2026-09-17 on a 6-min
chapter): batch 4 peaked at 4,167 MB, batch 1 at 2,438 MB with bit-identical
word timings and 13% more time. A 40-min chapter at batch 4 had peaked at
4,971 MB, so length adds little. Defaults are therefore batch 1, no slicing.

Slicing (`--slice-seconds`, a multiple of 30) is kept but off by default: it
saved no memory and ran 55% slower. Known defect: sliced emissions come out
shifted by one 20 ms frame relative to a single pass (word timings still agree
within 0.02 s). Fix before relying on it.

Usage (inside the container):

    python -m concordance.aligner \\
        --book-file /library/.../Book.kepub --fmt kepub --spines 10-10 \\
        --audio-manifest manifest.json --start 10293 --end 12711 \\
        --calibre-id 463 --library-item-id c7f6... --chapters 7-7
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from .config import ConfigError, env_number

SAMPLING_FREQ = 16_000
FRAME_SAMPLES = 320            # MMS / wav2vec2 stride: 20 ms per emission frame
CONTEXT_SECONDS = 2            # matches generate_emissions' default context_length
MODEL_ID = "MahmoudAshraf/mms-300m-1130-forced-aligner"


def peak_rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def _decode_file(path: Path, start: float, duration: float):
    import torch

    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}", "-t",
           f"{duration:.3f}", "-i", str(path), "-f", "s16le", "-ac", "1",
           "-acodec", "pcm_s16le", "-ar", str(SAMPLING_FREQ), "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed on {path.name}: {exc.stderr.decode(errors='replace')}") from exc
    return torch.frombuffer(bytearray(raw), dtype=torch.int16).to(torch.float32) / 32768.0


def decode_audio(manifest: list[tuple[Path, float]], start: float, end: float):
    """Decode book time [start, end) to a mono 16 kHz tensor.

    `manifest` is the book's audio files in playback order with durations, as
    ABS lists them. ABS chapter times run across the whole book, so a range can
    begin in one file and end in another (The Builders: 52 MP3s; most books: one
    .m4b). Each overlapping file is decoded for its share and concatenated.
    """
    import torch

    pieces, file_start = [], 0.0
    for path, duration in manifest:
        file_end = file_start + duration
        lo, hi = max(start, file_start), min(end, file_end)
        if hi > lo:
            pieces.append(_decode_file(path, lo - file_start, hi - lo))
        file_start = file_end
        if file_start >= end:
            break
    if not pieces:
        raise RuntimeError(f"no audio in manifest covers {start:.1f}-{end:.1f}s")
    return torch.cat(pieces)


# --- memory model ---------------------------------------------------------------
#
# Fitted to measured batch-1 runs (2026-09-17) inside the aligner image:
#   The Builders ch 6:  358 s audio,  3,628 alnum chars -> peak 2,438 MB
#   Lucky Day ch 7:   2,418 s audio, 25,782 alnum chars -> peak 3,024 MB
# The emissions phase grows slowly with audio length. The Viterbi phase adds
# backpointers of 2 bits x (2L+2) x (T-L) (ctc-forced-aligner's
# forced_align_impl.cpp), L = alphanumeric characters, T = 50 frames/s. That
# formula gave 1,226 MB for Lucky Day ch 7 against 1,178 MB measured. PyTorch
# keeps memory freed after emissions and reuses it, so the phases don't add;
# `retained` is fitted so the Viterbi phase reproduces the 3,024 MB peak.
VITERBI_SAFETY = 1.15        # headroom on the only term that grows quadratically


def viterbi_mb(audio_seconds: float, alnum_chars: int) -> float:
    frames = audio_seconds * 50.0
    return max(0.0, (2 * alnum_chars + 2) * max(frames - alnum_chars, 0.0) * 2 / 8 / 1e6)


def estimate_peak_mb(audio_seconds: float, alnum_chars: int) -> float:
    minutes = audio_seconds / 60.0
    emissions_phase = 2400.0 + 9.0 * minutes
    retained = 1690.0 + 2.7 * minutes
    return max(emissions_phase, retained + VITERBI_SAFETY * viterbi_mb(audio_seconds, alnum_chars))


def alnum_count(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def next_piece(token_spans: list[tuple[int, int]], first: int,
               seconds_per_char: float, target_seconds: float,
               tail_fraction: float = 0.25) -> int:
    """Index one past the last token of the next chunk.

    Takes tokens from `first` until the estimated narration time of the piece
    reaches `target_seconds`, always at least one token. Splits only between
    words, so every chunk's text is a run of whole tokens. A leftover shorter
    than `tail_fraction` of the target joins this piece rather than becoming a
    runt chunk that pays for its own model pass (seen live: a 119-word fifth
    chunk). The caller's budget check still applies to the enlarged piece, and
    halving the target shrinks the absorbed tail with it.
    """
    start_char = token_spans[first][0]
    last = first
    for i in range(first, len(token_spans)):
        if (token_spans[i][1] - start_char) * seconds_per_char > target_seconds and i > first:
            break
        last = i
    if last + 1 < len(token_spans):
        tail = (token_spans[-1][1] - token_spans[last + 1][0]) * seconds_per_char
        if tail < tail_fraction * target_seconds:
            return len(token_spans)
    return last + 1


def sliced_emissions(model, waveform, slice_seconds: int = 0, batch_size: int = 1):
    """Emissions for a waveform, computed slice by slice with context padding.

    `slice_seconds` must be a multiple of 30 so slice seams fall on the model's
    own window boundaries. A value of 0 disables slicing (one pass).
    """
    import torch
    from ctc_forced_aligner import generate_emissions

    if slice_seconds and slice_seconds % 30:
        raise ValueError("slice_seconds must be a multiple of 30")
    total = waveform.size(0)
    if not slice_seconds or total <= slice_seconds * SAMPLING_FREQ:
        return generate_emissions(model, waveform, batch_size=batch_size)

    step = slice_seconds * SAMPLING_FREQ
    pad = CONTEXT_SECONDS * SAMPLING_FREQ
    parts, stride = [], None
    for start in range(0, total, step):
        end = min(start + step, total)
        lo, hi = max(0, start - pad), min(total, end + pad)
        emissions, stride = generate_emissions(model, waveform[lo:hi], batch_size=batch_size)
        head = (start - lo) // FRAME_SAMPLES
        keep = (end - start) // FRAME_SAMPLES
        parts.append(emissions[head:head + keep])
    return torch.cat(parts, dim=0), stride


STAR = "<star>"


def drop_untokenizable(tokens: list[str], texts: list[str]) -> tuple[list[str], list[str], list[bool]]:
    """Remove words the romanizer produced no tokens for. Returns the kept mask too.

    A bare numeral romanizes to an empty string: "17" yields "". `get_alignments`
    drops it when building the token indices, but `get_spans` still walks `tokens`
    positionally, so the empty word is handed back a span starting at frame 0 and
    the alignment of everything after it is read off by one.

    That is not cosmetic. A chapter opening usually begins with the chapter's
    number, so a caller reading the first word's time is told the chapter starts
    at the beginning of whatever audio was searched. A book that spells its
    chapters out ("One", "Two") aligns perfectly and one that uses digits fails
    completely - exactly the split between The Burn Palace and Misery.
    """
    kept = [bool(tok.strip()) for tok in tokens]
    return ([t for t, k in zip(tokens, kept) if k],
            [t for t, k in zip(texts, kept) if k],
            kept)


def restore_untokenizable(results: list[dict], texts: list[str], kept: list[bool]) -> list[dict]:
    """Put the dropped words back, so the output stays one entry per source word.

    `build_entry` pairs the aligner's output with the group text word by word and
    refuses a mismatch, which is worth keeping: it is the check that would catch
    the aligner and the text drifting apart. A dropped word is restored with a
    zero-length span where the next aligned word begins, and that word's score,
    so it neither invents a duration nor moves the mean.
    """
    words = [t for t, star in zip(texts, (t == STAR for t in texts)) if not star]
    flags = [k for k, t in zip(kept, texts) if t != STAR]
    out, i = [], 0
    for text, keep in zip(words, flags):
        if keep:
            out.append(results[i])
            i += 1
            continue
        after = results[i] if i < len(results) else None
        before = out[-1] if out else None
        at = after["start"] if after else (before["end"] if before else 0.0)
        score = (after or before or {"score": 0.0})["score"]
        out.append({"start": at, "end": at, "text": text, "score": score})
    return out


def align_text(emissions, stride, tokenizer, text: str) -> list[dict]:
    """Word timings for `text` against emissions, with <star> at the edges only.

    Edge stars absorb unrelated audio before and after the text (announced
    titles, credits). The Python API defaults to star_frequency="segment"; only
    the CLI defaults to "edges", so it is set explicitly here.
    """
    from ctc_forced_aligner import get_alignments, get_spans, postprocess_results, preprocess_text

    tokens_starred, text_starred = preprocess_text(text, romanize=True, language="eng",
                                                   split_size="word", star_frequency="edges")
    tokens, texts, kept = drop_untokenizable(tokens_starred, text_starred)
    segments, scores, blank = get_alignments(emissions, tokens, tokenizer)
    spans = get_spans(tokens, segments, blank)
    results = postprocess_results(texts, spans, stride, scores)
    return restore_untokenizable(results, text_starred, kept)


def align_chunked(model, tokenizer, manifest, text: str, start: float, end: float,
                  budget_mb: float, target_seconds: float, end_margin: float,
                  batch_size: int, stats: dict, end_slack: float = 0.0) -> list[dict]:
    """Align a long group in pieces, each inside its own bounded audio window.

    Each piece is a run of whole words sized to ~target_seconds of narration.
    Its audio window starts where the previous piece's last word actually ended
    (minus a little overlap), so estimation error can't accumulate across
    chunks; only the far end needs slack (`end_margin` plus 15%). Edge <star>
    tokens absorb the extra audio. If a piece's last word lands against the
    window's end, the window was too short: it is retried with double the
    margin. Every window is checked against the memory budget before any audio
    is decoded; an over-budget piece is shrunk rather than run.

    `end` comes from the boundary matcher's coarse spine-to-chapter grid, which
    on some books runs hundreds of seconds early (Misery: 530 s median, p90
    671). It is therefore a soft target: a piece may reach up to `end_slack`
    past it, and the wall test applies to the last piece too. Before 2026-09-19
    it did not -- the final piece was pinned to `end` and excluded from the
    retry, making it the one piece that could neither detect nor recover from a
    short window, so its text was crammed against the boundary at 3-4x
    narration rate.
    """
    import re as _re

    spans = [(m.start(), m.end()) for m in _re.finditer(r"\S+", text)]
    words: list[dict] = []
    cursor_token, cursor_time = 0, start
    chunks = []
    hard_end = end + max(0.0, end_slack)
    while cursor_token < len(spans):
        remaining_chars = max(1, spans[-1][1] - spans[cursor_token][0])
        rate = max(end - cursor_time, 1.0) / remaining_chars
        target, margin = target_seconds, end_margin
        piece_words, attempt, peak = [], 0, 0.0
        for attempt in range(6):
            stop = next_piece(spans, cursor_token, rate, target)
            piece = text[spans[cursor_token][0]:spans[stop - 1][1]]
            est = (spans[stop - 1][1] - spans[cursor_token][0]) * rate
            w_start = max(start, cursor_time - 5.0)
            reach = cursor_time + est * 1.15 + margin
            # The last piece still claims at least everything up to `end`, so a
            # book whose grid is accurate behaves exactly as it did before.
            w_end = min(hard_end, max(end, reach) if stop == len(spans) else reach)
            peak = estimate_peak_mb(w_end - w_start, alnum_count(piece))
            if peak > budget_mb and stop - cursor_token > 1:
                target /= 2            # shrink the piece; never run over budget
                continue
            wav = decode_audio(manifest, w_start, w_end)
            emissions, stride = sliced_emissions(model, wav, 0, batch_size)
            del wav
            piece_words = align_text(emissions, stride, tokenizer, piece)
            del emissions
            hit_wall = (w_end < hard_end and piece_words
                        and piece_words[-1]["end"] >= (w_end - w_start) - 2.0)
            if hit_wall and attempt < 5:
                margin *= 2
                continue
            break
        for w in piece_words:
            w = dict(w)
            w["start"] += w_start - start      # relative to the group start, like a single pass
            w["end"] += w_start - start
            words.append(w)
        chunks.append({"tokens": [cursor_token, stop], "window": [round(w_start, 1), round(w_end, 1)],
                       "est_peak_mb": round(peak), "attempts": attempt + 1})
        cursor_token = stop
        cursor_time = start + words[-1]["end"]
    stats["chunks"] = chunks
    return words


def _range(value: str) -> tuple[int, int]:
    first, _, last = value.partition("-")
    return int(first), int(last or first)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="concordance.aligner")
    ap.add_argument("--book-file", type=Path, required=True)
    ap.add_argument("--fmt", choices=("kepub", "epub"), required=True)
    ap.add_argument("--spines", type=_range, required=True, help="first-last spine index")
    ap.add_argument("--audio-manifest", type=Path, required=True,
                    help='JSON list of {"path": ..., "duration": seconds} in playback order')
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--calibre-id", type=int, required=True)
    ap.add_argument("--library-item-id", required=True)
    ap.add_argument("--chapters", type=_range, required=True, help="first-last ABS chapter")
    ap.add_argument("--slice-seconds", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=int(env_number("CONCORDANCE_ALIGN_BATCH_SIZE", 1, int)),
                    help="emission batch size; 1 uses ~40%% less memory than 4 for ~13%% more time")
    ap.add_argument("--threads", type=int, default=int(env_number("CONCORDANCE_ALIGNER_THREADS", 0, int)),
                    help="torch CPU threads (default: CONCORDANCE_ALIGNER_THREADS, or torch's own choice)")
    ap.add_argument("--aligner-commit", default="unknown")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--memory-budget-mb", type=float,
                    default=env_number("CONCORDANCE_ALIGN_BUDGET_MB", 3600),
                    help="estimated peak above this switches to chunked alignment")
    ap.add_argument("--chunk-seconds", type=float, default=env_number("CONCORDANCE_ALIGN_CHUNK_SECONDS", 1500),
                    help="target narration per chunk when chunking")
    ap.add_argument("--end-margin", type=float, default=env_number("CONCORDANCE_ALIGN_END_MARGIN", 300),
                    help="extra audio past each chunk's estimated end")
    ap.add_argument("--end-slack", type=float, default=env_number("CONCORDANCE_ALIGN_END_SLACK", 2400),
                    help="how far past --end the last words may be sought when the "
                         "anchor grid's group end lands early (0 restores the pre-2026-09-19 "
                         "hard boundary)")
    ap.add_argument("--force-chunks", action="store_true",
                    help="chunk even when a single pass fits (for testing)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    import torch
    from ctc_forced_aligner import load_alignment_model

    from .cache import AlignmentCache, GroupKey, build_entry, manifest_fingerprint
    from .xpointer import parse_document, spine_documents

    if args.threads > 0:
        torch.set_num_threads(args.threads)
    stats: dict = {}
    t0 = time.time()
    model, tokenizer = load_alignment_model("cpu", dtype=torch.float32)
    stats["model_load_s"] = round(time.time() - t0, 1)

    items = spine_documents(args.book_file)
    first, last = args.spines
    item_texts = [(i, parse_document(items[i]).text) for i in range(first, last + 1) if i in items]
    group_text = " ".join(t for _, t in item_texts)

    manifest = [(Path(f["path"]), float(f["duration"])) for f in json.loads(args.audio_manifest.read_text())]
    stats["audio_seconds"] = round(args.end - args.start, 1)
    stats["estimated_peak_mb"] = round(estimate_peak_mb(args.end - args.start, alnum_count(group_text)))
    chunked = args.force_chunks or stats["estimated_peak_mb"] > args.memory_budget_mb
    stats["mode"] = "chunked" if chunked else "single"

    t = time.time()
    if chunked:
        words = align_chunked(model, tokenizer, manifest, group_text, args.start, args.end,
                              args.memory_budget_mb, args.chunk_seconds, args.end_margin,
                              args.batch_size, stats, args.end_slack)
    else:
        # Same short-window problem as the chunked path's last piece: `args.end`
        # is the coarse anchor grid's guess, and if the narration runs past it
        # the tail text has nowhere to go. Retry against a wider window, and
        # hand over to the chunked path if that no longer fits the budget.
        span_end, attempts = args.end, []
        while True:
            waveform = decode_audio(manifest, args.start, span_end)
            emissions, stride = sliced_emissions(model, waveform, args.slice_seconds, args.batch_size)
            del waveform
            stats["peak_rss_after_emissions_mb"] = peak_rss_mb()
            words = align_text(emissions, stride, tokenizer, group_text)
            del emissions
            attempts.append(round(span_end, 1))
            hit_wall = (words and span_end < args.end + args.end_slack
                        and words[-1]["end"] >= (span_end - args.start) - 2.0)
            if not hit_wall:
                break
            span_end = min(args.end + args.end_slack, span_end + max(args.end_margin, 60.0))
            if estimate_peak_mb(span_end - args.start, alnum_count(group_text)) > args.memory_budget_mb:
                stats["mode"] = "chunked-after-wall"
                words = align_chunked(model, tokenizer, manifest, group_text, args.start, args.end,
                                      args.memory_budget_mb, args.chunk_seconds, args.end_margin,
                                      args.batch_size, stats, args.end_slack)
                break
        if len(attempts) > 1:
            stats["single_pass_windows"] = attempts
    stats["align_total_s"] = round(time.time() - t, 1)
    stats["peak_rss_mb"] = peak_rss_mb()
    stats["words"] = len(words)
    stats["mean_score"] = round(sum(w["score"] for w in words) / max(1, len(words)), 4)

    aligner = {"name": "ctc-forced-aligner", "model": MODEL_ID, "commit": args.aligner_commit}
    key = GroupKey(calibre_book_id=args.calibre_id, library_item_id=args.library_item_id,
                   fmt=args.fmt, first_spine=first, last_spine=last,
                   first_chapter=args.chapters[0], last_chapter=args.chapters[1])
    # The words may now run past the grid's `end` (see align_chunked). Lookups
    # gate on `audio_start <= t < audio_end`, so an entry that stopped at `end`
    # would hold word timings it refuses to answer for. Widen to cover them;
    # never narrow, so an entry still spans the group it was asked for.
    covered_end = args.start + max((w["end"] for w in words), default=0.0)
    entry_end = max(args.end, covered_end)
    stats["audio_end_extended_s"] = round(entry_end - args.end, 1)
    entry = build_entry(key, item_texts, words, args.start, entry_end,
                        args.book_file, None, aligner,
                        audio_fingerprint=manifest_fingerprint([path for path, _ in manifest]))
    if not args.no_save:
        stats["saved_to"] = str(AlignmentCache().save(entry))
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
