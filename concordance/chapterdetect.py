"""Find where the real chapters start in an ebook, including several inside one file.

Three sources, tried in order of reliability:

1. **Table of contents with anchors.** When the EPUB's nav/NCX points *inside*
   files (`chapter.xhtml#ch12`), those targets are the chapter starts.
2. **Numbered headings.** Short heading-like blocks whose number counts upwards
   inside a file ("1", "2", "3"…, "Chapter Seven", "XII"). The run of consecutive
   numbers is what separates a chapter heading from a stray number in the prose.
3. **File starts.** Each content file is one chapter (common in modern EPUBs).

Offsets are canonical character offsets (see xpointer.py), the same coordinates
the alignment cache and KOReader positions use.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .xpointer import BLOCK_TAGS, Document, Node, _first_text_at_or_after, parse_document

MIN_CONTENT_CHARS = 2000
MAX_HEADING_CHARS = 80
MIN_RUN = 3                 # consecutive numbered headings needed to trust a file's numbering
MIN_GAP_CHARS = 40          # starts closer than this are the same place (a file start and its heading)

_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
_PREFIX = re.compile(r"^(?:chapter|chap\.?|ch\.?)\s+", re.I)


@dataclass(frozen=True)
class ChapterStart:
    spine: int
    offset: int
    title: str
    source: str             # "toc" | "heading" | "file"


def _word_number(text: str) -> int | None:
    text = text.lower().replace("‑", "-").replace("‐", "-")
    if text in _WORDS:
        return _WORDS[text]
    if text in _TENS:
        return _TENS[text]
    tens, _, unit = text.partition("-")
    if tens in _TENS and unit in _WORDS and 0 < _WORDS[unit] < 10:
        return _TENS[tens] + _WORDS[unit]
    return None


def _roman(text: str) -> int | None:
    text = text.lower()
    if not text or any(ch not in _ROMAN for ch in text) or len(text) > 8:
        return None
    total = 0
    for i, ch in enumerate(text):
        value = _ROMAN[ch]
        total += -value if i + 1 < len(text) and _ROMAN[text[i + 1]] > value else value
    return total if 0 < total <= 150 else None


def chapter_number(text: str) -> int | None:
    """The chapter number a heading states, or None. "12", "XII", "Chapter Twelve",
    "Chapter 12: The Storm" all give 12; ordinary words and sentences give None."""
    text = " ".join(text.split())
    if not text or len(text) > MAX_HEADING_CHARS:
        return None
    body = _PREFIX.sub("", text)
    has_prefix = body != text
    whole = _word_number(body.split(":")[0].strip())
    if whole is not None and (has_prefix or ":" not in body):
        return whole
    head = re.split(r"[\s:.—–-]+", body, maxsplit=1)
    first = head[0]
    rest = head[1] if len(head) > 1 else ""
    if first.isdigit():
        return int(first) if (not rest or has_prefix or len(first) <= 3) and int(first) < 1000 else None
    word = _word_number(body.split(":")[0].strip()) if not rest or has_prefix else None
    if word is None:
        word = _word_number(first)
        if word is not None and rest and not has_prefix:
            word = None
    if word is not None:
        return word
    roman = _roman(first)
    if roman is not None and (not rest or has_prefix):
        # A lone "I" or "V" is more often a word than a numeral; require a prefix or a run.
        return roman
    return None


def _text_of(node: Node) -> str:
    if node.is_text:
        return node.text
    return "".join(_text_of(child) for child in node.children)


def _has_block_child(node: Node) -> bool:
    return any(not c.is_text and (c.tag in BLOCK_TAGS or _has_block_child(c)) for c in node.children)


def heading_blocks(doc: Document, styled: bool = False) -> list:
    """(canonical offset, text) for short leaf blocks, in document order.

    With `styled`, each entry also carries a style: (tag, class, number format),
    so interleaved sequences (a book's chapters vs. a novel-within-the-novel's
    "CHAPTER 3", or a numbered list) can be told apart.
    """
    out: list = []

    def walk(node: Node) -> None:
        for child in node.children:
            if child.is_text:
                continue
            if child.tag in BLOCK_TAGS and child.tag not in {"body", "br", "hr"} and not _has_block_child(child):
                text = " ".join(_text_of(child).split())
                if text and len(text) <= MAX_HEADING_CHARS:
                    offset = _first_text_at_or_after(doc, child)
                    if styled:
                        out.append((offset, text, (child.tag, child.attrs.get("class", ""), _number_format(text))))
                    else:
                        out.append((offset, text))
            else:
                walk(child)

    walk(doc.body)
    return out


def _number_format(text: str) -> str:
    if _PREFIX.match(text):
        return "prefixed"
    first = text.split()[0] if text.split() else ""
    return "bare" if first == text else "titled"


def _numbered_runs(blocks: list[tuple[int, str]]) -> list[list[tuple[int, str, int]]]:
    """Runs of headings whose numbers go up by exactly one, in document order."""
    runs: list[list[tuple[int, str, int]]] = []
    current: list[tuple[int, str, int]] = []
    for offset, text in blocks:
        number = chapter_number(text)
        if number is None:
            continue
        if current and number == current[-1][2] + 1:
            current.append((offset, text, number))
        elif current and number > current[-1][2] + 1:
            continue        # a stray number (a year, a street number), not a restart
        else:
            if len(current) >= 2:
                runs.append(current)
            current = [(offset, text, number)]
    if len(current) >= 2:
        runs.append(current)
    return runs


def _title(text: str, number: int | None) -> str:
    if number is not None and re.fullmatch(r"[\dIVXLCivxlc]+", text.strip()):
        return f"Chapter {number}"
    if not text.isupper():
        return text
    # Capitalise words without str.title()'s "Impossible’S" after apostrophes.
    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split())


# --- table of contents ------------------------------------------------------------


def _opf(zf: zipfile.ZipFile) -> tuple[str, ET.Element]:
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(".//{*}rootfile")
    path = rootfile.get("full-path") if rootfile is not None else ""
    return path, ET.fromstring(zf.read(path))


def toc_targets(book_file: Path) -> list[tuple[str, str | None, str]]:
    """(zip path of the target file, fragment id or None, label) in TOC order."""
    with zipfile.ZipFile(book_file) as zf:
        opf_path, opf = _opf(zf)
        base = posixpath.dirname(opf_path)
        items = opf.findall(".//{*}manifest/{*}item")
        nav = next((i for i in items if "nav" in (i.get("properties") or "").split()), None)
        spine = opf.find(".//{*}spine")
        ncx_id = spine.get("toc") if spine is not None else None
        ncx = next((i for i in items if i.get("id") == ncx_id), None)
        chosen = nav or ncx
        if chosen is None:
            return []
        toc_path = posixpath.normpath(posixpath.join(base, chosen.get("href") or ""))
        try:
            root = ET.fromstring(zf.read(toc_path))
        except (KeyError, ET.ParseError):
            return []
    toc_dir = posixpath.dirname(toc_path)
    out: list[tuple[str, str | None, str]] = []
    if chosen is nav:
        toc = next((n for n in root.iter() if n.tag.endswith("nav") and
                    "toc" in (n.get("{http://www.idpf.org/2007/ops}type") or n.get("epub:type") or "")), None)
        anchors = (toc if toc is not None else root).iter()
        for a in anchors:
            if a.tag.endswith("}a") or a.tag == "a":
                href = a.get("href") or ""
                label = " ".join("".join(a.itertext()).split())
                out.append((*_split_href(toc_dir, href), label))
    else:
        for point in root.iter():
            if point.tag.endswith("navPoint"):
                content = next((c for c in point if c.tag.endswith("content")), None)
                label_el = next((c for c in point.iter() if c.tag.endswith("text")), None)
                if content is not None:
                    label = " ".join((label_el.text or "").split()) if label_el is not None else ""
                    out.append((*_split_href(toc_dir, content.get("src") or ""), label))
    return out


def _split_href(toc_dir: str, href: str) -> tuple[str, str | None]:
    path, _, fragment = unquote(href).partition("#")
    return posixpath.normpath(posixpath.join(toc_dir, path)), fragment or None


def spine_paths(book_file: Path) -> dict[str, int]:
    """Zip path -> 1-based spine index."""
    with zipfile.ZipFile(book_file) as zf:
        opf_path, opf = _opf(zf)
    base = posixpath.dirname(opf_path)
    manifest = {i.get("id"): i.get("href") for i in opf.findall(".//{*}manifest/{*}item")}
    out = {}
    for index, ref in enumerate(opf.findall(".//{*}spine/{*}itemref"), start=1):
        href = manifest.get(ref.get("idref"))
        if href:
            out[posixpath.normpath(posixpath.join(base, unquote(href)))] = index
    return out


def _element_with_id(node: Node, target: str) -> Node | None:
    if not node.is_text and node.attrs.get("id") == target:
        return node
    for child in node.children:
        found = _element_with_id(child, target)
        if found is not None:
            return found
    return None


# --- detection ----------------------------------------------------------------------


def detect(book_file: Path, docs: dict[int, Document]) -> list[ChapterStart]:
    """Chapter starts for the whole book, using the most reliable source available."""
    content = [i for i in sorted(docs) if len(docs[i].text) >= MIN_CONTENT_CHARS]
    from_toc = _from_toc(book_file, docs, set(content))
    inside_files = sum(1 for s in from_toc if s.offset > 0)
    if from_toc and inside_files >= 2:
        return _dedupe(from_toc)

    # Numbered headings: find runs per heading style, then keep the style with the
    # most headings in runs across the book (the real chapters), ignoring others.
    styled = {index: heading_blocks(docs[index], styled=True) for index in content}
    by_style: dict[tuple, dict[int, list]] = {}
    for index, blocks in styled.items():
        for offset, text, style in blocks:
            if chapter_number(text) is not None:
                by_style.setdefault(style, {}).setdefault(index, []).append((offset, text))
    best_style, best_runs, best_total = None, {}, 0
    for style, per_file in by_style.items():
        runs = {i: [r for r in _numbered_runs(blocks) if len(r) >= MIN_RUN] for i, blocks in per_file.items()}
        total = sum(len(r) for rs in runs.values() for r in rs)
        if total > best_total:
            best_style, best_runs, best_total = style, runs, total

    starts: list[ChapterStart] = []
    numbered_files = 0
    for index in content:
        runs = best_runs.get(index, [])
        if runs:
            numbered_files += 1
            for run in runs:
                starts.extend(ChapterStart(index, off, _title(text, n), "heading") for off, text, n in run)
            first_offset, _, first_number = runs[0][0]
            if first_offset > MIN_GAP_CHARS:
                # The file opens with a chapter in a different heading style (often the
                # first of a part); it starts at the top of the file.
                title = f"Chapter {first_number - 1}" if first_number > 1 else _file_start(index, docs[index]).title
                starts.append(ChapterStart(index, 0, title, "heading"))
    if numbered_files:
        covered = {s.spine for s in starts}
        starts.extend(_file_start(index, docs[index]) for index in content if index not in covered)
        return _label_parts(_dedupe(starts), _part_names(docs, content))
    return _dedupe([_file_start(index, docs[index]) for index in content])


_PART = re.compile(r"^part\s+(\S+)", re.I)


def _part_names(docs: dict[int, Document], content: list[int]) -> dict[int, str]:
    """Part label in force for each content item, from short "PART ONE" divider files.

    A run of non-content files with no part heading (back matter, an excerpt from
    another book) ends the current part, so appendices aren't filed under it.
    """
    names, current, index_set = {}, None, set(content)
    for index in sorted(docs):
        if index in index_set:
            if current:
                names[index] = current
            continue
        blocks = heading_blocks(docs[index])
        heading = next((t for _, t in blocks[:3] if _PART.match(t)), None)
        if heading is None:
            words = docs[index].text.split()
            if len(words) >= 2 and words[0].lower() == "part":
                heading = " ".join(words[:2])
        if heading:
            number = _PART.match(heading).group(1)
            current = f"Part {number.title() if not number.isdigit() else number}"
        elif docs[index].text.strip():
            current = None
    return names


def _label_parts(starts: list[ChapterStart], parts: dict[int, str]) -> list[ChapterStart]:
    """When chapter titles repeat (numbering restarts each part), prefix the part name."""
    titles = [s.title for s in starts if s.source == "heading"]
    if len(titles) == len(set(titles)):
        return starts
    out, counter, previous = [], 1, None
    for start in starts:
        if start.source != "heading":
            out.append(start)
            continue
        label = parts.get(start.spine)
        if label is None:
            number = chapter_number(start.title)
            if previous is not None and number is not None and number <= previous:
                counter += 1
            previous = number if number is not None else previous
            label = f"Part {counter}"
        out.append(ChapterStart(start.spine, start.offset, f"{label}, {start.title}", start.source))
    return out


def _file_start(index: int, doc: Document) -> ChapterStart:
    """A chapter at the start of a file, titled by its opening heading if it has one."""
    blocks = heading_blocks(doc)
    if blocks and blocks[0][0] < MIN_GAP_CHARS and len(blocks[0][1]) <= 60:
        first = blocks[0][1]
        # Titles run together with the opening words ("TWO WOODY POTTER’S PHONE") keep just the number.
        number = chapter_number(first.split()[0]) if first.split() else None
        if number is not None and chapter_number(first) is None:
            return ChapterStart(index, 0, f"Chapter {number}", "file")
        return ChapterStart(index, 0, _title(first, chapter_number(first)), "file")
    return ChapterStart(index, 0, "", "file")


def _from_toc(book_file: Path, docs: dict[int, Document], content: set[int]) -> list[ChapterStart]:
    try:
        targets = toc_targets(book_file)
        paths = spine_paths(book_file)
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        return []
    out = []
    for path, fragment, label in targets:
        index = paths.get(path)
        if index is None or index not in content:
            continue
        offset = 0
        if fragment:
            element = _element_with_id(docs[index].body, fragment)
            if element is None:
                continue
            offset = _first_text_at_or_after(docs[index], element)
        out.append(ChapterStart(index, offset, label or f"Chapter {len(out) + 1}", "toc"))
    return out


def _dedupe(starts: list[ChapterStart]) -> list[ChapterStart]:
    out: list[ChapterStart] = []
    for start in sorted(starts, key=lambda s: (s.spine, s.offset, s.source != "heading")):
        if out and out[-1].spine == start.spine and start.offset - out[-1].offset < MIN_GAP_CHARS:
            continue
        out.append(start)
    # Untitled chapters get their position in the book.
    return [s if s.title else ChapterStart(s.spine, s.offset, f"Chapter {i}", s.source)
            for i, s in enumerate(out, start=1)]


def load_documents(book_file: Path) -> dict[int, Document]:
    from .xpointer import spine_documents
    return {index: parse_document(raw) for index, raw in spine_documents(book_file).items()}


def book_positions(docs: dict[int, Document]) -> dict[int, int]:
    """Whole-book character position of each content item's start (content items only)."""
    positions, cursor = {}, 0
    for index in sorted(docs):
        if len(docs[index].text) >= MIN_CONTENT_CHARS:
            positions[index] = cursor
            cursor += len(docs[index].text) + 1
    positions[-1] = cursor          # total
    return positions
