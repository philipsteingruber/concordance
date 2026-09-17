"""KOReader XPointers <-> character offsets in a spine item's canonical text.

A KOSync position looks like `/body/DocFragment[28]/body/div/p[14]/text().452`.
`DocFragment[N]` is the Nth EPUB spine item; the rest is a path inside that
XHTML file ending in a text node and a character offset. Anchoring only used N,
which places every position at the start of its chapter. This module resolves
the whole path, so a position becomes an exact offset within the chapter.

The path rules follow crengine's `ldomXPointer::toStringV2`
(koreader/crengine, `crengine/src/lvtinydom.cpp`):

* element steps count preceding siblings with the same tag name, 1-based. The
  `[1]` is omitted when the element has no same-name siblings, except that DOM
  versions >= 20260812 always write it. Both forms are accepted here.
* `text()[k]` counts text-node children, 1-based, with the same omission rule.
* `.N` is a character offset inside the addressed text node. A path may also
  end on an element (optionally with `.0`), meaning the start of that element.

KEPUB files carry Kobo's `koboSpan` wrapper markup, so the same reading
position has a different path in the KEPUB than in the EPUB. Resolve against
the file the reader actually opened; `resolve_any` tries both and reports
which one the path fits.

**Canonical text** is the single text representation that offsets refer to,
and it must be what the aligner is fed. It is the body's text nodes in document
order, with a space inserted at block-element boundaries and every whitespace
run collapsed to one space. Because it is built from text content only, an EPUB
and its KEPUB produce the same canonical text, so offsets are comparable across
formats.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

_STEP = re.compile(r"^([A-Za-z_][\w.-]*|text\(\))(?:\[(\d+)\])?$")
_FRAGMENT = re.compile(r"^/body/DocFragment\[(\d+)\](/.*)?$")
_OFFSET = re.compile(r"^(.*?)\.(\d+)$")

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "head", "title"}
BLOCK_TAGS = {"address", "article", "aside", "blockquote", "body", "br", "dd", "div",
              "dl", "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
              "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
              "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul"}


class XPointerError(ValueError):
    """Raised when a string is not a KOReader XPointer or doesn't fit the document."""


# --- DOM ---------------------------------------------------------------------


