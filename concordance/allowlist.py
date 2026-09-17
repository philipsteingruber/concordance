"""The write allowlist: Calibre ids Concordance may write progress for.

Kept in a plain file, one id per line, so it can be edited without touching the
credential file:

    561  # Misery
    581  # The Escape Room

`#` starts a comment and lines without a leading id are ignored. The file is
`allowlist` in the repository root unless CONCORDANCE_WRITE_ALLOWLIST_FILE says
otherwise. Ids in the CONCORDANCE_WRITE_ALLOWLIST environment variable are
allowed too, so either place works.

    concordance-allow add 561
    concordance-allow remove 561
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def allowlist_path() -> Path:
    return Path(os.environ.get("CONCORDANCE_WRITE_ALLOWLIST_FILE") or REPO / "allowlist")


def _line_id(line: str) -> int | None:
    head = line.split("#", 1)[0].strip()
    return int(head) if head.isdigit() else None


def read_ids(path: Path) -> set[int]:
    """Ids in the file; a missing file is an empty allowlist."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return set()
    return {i for i in map(_line_id, text.splitlines()) if i is not None}


def env_ids() -> set[int]:
    raw = os.environ.get("CONCORDANCE_WRITE_ALLOWLIST", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(f"{line}\n" for line in lines))
    tmp.replace(path)


def add(path: Path, book_id: int, title: str | None = None) -> bool:
    """Add an id. Returns False if it was already there."""
    lines = path.read_text().splitlines() if path.exists() else []
    if any(_line_id(line) == book_id for line in lines):
        return False
    lines.append(f"{book_id}  # {title}" if title else str(book_id))
    _write(path, lines)
    return True


def remove(path: Path, book_id: int) -> bool:
    """Remove an id, keeping every other line. Returns False if it wasn't there."""
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    kept = [line for line in lines if _line_id(line) != book_id]
    if len(kept) == len(lines):
        return False
    _write(path, kept)
    return True


def calibre_title(book_id: int) -> str | None:
    """The book's title from Calibre's database, if it can be read. Used for the comment."""
    root = os.environ.get("CALIBRE_ROOT", "").rstrip("/")
    db = os.environ.get("CALIBRE_DB") or (f"{root}/metadata.db" if root else "")
    if not db:
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="concordance-allow",
                                 description="Add or remove books (Calibre ids) in the write allowlist.")
    ap.add_argument("action", choices=("add", "remove"))
    ap.add_argument("book_id", type=int, nargs="+")
    args = ap.parse_args(argv)

    path = allowlist_path()
    for book_id in args.book_id:
        if args.action == "add":
            title = calibre_title(book_id)
            if title is None and (os.environ.get("CALIBRE_ROOT") or os.environ.get("CALIBRE_DB")):
                print(f"warning: no Calibre book with id {book_id}", file=sys.stderr)
            changed = add(path, book_id, title)
            label = f"{book_id} ({title})" if title else str(book_id)
            print(f"added {label}" if changed else f"{label} is already allowed")
        else:
            changed = remove(path, book_id)
            print(f"removed {book_id}" if changed else f"{book_id} wasn't in {path}")
            if book_id in env_ids():
                print(f"warning: {book_id} is still allowed by CONCORDANCE_WRITE_ALLOWLIST; "
                      "remove it there too", file=sys.stderr)
    print(f"allowlist ({path}): {', '.join(map(str, sorted(read_ids(path)))) or 'empty'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
