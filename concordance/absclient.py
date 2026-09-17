"""Read audiobook metadata and listening progress from Audiobookshelf.

Notes verified against ABS 2.36.0 source, not the published docs (which are
stale and describe routes that no longer exist):

* progress lives at `PATCH/GET /api/me/progress`, not `/api/me/media-progress`
* `lastUpdate` and `startedAt` are milliseconds since epoch
* writing progress here does NOT create a playback session or accrue
  listening time, so external writes cannot inflate ABS statistics
* there is no stale-write guard server-side: last write wins, unconditionally
* the library-items listing serialises books with `toOldJSONMinified()`, which
  carries `numChapters` (a count) but NOT the `chapters` array. Only
  `GET /api/items/<id>?expanded=1` returns real chapter structure, so chapters
  are loaded lazily per book rather than in the listing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests


@dataclass(frozen=True)
class Chapter:
    index: int      # 1-based
    title: str
    start: float    # seconds
    end: float      # seconds

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class AbsBook:
    library_item_id: str
    title: str
    authors: str
    duration: float
    chapters: list[Chapter] = field(default_factory=list)
    isbn: str | None = None
    asin: str | None = None
    num_chapters: int = 0   # from the listing, before chapters are loaded

    def substantive_chapters(self, min_seconds: float = 60.0) -> list[Chapter]:
        """Chapters long enough to be real content.

        Audiobooks routinely carry sub-minute 'Opening Credits' / 'End Credits'
        tracks that have no counterpart in the ebook's spine. Including them
        would shift the whole ordinal mapping by one.
        """
        return [c for c in self.chapters if c.duration >= min_seconds]


@dataclass(frozen=True)
class AbsProgress:
    library_item_id: str
    current_time: float
    duration: float
    progress: float          # 0-1 fraction, as ABS reports it
    is_finished: bool
    last_update: datetime | None


def _ms_to_dt(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


class AbsClient:
    """Thin client over the Audiobookshelf API, authenticated with an API key."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"

    def _get(self, path: str) -> dict | list:
        try:
            resp = self._session.get(f"{self._base}{path}", timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"ABS request to {path} failed: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"ABS returned a non-JSON body for {path}") from exc

    def book_libraries(self) -> list[dict]:
        """Libraries whose mediaType is `book` (i.e. not podcasts)."""
        payload = self._get("/api/libraries")
        libs = payload.get("libraries", []) if isinstance(payload, dict) else []
        return [lib for lib in libs if lib.get("mediaType") == "book"]

    def chapters(self, library_item_id: str) -> list[Chapter]:
        """Fetch chapter structure for one book.

        Required because the library listing omits the chapters array; see the
        module docstring. Callers should request this only for books they will
        actually align, to avoid a request per library item.
        """
        payload = self._get(f"/api/items/{library_item_id}?expanded=1")
        media = payload.get("media") or {} if isinstance(payload, dict) else {}
        return [
            Chapter(
                index=i,
                title=str(c.get("title") or ""),
                start=float(c.get("start") or 0.0),
                end=float(c.get("end") or 0.0),
            )
            for i, c in enumerate(media.get("chapters") or [], start=1)
        ]

    def audio_manifest(self, library_item_id: str) -> list[tuple[str, float]]:
        """The item's audio files in playback order: (ABS-side path, duration seconds)."""
        payload = self._get(f"/api/items/{library_item_id}?expanded=1")
        media = payload.get("media") or {} if isinstance(payload, dict) else {}
        files = sorted(media.get("audioFiles") or [], key=lambda f: f.get("index") or 0)
        return [((f.get("metadata") or {}).get("path", ""), float(f.get("duration") or 0.0))
                for f in files]

    def books(self) -> list[AbsBook]:
        """Every book across all book libraries.

        Chapters are NOT populated here — the listing does not include them.
        Call `chapters()` for the books you actually need.
        """
        out: list[AbsBook] = []
        for lib in self.book_libraries():
            payload = self._get(f"/api/libraries/{lib['id']}/items?limit=0")
            for item in (payload.get("results", []) if isinstance(payload, dict) else []):
                media = item.get("media") or {}
                meta = media.get("metadata") or {}
                chapters = [
                    Chapter(
                        index=i,
                        title=str(c.get("title") or ""),
                        start=float(c.get("start") or 0.0),
                        end=float(c.get("end") or 0.0),
                    )
                    for i, c in enumerate(media.get("chapters") or [], start=1)
                ]
                out.append(  # chapters stays empty from this endpoint
                    AbsBook(
                        library_item_id=item["id"],
                        title=str(meta.get("title") or ""),
                        authors=str(meta.get("authorName") or ""),
                        duration=float(media.get("duration") or 0.0),
                        chapters=chapters,
                        isbn=meta.get("isbn"),
                        asin=meta.get("asin"),
                        num_chapters=int(media.get("numChapters") or 0),
                    )
                )
        return out

    def patch_progress(self, library_item_id: str, payload: dict) -> int:
        """PATCH listening progress; returns the HTTP status (200 = saved).

        Never pass `isFinished: False`: ABS resets currentTime to 0 when un-finishing.
        """
        if payload.get("isFinished", True) is False:
            raise ValueError("refusing to send isFinished:false (resets currentTime to 0)")
        try:
            resp = self._session.patch(f"{self._base}/api/me/progress/{library_item_id}",
                                       json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"ABS progress PATCH failed: {exc}") from exc
        return resp.status_code

    def progress(self) -> dict[str, AbsProgress]:
        """Current listening progress, keyed by libraryItemId."""
        payload = self._get("/api/me/progress")
        rows = payload.get("mediaProgress", []) if isinstance(payload, dict) else []
        out: dict[str, AbsProgress] = {}
        for row in rows:
            item_id = row.get("libraryItemId")
            # Podcast episodes carry an episodeId; they have no ebook counterpart.
            if not item_id or row.get("episodeId"):
                continue
            out[item_id] = AbsProgress(
                library_item_id=item_id,
                current_time=float(row.get("currentTime") or 0.0),
                duration=float(row.get("duration") or 0.0),
                progress=float(row.get("progress") or 0.0),
                is_finished=bool(row.get("isFinished")),
                last_update=_ms_to_dt(row.get("lastUpdate")),
            )
        return out
