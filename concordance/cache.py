"""On-disk cache of forced-alignment results, and lookups in both directions.

An entry covers one matched *group*: a run of spine items aligned against a run
of audio chapters (see `anchor.py`). It stores one record per word of the
group's canonical text: character span, time span, and the aligner's score.
Once written, both sync directions become lookups:

* ebook -> audio: (spine index, offset in that item's canonical text) -> seconds
* audio -> ebook: seconds -> (spine index, offset), which `xpointer.to_xpointer`
  turns into a real KOReader position

**Group text** is each spine item's canonical text (`xpointer.parse_document`)
joined with a single space. That is exactly the text fed to the aligner.
ctc-forced-aligner's word mode tokenises with `str.split()`, and canonical text
has whitespace collapsed, so aligner word *i* is the *i*th `\\S+` match in the
group text. `build_entry` relies on that and checks it word by word.

**Invalidation:** an entry records the size and mtime of the book file and the
audio file it was built from, plus the text-extraction and aligner versions.
If any of them differs, the entry is stale and gets rebuilt rather than trusted.
Emissions are the expensive part (~22 CPU-min per audio-hour); this cache is
what lets that cost be paid once per chapter.
"""

from __future__ import annotations

import bisect
import gzip
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

CACHE_VERSION = 1
TEXT_EXTRACTION = "xpointer-canonical-v1"
_WORD = re.compile(r"\S+")


class CacheError(RuntimeError):
    """Raised when aligner output doesn't fit the group text it claims to align."""


@dataclass(frozen=True, eq=False)
class FileFingerprint:
    path: str        # informational only: the container and host mount files at different paths
    size: int
    mtime_ns: int

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileFingerprint):
            return NotImplemented
        return self.size == other.size and self.mtime_ns == other.mtime_ns

    def __hash__(self) -> int:
        return hash((self.size, self.mtime_ns))

    @classmethod
    def of(cls, path: Path) -> "FileFingerprint":
        st = path.stat()
        return cls(path=str(path), size=st.st_size, mtime_ns=st.st_mtime_ns)


def manifest_fingerprint(paths: list[Path]) -> FileFingerprint:
    """One fingerprint for a multi-file audiobook: total size and newest mtime."""
    prints = [FileFingerprint.of(path) for path in paths]
    return FileFingerprint(path=f"manifest:{len(prints)} files",
                           size=sum(p.size for p in prints),
                           mtime_ns=max(p.mtime_ns for p in prints))


@dataclass(frozen=True)
class GroupKey:
    calibre_book_id: int
    library_item_id: str
    fmt: str                 # "kepub" | "epub", the file whose text was aligned
    first_spine: int
    last_spine: int
    first_chapter: int
    last_chapter: int

    def filename(self) -> str:
        return (f"{self.calibre_book_id}/{self.fmt}-sp{self.first_spine}-{self.last_spine}"
                f"-ch{self.first_chapter}-{self.last_chapter}.json.gz")


@dataclass
class Entry:
    key: GroupKey
    # (spine index, canonical start of that item within the group text, item length)
    items: list[tuple[int, int, int]]
    audio_start: float
    audio_end: float
    book_file: FileFingerprint
    audio_file: FileFingerprint
    aligner: dict
    # One record per word: (char_start, char_end, t_start, t_end, score), times absolute.
    words: list[tuple[int, int, float, float, float]]

    # -- construction -----------------------------------------------------------

    @staticmethod
    def group_text(item_texts: list[tuple[int, str]]) -> tuple[str, list[tuple[int, int, int]]]:
        """Join spine-item canonical texts; return (text, [(spine, start, length)])."""
        parts, items, cursor = [], [], 0
        for spine_index, text in item_texts:
            if parts:
                parts.append(" ")
                cursor += 1
            items.append((spine_index, cursor, len(text)))
            parts.append(text)
            cursor += len(text)
        return "".join(parts), items

    # -- lookups ----------------------------------------------------------------

    def _item(self, spine_index: int) -> tuple[int, int, int]:
        for item in self.items:
            if item[0] == spine_index:
                return item
        raise KeyError(f"spine item {spine_index} is not in this group")

    def time_for(self, spine_index: int, item_offset: int) -> tuple[float, float]:
        """(seconds, score) for a character offset inside a spine item.

        Inside a word the time is interpolated across the word; between words
        (spaces, punctuation-only gaps) it snaps to the next word's start.
        """
        if not self.words:
            raise CacheError("entry has no words")
        _, start, length = self._item(spine_index)
        char = start + max(0, min(item_offset, max(length - 1, 0)))
        starts = [w[0] for w in self.words]
        i = bisect.bisect_right(starts, char) - 1
        if i < 0:
            w = self.words[0]
            return w[2], w[4]
        w = self.words[i]
        if char < w[1]:
            span = max(1, w[1] - w[0])
            return w[2] + (char - w[0]) / span * (w[3] - w[2]), w[4]
        nxt = self.words[i + 1] if i + 1 < len(self.words) else w
        return (nxt[2] if nxt is not w else w[3]), nxt[4]

    def position_for(self, seconds: float) -> tuple[int, int, float]:
        """(spine index, offset in that item, score) for an absolute audio time."""
        if not self.words:
            raise CacheError("entry has no words")
        t_starts = [w[2] for w in self.words]
        i = max(0, bisect.bisect_right(t_starts, seconds) - 1)
        w = self.words[i]
        char = w[0]
        for spine_index, start, length in reversed(self.items):
            if char >= start:
                return spine_index, min(char - start, max(length - 1, 0)), w[4]
        spine_index, start, _ = self.items[0]
        return spine_index, 0, w[4]

    def mean_score(self, around_seconds: float, window: float = 30.0) -> float | None:
        """Mean word score within ±window of a time: the confidence gate for a write.

        Positional on purpose. An entry can align well overall and still be
        rubbish in one stretch - Misery's spine 14 opens with 150 s of text
        crammed at four times narration speed - and a lookup lands at a
        position, not at an average.
        """
        scores = [w[4] for w in self.words if abs(w[2] - around_seconds) <= window]
        return sum(scores) / len(scores) if scores else None

    def item_bounds(self) -> list[tuple[int, int, float, int, float]]:
        """Per spine item: (spine, first offset, its time, last offset, its time).

        The ends of an item are the only points where the character-to-time rate
        is known to change abruptly, because the audio between two items can hold
        a part announcement or music that carries no book text at all.
        """
        out = []
        for spine, _, length in self.items:
            if length <= 0:
                continue
            last = length - 1
            out.append((spine, 0, self.time_for(spine, 0)[0], last, self.time_for(spine, last)[0]))
        return out

    # -- freshness --------------------------------------------------------------

    def is_fresh(self, book_path: Path, audio: Path | FileFingerprint, aligner: dict) -> bool:
        """`audio` is a single file, or a precomputed fingerprint for a multi-file book."""
        try:
            audio_print = audio if isinstance(audio, FileFingerprint) else FileFingerprint.of(audio)
            return (FileFingerprint.of(book_path) == self.book_file
                    and audio_print == self.audio_file
                    and self.aligner == aligner)
        except OSError:
            return False

    # -- serialisation ----------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "version": CACHE_VERSION,
            "text_extraction": TEXT_EXTRACTION,
            "key": self.key.__dict__,
            "items": self.items,
            "audio_start": self.audio_start,
            "audio_end": self.audio_end,
            "book_file": self.book_file.__dict__,
            "audio_file": self.audio_file.__dict__,
            "aligner": self.aligner,
            "words": self.words,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Entry":
        if data.get("version") != CACHE_VERSION or data.get("text_extraction") != TEXT_EXTRACTION:
            raise CacheError("cache entry from an incompatible version")
        return cls(
            key=GroupKey(**data["key"]),
            items=[tuple(i) for i in data["items"]],
            audio_start=data["audio_start"],
            audio_end=data["audio_end"],
            book_file=FileFingerprint(**data["book_file"]),
            audio_file=FileFingerprint(**data["audio_file"]),
            aligner=data["aligner"],
            words=[tuple(w) for w in data["words"]],
        )


