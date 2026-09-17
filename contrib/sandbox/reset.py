"""Reset a sandbox book's reading progress in CWA to baseline.

Why this exists: CWA-NextGen's Kobo bookmark only ratchets forward — the SQL
arbiter behind the KOSync mirror rejects any percentage lower than the stored
one — so test progress cannot be taken back through the API.

"Baseline" means the placeholder state every book on the Kobo sync list is
created in: one book_read_link /
kobo_reading_state / kobo_bookmark / kobo_statistics row each, with
read_status 0 and a NULL progress_percent. Resetting *restores those values*
rather than deleting the rows — deleting would leave the sandbox in a state no
other book is in. A NULL percentage also guarantees the arbiter accepts the
next test push.

This is the one place Concordance writes to CWA's SQLite, and it is fenced in:

* the book comes from CONCORDANCE_SANDBOX_BOOK_ID, never a command-line argument
* it refuses to touch a book with KOSync progress from any device not listed in
  CONCORDANCE_SANDBOX_DEVICES, so a real reading position can't be wiped
* dry-run by default; `--apply` is required to write
* one `BEGIN IMMEDIATE` transaction — all or nothing
* progress columns only; timestamps, device entitlements, shelves, downloads
  and annotations are left alone

Bypassing the ORM is deliberate: no `kobo_reading_state.last_modified` bump
means nothing is pushed to a device, and CWA can keep running.

Configuration (environment):
  CONCORDANCE_SANDBOX_BOOK_ID   Calibre id of the sandbox book (required)
  CWA_APP_DB                    path to CWA's app.db (required)
  CALIBRE_DB / CALIBRE_ROOT     Calibre metadata.db, for legacy KOReader checksums
  CONCORDANCE_SANDBOX_DEVICES   KOSync device names allowed to have written progress
                                (default: concordance,concordance-sandbox)
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


def _env_config() -> tuple[int, str, str, set[str]]:
    book = os.environ.get("CONCORDANCE_SANDBOX_BOOK_ID", "").strip()
    app_db = os.environ.get("CWA_APP_DB", "").strip()
    if not book.isdigit() or not app_db:
        raise SystemExit("error: set CONCORDANCE_SANDBOX_BOOK_ID (a Calibre id) and CWA_APP_DB")
    root = os.environ.get("CALIBRE_ROOT", "").rstrip("/")
    metadata = os.environ.get("CALIBRE_DB") or (f"{root}/metadata.db" if root else "")
    devices = {d.strip() for d in os.environ.get(
        "CONCORDANCE_SANDBOX_DEVICES", "concordance,concordance-sandbox").split(",") if d.strip()}
    return int(book), app_db, metadata, devices


SANDBOX_BOOK_ID, APP_DB, METADATA_DB, SANDBOX_DEVICES = 0, "", "", set()

_STATE_IDS = "SELECT id FROM kobo_reading_state WHERE book_id = :book"

RESTORE: list[str] = [
    f"""UPDATE kobo_bookmark SET progress_percent = NULL,
        content_source_progress_percent = NULL, location_source = NULL,
        location_type = NULL, location_value = NULL, created_at = NULL
        WHERE kobo_reading_state_id IN ({_STATE_IDS})""",
    f"""UPDATE kobo_statistics SET remaining_time_minutes = NULL,
        spent_reading_minutes = NULL
        WHERE kobo_reading_state_id IN ({_STATE_IDS})""",
    """UPDATE book_read_link SET read_status = 0, times_started_reading = 0,
        last_time_started_reading = NULL WHERE book_id = :book""",
    # Neither of these exists at baseline, so they are removed outright.
    "DELETE FROM bookmark WHERE book_id = :book",
]


def legacy_checksums(book_id: int) -> list[str]:
    """KOReader checksums for the book, for kosync rows written before resolution."""
    if not METADATA_DB:
        return []
    try:
        conn = sqlite3.connect(f"file:{METADATA_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        return [r[0] for r in conn.execute(
            "SELECT checksum FROM book_format_checksums WHERE book = ?", (book_id,)
        )]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def deviations(conn: sqlite3.Connection, docs: list[str]) -> dict[str, int]:
    """Count rows that differ from baseline. All zero means untouched."""
    marks = ",".join("?" * len(docs))
    b = {"book": SANDBOX_BOOK_ID}
    one = lambda sql, params: conn.execute(sql, params).fetchone()[0]  # noqa: E731
    return {
        "kobo_bookmark with progress": one(
            f"""SELECT count(*) FROM kobo_bookmark WHERE kobo_reading_state_id IN ({_STATE_IDS})
                AND (progress_percent IS NOT NULL OR location_value IS NOT NULL)""", b),
        "kobo_statistics with values": one(
            f"""SELECT count(*) FROM kobo_statistics WHERE kobo_reading_state_id IN ({_STATE_IDS})
                AND (spent_reading_minutes IS NOT NULL OR remaining_time_minutes IS NOT NULL)""", b),
        "book_read_link not unread": one(
            """SELECT count(*) FROM book_read_link WHERE book_id = :book
                AND (read_status != 0 OR times_started_reading != 0)""", b),
        "bookmark (web reader)": one("SELECT count(*) FROM bookmark WHERE book_id = :book", b),
        "kosync_progress": one(f"SELECT count(*) FROM kosync_progress WHERE document IN ({marks})", docs),
    }


def reset(apply: bool) -> int:
    docs = [str(SANDBOX_BOOK_ID), *legacy_checksums(SANDBOX_BOOK_ID)]
    marks = ",".join("?" * len(docs))
    mode = "" if apply else "?mode=ro"
    try:
        conn = sqlite3.connect(f"file:{APP_DB}{mode}", uri=True, timeout=10,
                               isolation_level=None)
    except sqlite3.Error as exc:
        print(f"error: cannot open {APP_DB}: {exc}", file=sys.stderr)
        return 1

    try:
        foreign = conn.execute(
            f"SELECT DISTINCT device FROM kosync_progress WHERE document IN ({marks})", docs
        ).fetchall()
        foreign = sorted({d for (d,) in foreign if d not in SANDBOX_DEVICES})
        if foreign:
            print(f"error: book {SANDBOX_BOOK_ID} has KOSync progress from {', '.join(foreign)}, "
                  "which isn't a sandbox device. Refusing to reset a book that may hold real "
                  "reading; add the device to CONCORDANCE_SANDBOX_DEVICES if it was a test.",
                  file=sys.stderr)
            return 1
        before = deviations(conn, docs)
        print(f"Sandbox book {SANDBOX_BOOK_ID} — rows differing from baseline:")
        for label, n in before.items():
            print(f"  {label:<30} {n}")
        if not any(before.values()):
            print("Already at baseline. Nothing to do.")
            return 0
        if not apply:
            print("\nDry run. Re-run with --apply to restore baseline.")
            return 0

        conn.execute("BEGIN IMMEDIATE")
        try:
            for sql in RESTORE:
                conn.execute(sql, {"book": SANDBOX_BOOK_ID})
            conn.execute(f"DELETE FROM kosync_progress WHERE document IN ({marks})", docs)
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise

        after = deviations(conn, docs)
        if any(after.values()):
            print(f"error: still differs from baseline after reset: {after}", file=sys.stderr)
            return 1
        print("\nReset to baseline.")
        return 0
    except sqlite3.Error as exc:
        print(f"error: reset failed, nothing committed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    global SANDBOX_BOOK_ID, APP_DB, METADATA_DB, SANDBOX_DEVICES
    parser = argparse.ArgumentParser(
        prog="contrib/sandbox/reset.py",
        description="Restore baseline CWA progress for the sandbox book (CONCORDANCE_SANDBOX_BOOK_ID) only.",
    )
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    args = parser.parse_args(argv)
    SANDBOX_BOOK_ID, APP_DB, METADATA_DB, SANDBOX_DEVICES = _env_config()
    return reset(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
