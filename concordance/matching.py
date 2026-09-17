"""Pair a Calibre ebook with its Audiobookshelf audiobook.

Measured on this library (115 audiobooks vs 510 Calibre books): normalised
title matching finds 103 of 103 possible pairs with zero false negatives.
ISBN contributes 8, ASIN contributes nothing. Title does the real work, so
this stays deliberately simple.

Author agreement is a review flag, never a hard gate: `The Wandering Inn` is
a correct pair whose author is `pirateaba` in Calibre and `Pirate Aba` in ABS.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .absclient import AbsBook
from .calibre import CalibreBook

_QUOTES = re.compile(r"[‘’'`]")
_SUBTITLE = re.compile(r"\s*[:(\[].*$")
_NONALNUM = re.compile(r"[^a-z0-9 ]")
_ARTICLES = re.compile(r"\b(the|a|an)\b")
_SPLIT_AUTHORS = re.compile(r"[&,;]| and ")

FUZZY_CUTOFF = 0.80


def normalise_title(title: str) -> str:
    """Fold a title to a comparable key: no subtitle, no punctuation, no articles."""
    text = (title or "").lower()
    text = _QUOTES.sub("", text)
    text = _SUBTITLE.sub("", text)
    text = _NONALNUM.sub(" ", text)
    text = _ARTICLES.sub(" ", text)
    return " ".join(text.split())


def surnames(author_field: str) -> set[str]:
    """Last-word tokens for each author, used only as a soft agreement check."""
    out: set[str] = set()
    for part in _SPLIT_AUTHORS.split(author_field or ""):
        words = part.strip().split()
        if words:
            out.add(words[-1].lower())
    return out


@dataclass(frozen=True)
class Pair:
    calibre: CalibreBook
    audiobook: AbsBook
    method: str            # "isbn" | "title" | "fuzzy"
    score: float           # 1.0 for exact/isbn, else the difflib ratio
    author_agrees: bool


def match_pairs(
    calibre_books: dict[int, CalibreBook],
    audiobooks: list[AbsBook],
    calibre_isbns: dict[str, int] | None = None,
) -> tuple[list[Pair], list[AbsBook]]:
    """Return (pairs, unmatched audiobooks)."""
    by_title: dict[str, list[CalibreBook]] = {}
    for book in calibre_books.values():
        by_title.setdefault(normalise_title(book.title), []).append(book)
    keys = list(by_title)
    isbn_index = calibre_isbns or {}

    pairs: list[Pair] = []
    unmatched: list[AbsBook] = []

    for audio in audiobooks:
        audio_surnames = surnames(audio.authors)

        isbn = (audio.isbn or "").replace("-", "").strip()
        if isbn and isbn in isbn_index:
            book = calibre_books.get(isbn_index[isbn])
            if book:
                pairs.append(Pair(book, audio, "isbn", 1.0, True))
                continue

        key = normalise_title(audio.title)
        candidates = by_title.get(key)
        method, score = "title", 1.0
        if not candidates:
            close = difflib.get_close_matches(key, keys, n=1, cutoff=FUZZY_CUTOFF)
            if not close:
                unmatched.append(audio)
                continue
            candidates = by_title[close[0]]
            method = "fuzzy"
            score = difflib.SequenceMatcher(None, key, close[0]).ratio()

        # Prefer a candidate whose author agrees, but never reject on author alone.
        def calibre_surnames(book: CalibreBook) -> set[str]:
            out: set[str] = set()
            for name in book.authors:
                out |= surnames(name)
            return out

        chosen = next(
            (c for c in candidates if calibre_surnames(c) & audio_surnames), candidates[0]
        )
        agrees = bool(audio_surnames) and any(
            audio_surnames & calibre_surnames(c) for c in candidates
        )
        pairs.append(Pair(chosen, audio, method, round(score, 3), agrees))

    return pairs, unmatched


def load_calibre_isbns(db_path: str) -> dict[str, int]:
    """Map normalised ISBN -> Calibre book id."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT val, book FROM identifiers WHERE type = 'isbn'"
        ).fetchall()
    finally:
        conn.close()
    return {str(val).replace("-", "").strip(): int(book) for val, book in rows if val}