def build_entry(key: GroupKey, item_texts: list[tuple[int, str]], aligned_words: list[dict],
                audio_start: float, audio_end: float, book_path: Path, audio_path: Path | None,
                aligner: dict, audio_fingerprint: FileFingerprint | None = None) -> Entry:
    """Turn ctc-forced-aligner word output into a cache entry.

    `aligned_words` is `postprocess_results` output for the group text: dicts
    with `text`, `start`, `end` (seconds relative to the audio slice) and
    `score`. Times are shifted by `audio_start` to absolute book time.
    """
    text, items = Entry.group_text(item_texts)
    tokens = list(_WORD.finditer(text))
    if len(tokens) != len(aligned_words):
        raise CacheError(f"aligner returned {len(aligned_words)} words for {len(tokens)} tokens")
    words = []
    for token, word in zip(tokens, aligned_words):
        if word.get("text") != token.group(0):
            raise CacheError(f"word mismatch at char {token.start()}: "
                             f"{word.get('text')!r} vs {token.group(0)!r}")
        words.append((token.start(), token.end(),
                      audio_start + float(word["start"]), audio_start + float(word["end"]),
                      float(word["score"])))
    return Entry(key=key, items=items, audio_start=audio_start, audio_end=audio_end,
                 book_file=FileFingerprint.of(book_path),
                 audio_file=audio_fingerprint or FileFingerprint.of(audio_path),
                 aligner=aligner, words=words)


class AlignmentCache:
    """Directory of gzipped JSON entries. Writes are atomic (temp file + rename)."""

    def __init__(self, root: Path | None = None) -> None:
        # CONCORDANCE_CACHE_DIR overrides the location (the aligner container and the
        # sandbox tests use it); otherwise the cache sits under the state directory.
        state = os.environ.get("CONCORDANCE_STATE_DIR") or Path.home() / ".cache" / "concordance"
        default = Path(os.environ.get("CONCORDANCE_CACHE_DIR") or Path(state) / "alignments")
        self.root = root or default

    def path(self, key: GroupKey) -> Path:
        return self.root / key.filename()

    def save(self, entry: Entry) -> Path:
        target = self.path(entry.key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                gz.write(json.dumps(entry.to_json()).encode("utf-8"))
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target

    def load(self, key: GroupKey) -> Entry | None:
        target = self.path(key)
        if not target.exists():
            return None
        try:
            with gzip.open(target, "rb") as gz:
                return Entry.from_json(json.loads(gz.read().decode("utf-8")))
        except (OSError, ValueError, KeyError, TypeError, CacheError):
            return None      # unreadable or incompatible: treat as a miss and rebuild

    def entries_for(self, calibre_book_id: int) -> list[Entry]:
        folder = self.root / str(calibre_book_id)
        out = []
        for file in sorted(folder.glob("*.json.gz")) if folder.is_dir() else []:
            try:
                with gzip.open(file, "rb") as gz:
                    out.append(Entry.from_json(json.loads(gz.read().decode("utf-8"))))
            except (OSError, ValueError, KeyError, TypeError, CacheError):
                continue
        return out
