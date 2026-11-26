"""Name ABS chapters after the ebook chapters that start where they do.

Many releases name their chapters after their files ("001" … "041", "<book
title> - 001", "Chapter 001  - 00:43:43"). The numbers are file numbers, not
chapter numbers: a release's "001" can hold credits and front matter, and
its "Chapter 2" can be the book's Chapter 1, so relabelling by number would shift
every name after it. A name
here only ever comes from an ebook chapter whose opening was located at that
ABS chapter's start. Renaming never moves a boundary.

Everything in this module is pure; `chapters.propose` does the locating.
"""

from __future__ import annotations

import re

from .absclient import Chapter
from .chapterdetect import chapter_number

# A located chapter opening names an ABS chapter if it lies this close to its start.
# The lead-in absorbs what a narrator says before the heading: a book title, a
# credits announcement, "Chapter One" read after a pause.
MATCH_BEFORE_SECONDS = 20.0
MATCH_LEAD_IN_SECONDS = 120.0
# Probe windows reach a little past the tolerance on each side, because the probe
# rejects a match pinned to either edge of the audio it was given.
PROBE_MARGIN_SECONDS = 10.0
# An unmatched chapter before the first match or after the last is called credits
# only if it is short. A long one is something the matcher missed, not credits.
CREDITS_MAX_SECONDS = 180.0
# A probe that missed the -0.5 confirmation bar can still name a chapter when the
# book leaves it no other choice: it is the only ABS chapter between two matched
# ones, with exactly one ebook chapter left between those. Measured on a real book:
# such near-misses scored -0.53 to -0.59 (and -1.23 once), while an opening tried
# against the wrong audio scored -2.23. Never kept as an anchor for later runs.
NEAR_MISS_SCORE = -1.5
OPENING_CREDITS = "Opening Credits"
END_CREDITS = "End Credits"

_TIME = re.compile(r"\d+:\d{2}(?::\d{2})?(?:\.\d+)?")
_SEPARATOR = re.compile(r"\s*[•·—–:|]\s*|\.\s+|\s+-\s+")
_NUMBER_HEAD = re.compile(r"(?:(?:chapter|chap\.?|ch\.?)\s+)?[\w-]+(?:\s+[\w-]+)?", re.I)
NAMED_HEADS = {"prologue", "epilogue", "interlude", "introduction", "preface", "foreword",
                "afterword", "coda", "postscript"}


def _shape(title: str) -> str:
    return re.sub(r"\d+", "#", _TIME.sub("T", title)).strip()


def has_subtitles(ebook_titles: list[str | None]) -> bool:
    """True when most of the ebook's titled chapters say more than "Chapter N"."""
    titled = [t for t in ebook_titles if t]
    rich = [t for t in titled if not re.fullmatch(r"Chapter \d+", t)]
    return bool(titled) and len(rich) * 2 > len(titled)


def count_differs(titles: list[str], ebook_titles: list[str | None]) -> bool:
    """True when ABS and the ebook disagree on how many titled chapters there are."""
    titled = [t for t in ebook_titles if t]
    return bool(titled) and len(titled) != len(titles)


def is_generic(titles: list[str], ebook_titles: list[str | None] | None = None) -> bool:
    """True when every ABS title is the same text apart from numbers and timestamps.

    "Chapter 1" … "Chapter N" counts only when the ebook has better names to
    offer (`has_subtitles`), or when the two lists can't be numbered the same
    (`count_differs`): credits split into their own files push every ABS number
    past the book's. A plain list against an ebook's own plain numbers, chapter
    for chapter, is left alone. Empty titles count as generic.
    """
    if len(titles) < 2:
        return False
    shapes = {_shape(t) for t in titles}
    if len(shapes) != 1:
        return False
    shape = shapes.pop()
    if shape == "":
        return True
    if shape.lower() == "chapter #":
        return ebook_titles is not None and (has_subtitles(ebook_titles)
                                             or count_differs(titles, ebook_titles))
    return "#" in shape


def _case(text: str) -> str:
    if not text.isupper():
        return text
    # Capitalise words without str.title()'s "Impossible’S" after apostrophes.
    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split())


def _number(head: str) -> int | None:
    return chapter_number(head) if _NUMBER_HEAD.fullmatch(head) else None


def display_title(text: str) -> str:
    """An ebook chapter title in the house style: "Chapter 1: The Subtitle".

    Only a number or a named section (Prologue, Epilogue…) is treated as a head to
    split a subtitle from; any other title is kept as written, so "Mr. Brown" or
    "Part I: The Long Road" are never rearranged.
    """
    text = " ".join(text.split())
    if not text:
        return text
    parts = _SEPARATOR.split(text, maxsplit=1)
    head = parts[0].strip(" .")
    sub = parts[1].strip() if len(parts) > 1 else ""
    number = _number(head)
    if sub and number is None and head.lower() not in NAMED_HEADS:
        return _case(text)
    head = f"Chapter {number}" if number is not None else _case(head)
    return f"{head}: {_case(sub)}" if sub else head


