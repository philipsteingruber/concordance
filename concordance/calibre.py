"""Read book locations from Calibre's metadata.db and extract EPUB spine structure.

Read-only throughout. metadata.db is in WAL mode and is written by a live CWA
process, so we open it with `mode=ro` and never hold a transaction open.
"""

from __future__ import annotations

import html
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

_TAG = re.compile(r"(?s)<[^>]+>")
_SCRIPT = re.compile(r"(?s)<(script|style).*?</\1>")
_WS = re.compile(r"\s+")

# Spine items below this length are structural (cover, title page, blank
# separators). They have no audio counterpart and must be excluded before
# chapters are aligned by ordinal position.
MIN_CONTENT_CHARS = 1500

# Roles a narrator never reads aloud. Deliberately excludes the "sometimes
# narrated" set (dedication, epigraph, foreword, preface, acknowledgements,
# afterword): those do appear in recordings, and dropping a narrated item is
# what made the 2026-09-16 classifier experiment produce false positives.
# A table of contents is the important one here. It clears MIN_CONTENT_CHARS
# comfortably (Jade City's is 2,091 chars of chapter titles), so length alone
# cannot tell it apart from a short chapter, and aligning it against the
# opening minutes of the audio wrecks that group's score.
NEVER_NARRATED_TYPES = frozenset({
    "cover", "titlepage", "title-page", "toc", "copyright-page", "copyright",
    "colophon", "landmarks", "loi", "lot", "index", "backmatter-toc",
})
_EPUB_TYPE = re.compile(rb'epub:type\s*=\s*["\']([^"\']+)["\']')
_HEADING = re.compile(rb"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>")
# Headings that name a structural page. Kept deliberately small: every entry
# has to be a title no novel would give a real chapter. Publishers mislabel the
# machine-readable role (Jade City's copyright page declares
# `epub:type="bodymatter chapter"`), so this is the only signal left for it.
NEVER_NARRATED_HEADINGS = frozenset({
    "copyright", "contents", "table of contents", "cover", "title page",
    "also by this author", "about the publisher",
})


def _never_narrated(doc: bytes) -> bool:
    """Whether the document declares a role no narrator reads.

    Only the first few declarations are considered: `epub:type` also appears on
    inline elements deep in real chapters (`noteref`, `pagebreak`), and a
    chapter that happens to cite a footnote must not be mistaken for structure.
    """
    tokens: set[str] = set()
    for match in _EPUB_TYPE.findall(doc[:4096]):
        tokens.update(match.decode("utf-8", "replace").lower().split())
    if tokens & NEVER_NARRATED_TYPES:
        return True
    heading = _HEADING.search(doc)
    if heading is None:
        return False
    text = _WS.sub(" ", html.unescape(_TAG.sub("", heading.group(1).decode("utf-8", "replace"))))
    return text.strip().lower() in NEVER_NARRATED_HEADINGS


@dataclass(frozen=True)
class SpineItem:
    index: int          # 1-based position in the spine, matching DocFragment[N]
    href: str
    chars: int
    narrated: bool = True   # False only when the EPUB says the role is never read aloud

    @property
    def is_content(self) -> bool:
        return self.chars >= MIN_CONTENT_CHARS


@dataclass(frozen=True)
class CalibreBook:
    book_id: int
    title: str
    path: Path
    authors: tuple[str, ...] = ()


def load_books(db_path: str, root: str) -> dict[int, CalibreBook]:
    """Map Calibre book id -> book, for every book in the library."""
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot open Calibre metadata.db at {db_path}: {exc}") from exc
    try:
        rows = conn.execute("SELECT id, title, path FROM books").fetchall()
        author_rows = conn.execute(
            "SELECT l.book, a.name FROM authors a JOIN books_authors_link l ON l.author = a.id"
        ).fetchall()
    finally:
        conn.close()

    authors: dict[int, list[str]] = {}
    for book_id, name in author_rows:
        authors.setdefault(int(book_id), []).append(name or "")

    return {
        int(bid): CalibreBook(
            book_id=int(bid),
            title=title or "",
            path=Path(root) / rel,
            authors=tuple(authors.get(int(bid), ())),
        )
        for bid, title, rel in rows
    }


def spine_file(book: CalibreBook, fmt: str | None) -> tuple[str, Path] | None:
    """The (format, file) whose spine positions should be used for this book.

    `fmt` is the format the Kobo has open (from its XPointer); without one,
    KEPUB is assumed (the OPDS download KOReader uses), then EPUB. Spine indices
    are NOT shared between the two: kepubify inserts a title-page dummy at
    spine 1 in some KEPUBs, so an EPUB index is one short of the KEPUB's.
    """
    if not book.path.is_dir():
        return None
    order = [fmt] if fmt else []
    order += [f for f in ("kepub", "epub") if f not in order]
    for ext in order:
        found = sorted(book.path.glob(f"*.{ext}"))
        if found:
            return ext, found[0]
    return None


def find_epub(book: CalibreBook) -> Path | None:
    """Locate the book's EPUB.

    Prefers `.epub` over `.kepub`. Don't use its spine indices for positions the
    Kobo reports: they can differ from the KEPUB's (see `spine_file`).
    """
    if not book.path.is_dir():
        return None
    epubs = sorted(book.path.glob("*.epub"))
    if epubs:
        return epubs[0]
    kepubs = sorted(book.path.glob("*.kepub"))
    return kepubs[0] if kepubs else None


def _visible_chars(raw: bytes) -> int:
    text = raw.decode("utf-8", errors="ignore")
    text = _SCRIPT.sub(" ", text)
    text = html.unescape(_TAG.sub(" ", text))
    return len(_WS.sub(" ", text).strip())


def read_spine(epub_path: Path) -> list[SpineItem]:
    """Return the EPUB spine in reading order, with the visible text length of each item.

    The index is 1-based so it lines up directly with KOReader's `DocFragment[N]`.
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                raise RuntimeError("container.xml has no rootfile")
            opf_path = rootfile.get("full-path") or ""
            opf = ET.fromstring(zf.read(opf_path))
            base = "/".join(opf_path.split("/")[:-1])

            manifest = {
                item.get("id"): item.get("href")
                for item in opf.findall(".//{*}manifest/{*}item")
            }
            # The EPUB 3 navigation document is structure, never narration, and
            # it is the one item that reliably carries a machine-readable role.
            nav_ids = {
                item.get("id")
                for item in opf.findall(".//{*}manifest/{*}item")
                if "nav" in (item.get("properties") or "").split()
            }
            # EPUB 2 books have no epub:type; the OPF guide is the equivalent.
            guide_hrefs = {
                (ref.get("href") or "").split("#")[0]
                for ref in opf.findall(".//{*}guide/{*}reference")
                if (ref.get("type") or "").lower() in NEVER_NARRATED_TYPES
            }
            items: list[SpineItem] = []
            for i, ref in enumerate(opf.findall(".//{*}spine/{*}itemref"), start=1):
                href = manifest.get(ref.get("idref"))
                if not href:
                    continue
                name = f"{base}/{href}" if base else href
                try:
                    doc = zf.read(name)
                except KeyError:
                    doc, chars = b"", 0
                else:
                    chars = _visible_chars(doc)
                narrated = not (
                    ref.get("linear") == "no"
                    or ref.get("idref") in nav_ids
                    or href.split("#")[0] in guide_hrefs
                    or _never_narrated(doc)
                )
                items.append(SpineItem(index=i, href=href, chars=chars, narrated=narrated))
            return items
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as exc:
        raise RuntimeError(f"cannot read EPUB {epub_path.name}: {exc}") from exc
