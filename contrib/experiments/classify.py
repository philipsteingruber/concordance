"""Prototype non-content classifier — experiment only, not wired into concordance.

Measured against the ordinal matcher it barely helped (see docs/design.md). The boundary
matcher handles trailing extras via edge skipping instead.
"""
import re, zipfile, html
from xml.etree import ElementTree as ET

NEVER_NARRATED = {"cover","titlepage","halftitlepage","toc","copyright-page","colophon","index",
                  "loi","lot","landmarks","page-list","imprint","contributors","other-credits"}
SOMETIMES = {"dedication","epigraph","acknowledgments","acknowledgements","foreword","preface",
             "afterword","appendix","glossary","bibliography","notes","endnotes","footnotes"}
HEADING_NC = re.compile(r"^\s*(contents|table of contents|copyright|also by|praise for|about the author|"
                        r"about the publisher|acknowledg|dedication|title page|newsletter|sign up|"
                        r"books by|by the same author|a note on the (type|author))", re.I)
EPUBTYPE = re.compile(rb'epub:type="([^"]+)"')

def epub_roles(epub_path):
    """[(spine_index, role, reason)] where role in content|non_content|sometimes."""
    z = zipfile.ZipFile(epub_path)
    opfp = ET.fromstring(z.read("META-INF/container.xml")).find(".//{*}rootfile").get("full-path")
    opf = ET.fromstring(z.read(opfp)); base = "/".join(opfp.split("/")[:-1])
    full = lambda h: f"{base}/{h}" if base else h
    man = {i.get("id"): i.get("href") for i in opf.findall(".//{*}manifest/{*}item")}
    guide = {}
    for r in opf.findall(".//{*}guide/{*}reference"):
        guide[(r.get("href") or "").split("#")[0]] = (r.get("type") or "").lower().replace("other.", "")
    out = []
    for i, ref in enumerate(opf.findall(".//{*}spine/{*}itemref"), start=1):
        href = man.get(ref.get("idref")) or ""
        try: raw = z.read(full(href))
        except KeyError: out.append((i, "non_content", "missing")); continue
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", re.sub(rb"(?s)<(script|style).*?</\1>", b" ", raw).decode("utf8", "ignore")))).strip()
        # epub:type on the first ~2KB of body — section-level, not inline noterefs
        head = raw[raw.find(b"<body"):][:2000] if b"<body" in raw else raw[:2000]
        types = {t for m in EPUBTYPE.findall(head) for t in m.decode().lower().split()}
        gtype = guide.get(href, "")
        if ref.get("linear") == "no":
            out.append((i, "non_content", "linear=no")); continue
        if types & NEVER_NARRATED or gtype in NEVER_NARRATED or gtype in {"copyright", "title-page"}:
            out.append((i, "non_content", f"epub:type/guide {sorted(types & NEVER_NARRATED) or gtype}")); continue
        if HEADING_NC.match(text[:80]):
            role = "sometimes" if re.match(r"^\s*(acknowledg|dedication)", text, re.I) else "non_content"
            out.append((i, role, f"heading '{text[:30]}'")); continue
        if types & SOMETIMES:
            out.append((i, "sometimes", f"epub:type {sorted(types & SOMETIMES)}")); continue
        if len(text) < 1500:
            out.append((i, "non_content", f"short ({len(text)} chars)")); continue
        out.append((i, "content", ""))
    return out

ABS_NC = re.compile(r"credit|copyright|title page|about the author|also by|preview|excerpt|"
                    r"^\s*(the )?end\s*$|bonus", re.I)
ABS_SOMETIMES = re.compile(r"dedication|epigraph|acknowledg|thank|author.?s note|foreword|afterword|appendix|glossary", re.I)

def abs_roles(chapters):
    """[(index, role, reason)] for ABS chapter dicts with start/end/title."""
    out = []
    for i, c in enumerate(chapters, start=1):
        t = c.get("title") or ""; d = c["end"] - c["start"]
        if ABS_NC.search(t): out.append((i, "non_content", f"title '{t}'"))
        elif d < 60: out.append((i, "non_content", f"short ({d:.0f}s)"))
        elif ABS_SOMETIMES.search(t): out.append((i, "sometimes", f"title '{t}'"))
        else: out.append((i, "content", ""))
    return out
