import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from concordance.calibre import SpineItem, _skip_leading_glossaries, koreader_checksum, read_spine

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


def spine_item(index, chars=20_000, narrated=True):
    return SpineItem(index=index, href=f"c{index}.xhtml", chars=chars, narrated=narrated)


def narrated_flags(items):
    return [item.narrated for item in items]


class LeadingGlossaryTest(unittest.TestCase):
    def test_marks_a_glossary_before_the_first_chapter_as_never_narrated(self):
        items = [spine_item(1, chars=6_800), spine_item(2), spine_item(3)]
        self.assertEqual(narrated_flags(_skip_leading_glossaries(items, {1})), [False, True, True])

    def test_looks_past_short_front_matter_to_find_the_glossary(self):
        items = [spine_item(1, chars=600), spine_item(2, chars=40), spine_item(3, chars=6_800),
                 spine_item(4)]
        self.assertEqual(narrated_flags(_skip_leading_glossaries(items, {3})),
                         [True, True, False, True])

    def test_keeps_a_glossary_after_the_first_chapter_narrated(self):
        items = [spine_item(1), spine_item(2, chars=6_800)]
        self.assertEqual(narrated_flags(_skip_leading_glossaries(items, {2})), [True, True])


CHAPTER = b"<html><body><h1>Chapter 1</h1><p>" + b"word " * 400 + b"</p></body></html>"


def write_epub(path, docs):
    manifest = "".join(f'<item id="i{n}" href="c{n}.xhtml" media-type="application/xhtml+xml"/>'
                       for n in range(len(docs)))
    spine = "".join(f'<itemref idref="i{n}"/>' for n in range(len(docs)))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
        zf.writestr("content.opf", f"<package><manifest>{manifest}</manifest>"
                                   f"<spine>{spine}</spine></package>")
        for n, doc in enumerate(docs):
            zf.writestr(f"c{n}.xhtml", doc)


class ReadSpineGlossaryTest(unittest.TestCase):
    def test_recognises_a_glossary_heading_prefixed_with_the_series_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub = Path(tmp) / "book.epub"
            glossary = ('<html><body><h2><span class="koboSpan">Court of Chains \u2013 Glossary'
                        "</span></h2></body></html>").encode()
            write_epub(epub, [glossary, CHAPTER])
            self.assertEqual(narrated_flags(read_spine(epub)), [False, True])