def match_starts(chapters: list[Chapter], known: dict[int, float],
                 eligible: set[int] | None = None) -> dict[int, int]:
    """ABS chapter index -> ebook start index, for openings located at a chapter's start.

    `known` is start index -> located time. Matches must be strictly increasing in
    the ebook, the same guard the split feature relies on: a later ABS chapter
    can't open with an earlier ebook chapter.
    """
    out: dict[int, int] = {}
    last = -1
    located = sorted(known.items())
    for i, c in enumerate(chapters):
        if eligible is not None and i not in eligible:
            continue
        hi = min(c.start + MATCH_LEAD_IN_SECONDS, c.end)
        for k, t in located:
            if k > last and c.start - MATCH_BEFORE_SECONDS <= t <= hi:
                out[i] = k
                last = k
                break
    return out


def probe_window(chapter: Chapter, total: float) -> tuple[float, float]:
    lo = max(0.0, chapter.start - MATCH_BEFORE_SECONDS - PROBE_MARGIN_SECONDS)
    hi = min(total, chapter.end + PROBE_MARGIN_SECONDS,
             chapter.start + MATCH_LEAD_IN_SECONDS + PROBE_MARGIN_SECONDS)
    return lo, hi


def candidates(chapters: list[Chapter], eligible: list[int], estimates: list[float],
               titled: set[int], matched: dict[int, int],
               tried: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """(ABS chapter, ebook start) pairs to probe next: for each unmatched chapter, the
    untried titled start whose estimated time is nearest its start, between the
    starts already matched on either side."""
    out: list[tuple[int, int]] = []
    for i in eligible:
        if i in matched:
            continue
        lo = max((k for j, k in matched.items() if j < i), default=-1)
        hi = min((k for j, k in matched.items() if j > i), default=len(estimates))
        options = [k for k in range(lo + 1, hi) if k in titled and (i, k) not in tried]
        if options:
            out.append((i, min(options, key=lambda k: abs(estimates[k] - chapters[i].start))))
    return out


def renamed_titles(chapters: list[Chapter], matched: dict[int, str]) -> dict[int, str]:
    """ABS chapter index -> new title, only where the title changes.

    `matched` is chapter index -> display title of the ebook chapter found there.
    With no match at all nothing is renamed: credits are only recognisable
    relative to the book's first and last located chapters.
    """
    if not matched:
        return {}
    first, last = min(matched), max(matched)
    out = dict(matched)
    for i, c in enumerate(chapters):
        if i in out or c.end - c.start >= CREDITS_MAX_SECONDS:
            continue
        if i < first:
            out[i] = OPENING_CREDITS
        elif i > last:
            out[i] = END_CREDITS
    return {i: t for i, t in out.items() if t and t != chapters[i].title}


def fill_gaps(chapters: list[Chapter], eligible: set[int], matched: dict[int, int],
              titles: list[str | None], attempts: dict[tuple[int, int], tuple[float, float]]) -> dict[int, str]:
    """Names for unmatched chapters that their matched neighbours leave no doubt about.

    `attempts` is (ABS chapter, ebook start) -> (score, landed seconds) for every
    probe that ran. Only a single unmatched ABS chapter between two matched ones is
    considered:

    - one titled ebook chapter between the neighbours, and the probe for exactly
      that pair scored NEAR_MISS_SCORE or better and landed within the match
      tolerance: it is that chapter;
    - no ebook start at all between them, titled or not: no chapter can open
      inside it, so it is the rest of the previous chapter (a release split one
      chapter across two files). No probe is needed or consulted: the two
      confirmed neighbours are the evidence, and probe scores are not kept between
      runs, so requiring one left such a chapter unnamed on a re-propose.
    """
    out: dict[int, str] = {}
    order = sorted(matched.items())
    for (ia, ka), (ib, kb) in zip(order, order[1:]):
        if ib - ia != 2 or ia + 1 not in eligible:
            continue
        i = ia + 1
        c = chapters[i]
        between = [k for k in range(ka + 1, kb) if titles[k]]
        tried = {k: v for (j, k), v in attempts.items() if j == i}
        if len(between) == 1:
            k = between[0]
            if k in tried:
                score, landed = tried[k]
                if score >= NEAR_MISS_SCORE and \
                        c.start - MATCH_BEFORE_SECONDS <= landed <= min(c.start + MATCH_LEAD_IN_SECONDS, c.end):
                    out[i] = titles[k]
        elif kb == ka + 1 and titles[ka]:
            out[i] = f"{titles[ka]} (continued)"
    return out


def merge_repeats(chapters: list[dict]) -> list[dict]:
    """Join runs of adjacent chapters that ended up with the same title.

    Renaming can give several files one name, typically opening credits split
    into two or three short files. Showing "Opening Credits" three times in a row
    says nothing the first one doesn't, so the run becomes one chapter from the
    first start to the last end. Only boundaries are removed, never moved.
    """
    out: list[dict] = []
    for c in chapters:
        if out and out[-1]["title"] == c["title"]:
            out[-1] = {**out[-1], "end": c["end"]}
        else:
            out.append(dict(c))
    return out
