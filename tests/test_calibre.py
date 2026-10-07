import sqlite3
import tempfile
import unittest
from pathlib import Path

from concordance.calibre import koreader_checksum

ROWS = [
    (597, "EPUB", "epub-content", "koreader", "2026-10-01T15:00:00"),
    (597, "EPUB", "epub-name", "koreader_filename", "2026-10-01T15:00:01"),
    (597, "KEPUB", "kepub-old", "koreader", "2026-10-01T09:00:00"),
    (597, "KEPUB", "kepub-new", "koreader", "2026-10-01T10:00:00"),
]


class KoreaderChecksumTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "metadata.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE book_format_checksums "
                     "(id INTEGER PRIMARY KEY, book INTEGER, format TEXT, checksum TEXT, "
                     "version TEXT, created TEXT)")
        conn.executemany("INSERT INTO book_format_checksums (book, format, checksum, version, "
                         "created) VALUES (?, ?, ?, ?, ?)", ROWS)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_prefers_the_newest_content_checksum_of_the_open_format(self):
        self.assertEqual(koreader_checksum(self.db, 597, "kepub"), "kepub-new")

    def test_ignores_filename_based_checksums(self):
        self.assertEqual(koreader_checksum(self.db, 597, "epub"), "epub-content")

    def test_falls_back_to_the_newest_checksum_when_the_open_format_has_none(self):
        self.assertEqual(koreader_checksum(self.db, 597, "pdf"), "epub-content")

    def test_returns_none_for_a_book_without_checksums(self):
        self.assertIsNone(koreader_checksum(self.db, 1, "kepub"))

    def test_returns_none_when_the_checksum_table_is_missing(self):
        empty = str(Path(self.tmp.name) / "empty.db")
        sqlite3.connect(empty).close()
        self.assertIsNone(koreader_checksum(empty, 597, "kepub"))


if __name__ == "__main__":
    unittest.main()
