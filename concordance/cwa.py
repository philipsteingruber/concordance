"""Read reading progress from Calibre-Web-Automated's built-in KOSync server.

Two facts drive this module, both verified against the live stack:

1. `/kosync/export` returns identity, position and timestamps for every book
   that resolved to a Calibre id, in one authenticated call. No per-book fan-out.
2. `/kosync/export` does NOT carry the KOReader locator — only identity,
   percentage and timestamps. The XPointer comes from
   `/kosync/syncs/progress/<doc>`, one request per book, so it is fetched
   lazily for books we actually report on.
3. Percentage scale differs between endpoints: `/kosync/export` reports 0-100,
   while `/kosync/syncs/progress/<doc>` reports a 0-1 fraction. We normalise to
   0-100 at this boundary so nothing downstream has to remember which is which.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import requests

KOSYNC_ACCEPT = "application/vnd.koreader.v1+json"
_DOCFRAGMENT = re.compile(r"DocFragment\[(\d+)\]")


@dataclass(frozen=True)
class CwaProgress:
    """One book's reading position as CWA knows it."""

    calibre_book_id: int
    title: str
    authors: list[str]
    percentage: float          # always 0-100
    xpointer: str | None       # KOReader locator, e.g. /body/DocFragment[28]/...
    last_modified: datetime | None
    identifiers: dict[str, str] = field(default_factory=dict)

    @property
    def spine_index(self) -> int | None:
        """The 1-based EPUB spine item the reader is in, from the XPointer.

        `DocFragment[N]` is the Nth spine item. This is structural and exact,
        unlike `percentage`, which KOReader derives from its own screen
        pagination and which disagrees with the document by ~1 percentage point.
        """
        if not self.xpointer:
            return None
        match = _DOCFRAGMENT.search(self.xpointer)
        return int(match.group(1)) if match else None


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class CwaClient:
    """Thin client over CWA's KOSync endpoints, using HTTP Basic app-password auth."""

    def __init__(self, base_url: str, user: str, password: str, timeout: int = 30) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers["accept"] = KOSYNC_ACCEPT

    def check_auth(self) -> bool:
        """Cheapest possible credential check. Returns True on an authorised response."""
        try:
            resp = self._session.get(f"{self._base}/kosync/users/auth", timeout=self._timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"CWA unreachable at {self._base}: {exc}") from exc
        return resp.status_code == 200

    def locator(self, calibre_book_id: int) -> str | None:
        """Fetch the KOReader XPointer for one book.

        A numeric Calibre book id is accepted as the KOSync document key, so no
        partial-MD5 computation is needed. Returns None when the book has no
        locator (web-reader progress stores a `cwng:` marker instead).
        """
        try:
            resp = self._session.get(
                f"{self._base}/kosync/syncs/progress/{calibre_book_id}",
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError):
            return None
        progress = payload.get("progress") if isinstance(payload, dict) else None
        if not progress or str(progress).startswith("cwng:"):
            return None
        return str(progress)

    def put_progress(self, payload: dict) -> int:
        """PUT a KOSync progress record; returns the HTTP status (200 = accepted).

        A 200 does not mean the Kobo bookmark moved: CWA's arbiter silently keeps a
        higher stored percentage. Read back with `locator`/`export` to confirm.
        """
        try:
            resp = self._session.put(f"{self._base}/kosync/syncs/progress", json=payload,
                                     timeout=self._timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"CWA progress PUT failed: {exc}") from exc
        return resp.status_code

    def export(self) -> list[CwaProgress]:
        """Return progress for every book CWA resolved to a Calibre id.

        Books whose KOReader checksum never resolved are absent by design, as is
        web-reader progress (its locator is prefixed `cwng:` rather than being an
        XPointer).
        """
        try:
            resp = self._session.get(f"{self._base}/kosync/export", timeout=self._timeout)
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"CWA export failed: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("CWA export returned a non-JSON body") from exc

        if not isinstance(payload, list):
            raise RuntimeError(f"CWA export returned {type(payload).__name__}, expected a list")

        out: list[CwaProgress] = []
        for entry in payload:
            book_id = entry.get("calibre_book_id")
            if not book_id:
                continue  # unresolved checksum; nothing to pair against
            out.append(
                CwaProgress(
                    calibre_book_id=int(book_id),
                    title=entry.get("title") or "",
                    authors=list(entry.get("authors") or []),
                    percentage=float(entry.get("percentage") or 0.0),
                    xpointer=entry.get("progress"),
                    last_modified=_parse_timestamp(entry.get("last_modified")),
                    identifiers=dict(entry.get("identifiers") or {}),
                )
            )
        return out