@dataclass
class Node:
    tag: str | None                      # None for text nodes
    parent: "Node | None" = None
    children: list["Node"] = field(default_factory=list)
    text: str = ""                       # text nodes only
    attrs: dict[str, str] = field(default_factory=dict)   # elements only
    # Filled in by build_canonical for text nodes: canonical index of each raw char.
    char_map: list[int] = field(default_factory=list)
    canon_start: int = 0                 # canonical offset at this node's start

    @property
    def is_text(self) -> bool:
        return self.tag is None


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag.lower(), parent=self.stack[-1], attrs={k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag.lower(), parent=self.stack[-1], attrs={k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        parent = self.stack[-1]
        # Merge adjacent character data (the parser may split it around entities).
        if parent.children and parent.children[-1].is_text:
            parent.children[-1].text += data
        else:
            parent.children.append(Node(None, parent=parent, text=data))


@dataclass
class Document:
    """One parsed spine item plus its canonical text."""

    body: Node
    text: str                            # canonical text
    text_nodes: list[Node]               # in document order


def parse_document(xhtml: bytes | str) -> Document:
    source = xhtml.decode("utf-8", errors="replace") if isinstance(xhtml, bytes) else xhtml
    builder = _TreeBuilder()
    builder.feed(source)
    builder.close()
    body = _find(builder.root, "body") or builder.root
    return _build_canonical(body)


def _find(node: Node, tag: str) -> Node | None:
    if node.tag == tag:
        return node
    for child in node.children:
        found = _find(child, tag)
        if found:
            return found
    return None


def _build_canonical(body: Node) -> Document:
    out: list[str] = []
    text_nodes: list[Node] = []
    pending_space = False

    def emit_space() -> None:
        nonlocal pending_space
        if out:
            pending_space = True

    def walk(node: Node) -> None:
        nonlocal pending_space
        if node.is_text:
            node.canon_start = len(out) + (1 if pending_space and not node.text[:1].isspace() else 0)
            mapping = []
            for ch in node.text:
                if ch.isspace():
                    if out:
                        pending_space = True
                    mapping.append(len(out))
                else:
                    if pending_space:
                        out.append(" ")
                        pending_space = False
                    mapping.append(len(out))
                    out.append(ch)
            node.char_map = mapping
            node.canon_start = mapping[0] if mapping else len(out)
            text_nodes.append(node)
            return
        if node.tag in SKIP_TAGS:
            return
        block = node.tag in BLOCK_TAGS
        if block:
            emit_space()
        for child in node.children:
            walk(child)
        if block:
            emit_space()

    walk(body)
    return Document(body=body, text="".join(out), text_nodes=text_nodes)


# --- XPointer parsing and resolution -------------------------------------------


@dataclass(frozen=True)
class ParsedXPointer:
    fragment: int                        # 1-based spine index
    steps: tuple[tuple[str, int | None], ...]   # (name or "text()", index or None)
    offset: int | None


def parse_xpointer(value: str) -> ParsedXPointer:
    value = (value or "").strip()
    match = _FRAGMENT.match(value)
    if not match:
        raise XPointerError(f"not a DocFragment XPointer: {value!r}")
    fragment = int(match.group(1))
    rest = match.group(2) or ""
    offset = None
    off = _OFFSET.match(rest)
    if off:
        rest, offset = off.group(1), int(off.group(2))
    parts = [p for p in rest.split("/") if p]
    if parts and parts[0] == "body":
        parts = parts[1:]
    steps = []
    for part in parts:
        step = _STEP.match(part)
        if not step:
            raise XPointerError(f"unparseable step {part!r} in {value!r}")
        steps.append((step.group(1).lower(), int(step.group(2)) if step.group(2) else None))
    return ParsedXPointer(fragment=fragment, steps=tuple(steps), offset=offset)


def _is_counted_text(node: Node, skip_whitespace: bool) -> bool:
    return node.is_text and not (skip_whitespace and not node.text.strip())


def _first_text_at_or_after(doc: Document, element: Node) -> int:
    """Canonical offset of the start of `element` (first text inside or after it)."""
    for text_node in doc.text_nodes:
        n: Node | None = text_node
        while n is not None and n is not element:
            n = n.parent
        if n is element:
            return text_node.canon_start
    # No text inside: use the first text node that follows it in document order.
    order = _document_order(doc.body)
    try:
        start = order.index(element)
    except ValueError:
        return len(doc.text)
    for node in order[start + 1:]:
        if node.is_text:
            return node.canon_start
    return len(doc.text)


def _document_order(node: Node) -> list[Node]:
    out = [node]
    for child in node.children:
        out.extend(_document_order(child))
    return out


def resolve(doc: Document, xp: ParsedXPointer, skip_whitespace_text: bool = True) -> int:
    """Canonical character offset for an XPointer's path inside `doc`.

    Raises XPointerError if the path doesn't exist in this document.
    """
    node = doc.body
    for position, (name, index) in enumerate(xp.steps):
        wanted = index or 1
        if name == "text()":
            candidates = [c for c in node.children if _is_counted_text(c, skip_whitespace_text)]
        else:
            candidates = [c for c in node.children if c.tag == name]
        if len(candidates) < wanted:
            raise XPointerError(
                f"step {position + 1} {name}[{wanted}] not found "
                f"({len(candidates)} candidates)"
            )
        node = candidates[wanted - 1]

    if node.is_text:
        k = min(max(xp.offset or 0, 0), max(len(node.text) - 1, 0))
        if not node.char_map:
            return node.canon_start
        return node.char_map[k] if xp.offset is None or xp.offset < len(node.char_map) \
            else node.char_map[-1] + 1
    return _first_text_at_or_after(doc, node)


def to_xpointer(doc: Document, fragment: int, canonical_offset: int,
                skip_whitespace_text: bool = True) -> str:
    """Build a KOReader XPointer for a canonical offset in this spine item.

    Uses V2 index rules: `[k]` only when the parent has more than one candidate,
    which newer KOReader builds also accept.
    """
    target = None
    for node in doc.text_nodes:
        if not node.char_map or (skip_whitespace_text and not node.text.strip()):
            continue
        if node.char_map[0] <= canonical_offset <= node.char_map[-1]:
            target = node
            break
        if node.char_map[0] > canonical_offset:
            target = node          # offset fell in a gap: use the next text node's start
            canonical_offset = node.char_map[0]
            break
    if target is None:
        counted = [n for n in doc.text_nodes if n.char_map and n.text.strip()]
        if not counted:
            return f"/body/DocFragment[{fragment}]/body"
        target = counted[-1]
        canonical_offset = target.char_map[-1]

    raw_offset = next(i for i, c in enumerate(target.char_map) if c >= canonical_offset)

    parts = []
    node = target
    while node is not None and node is not doc.body:
        parent = node.parent
        if node.is_text:
            siblings = [c for c in parent.children if _is_counted_text(c, skip_whitespace_text)]
            name = "text()"
        else:
            siblings = [c for c in parent.children if c.tag == node.tag]
            name = node.tag
        index = next(i for i, c in enumerate(siblings, start=1) if c is node)
        parts.append(f"{name}[{index}]" if len(siblings) > 1 else name)
        node = parent
    path = "/".join(reversed(parts))
    return f"/body/DocFragment[{fragment}]/body/{path}.{raw_offset}"


# --- EPUB access ---------------------------------------------------------------


def spine_documents(book_path: Path) -> dict[int, bytes]:
    """Map 1-based spine index -> raw XHTML bytes for an EPUB or KEPUB."""
    with zipfile.ZipFile(book_path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        opf_path = rootfile.get("full-path") if rootfile is not None else ""
        opf = ET.fromstring(zf.read(opf_path))
        base = "/".join(opf_path.split("/")[:-1])
        manifest = {i.get("id"): i.get("href") for i in opf.findall(".//{*}manifest/{*}item")}
        out: dict[int, bytes] = {}
        for i, ref in enumerate(opf.findall(".//{*}spine/{*}itemref"), start=1):
            href = manifest.get(ref.get("idref"))
            if not href:
                continue
            try:
                out[i] = zf.read(f"{base}/{href}" if base else href)
            except KeyError:
                continue
        return out


@dataclass(frozen=True)
class Resolution:
    fmt: str                 # "epub" | "kepub"
    fragment: int
    offset: int              # canonical offset within the spine item
    item_length: int         # canonical length of the spine item


def resolve_any(xpointer: str, candidates: dict[str, Path]) -> Resolution:
    """Resolve against each candidate file ("epub"/"kepub" -> path); return the first fit.

    KOReader's path encodes the file's markup, so a KEPUB path (koboSpan steps)
    fails against the EPUB and vice versa. That failure is what identifies the
    format the reader had open.
    """
    xp = parse_xpointer(xpointer)
    errors = []
    for fmt, path in candidates.items():
        try:
            items = spine_documents(path)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
            errors.append(f"{fmt}: unreadable ({exc})")
            continue
        raw = items.get(xp.fragment)
        if raw is None:
            errors.append(f"{fmt}: no spine item {xp.fragment}")
            continue
        doc = parse_document(raw)
        try:
            offset = resolve(doc, xp)
        except XPointerError as exc:
            errors.append(f"{fmt}: {exc}")
            continue
        return Resolution(fmt=fmt, fragment=xp.fragment, offset=offset, item_length=len(doc.text))
    raise XPointerError("; ".join(errors) or "no candidate files")
