"""Plan and (optionally) apply progress writes to CWA and Audiobookshelf.

Dry run by default. A write is only *applied* when all of these hold:

1. `--apply` was given on the command line;
2. the book's Calibre id is in the write allowlist (the `allowlist` file, see
   allowlist.py, or `CONCORDANCE_WRITE_ALLOWLIST`), so writes can be enabled one
   book at a time: a test book, then one real book, then more;
3. the decision's tier is in `CONCORDANCE_WRITE_TIERS` (default
   `aligned,interpolated,finished`). `anchor` (chapter start, typically ~10 min
   out) and `percentage` tiers miss the one-minute bar and are never written by
   default.

Payload rules, each learned the hard way (see docs/design.md):

* **CWA** `PUT /kosync/syncs/progress`: `percentage` is a 0-1 fraction (the
  server multiplies values <= 1.0 by 100), written `CWA_PERCENT_MARGIN` points
  low so the Kobo's own subsequent pushes aren't rejected as "behind". `progress` is a real XPointer built
  for the file format the Kobo has open (a KEPUB path fails against the EPUB and
  vice versa). The device name is `concordance`, distinct from the Kobo, so
  KOSync's same-device-rewind rule can never let Concordance move the Kobo's own
  row backwards; the Kobo bookmark ratchets forward regardless.
* **ABS** `PATCH /api/me/progress/<item>`: never send `isFinished: false`, which
  resets `currentTime` to 0. A finished book is sent as `isFinished: true` at the
  full duration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .allowlist import allowlist_path, env_ids, read_ids
from .config import CWA_PERCENT_MARGIN
from .decide import Decision
from .xpointer import Document, parse_document, spine_documents, to_xpointer

DEVICE_NAME = "concordance"
DEFAULT_WRITE_TIERS = ("aligned", "interpolated", "finished")


@dataclass(frozen=True)
class WritePolicy:
    apply: bool = False
    allowlist: frozenset[int] = frozenset()
    tiers: tuple[str, ...] = DEFAULT_WRITE_TIERS

    @classmethod
    def from_env(cls, apply: bool) -> "WritePolicy":
        ids = frozenset(env_ids() | read_ids(allowlist_path()))
        tiers_raw = os.environ.get("CONCORDANCE_WRITE_TIERS", "")
        tiers = tuple(t.strip() for t in tiers_raw.split(",") if t.strip()) or DEFAULT_WRITE_TIERS
        return cls(apply=apply, allowlist=ids, tiers=tiers)


@dataclass
class PlannedWrite:
    target: str                       # "cwa" | "abs"
    calibre_book_id: int
    payload: dict
    description: str
    blocked_by: list[str] = field(default_factory=list)   # empty = would be applied

    @property
    def allowed(self) -> bool:
        return not self.blocked_by


def _book_file(book_dir: Path, fmt: str | None) -> tuple[str, Path] | None:
    """The file to build an XPointer for: the format the Kobo has open, else KEPUB, else EPUB."""
    order = [fmt] if fmt else []
    order += [f for f in ("kepub", "epub") if f not in order]
    for ext in order:
        found = sorted(book_dir.glob(f"*.{ext}"))
        if found:
            return ext, found[0]
    return None


def _nonspace_offset(doc: Document, fraction: float) -> int:
    if not doc.text:
        return 0
    offset = min(int(len(doc.text) * max(0.0, min(fraction, 1.0))), len(doc.text) - 1)
    while offset < len(doc.text) - 1 and doc.text[offset] == " ":
        offset += 1
    return offset


def plan_cwa_write(decision: Decision, calibre_book_id: int, book_dir: Path,
                   kobo_format: str | None) -> PlannedWrite | None:
    if decision.direction != "to_cwa":
        return None
    chosen = _book_file(book_dir, kobo_format)
    if chosen is None:
        return PlannedWrite("cwa", calibre_book_id, {}, "no EPUB/KEPUB to build an XPointer from",
                            blocked_by=["no book file"])
    fmt, path = chosen
    items = spine_documents(path)

    if decision.cwa_finished:
        spine = max(items)
        doc = parse_document(items[spine])
        xpointer = to_xpointer(doc, spine, max(len(doc.text) - 1, 0))
        percentage = 1.0
        description = f"mark finished ({fmt})"
    else:
        spine = decision.cwa_spine_index
        if spine is None or spine not in items or decision.cwa_item_fraction is None:
            return PlannedWrite("cwa", calibre_book_id, {}, "no spine position to write",
                                blocked_by=["no resolvable position"])
        doc = parse_document(items[spine])
        xpointer = to_xpointer(doc, spine, _nonspace_offset(doc, decision.cwa_item_fraction))
        percentage = round(max(0.0, (decision.cwa_percentage or 0.0) - CWA_PERCENT_MARGIN) / 100.0, 6)
        description = f"{percentage * 100:.1f}% at DocFragment[{spine}] ({fmt})"

    payload = {"document": str(calibre_book_id), "progress": xpointer, "percentage": percentage,
               "device": DEVICE_NAME, "device_id": DEVICE_NAME}
    return PlannedWrite("cwa", calibre_book_id, payload, description)


def plan_abs_write(decision: Decision, calibre_book_id: int, duration: float) -> PlannedWrite | None:
    if decision.direction != "to_abs":
        return None
    if decision.abs_finished:
        payload = {"isFinished": True, "currentTime": duration, "duration": duration, "progress": 1}
        description = "mark finished"
    else:
        if decision.abs_seconds is None or duration <= 0:
            return PlannedWrite("abs", calibre_book_id, {}, "no audio position to write",
                                blocked_by=["no position"])
        seconds = round(decision.abs_seconds, 1)
        payload = {"currentTime": seconds, "duration": duration,
                   "progress": round(seconds / duration, 6)}
        description = f"currentTime {int(seconds) // 3600}h{int(seconds) % 3600 // 60:02d}m{int(seconds) % 60:02d}s"
    # Never un-finish: isFinished:false resets currentTime to 0 server-side.
    assert payload.get("isFinished", True) is not False
    return PlannedWrite("abs", calibre_book_id, payload, description)


def gate(plan: PlannedWrite, decision: Decision, policy: WritePolicy) -> PlannedWrite:
    """Record every reason this write would not be applied."""
    if not policy.apply:
        plan.blocked_by.append("dry run (no --apply)")
    if plan.calibre_book_id not in policy.allowlist:
        plan.blocked_by.append("not in the write allowlist")
    if (decision.tier or "") not in policy.tiers:
        plan.blocked_by.append(f"tier '{decision.tier}' not in CONCORDANCE_WRITE_TIERS")
    return plan
