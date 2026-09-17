import tempfile
import unittest
import zipfile
from pathlib import Path

from concordance.xpointer import (
    XPointerError,
    parse_document,
    parse_xpointer,
    resolve,
    resolve_any,
    to_xpointer,
)

EPUB_BODY = (
    "<html><head><title>t</title></head><body>"
    "<h1>Chapter One</h1>"
    "<p>First paragraph here.</p>"
    "<p>Second <em>emphasised</em> paragraph.</p>"
    "</body></html>"
)
# Same content as a KEPUB: kepubify's wrapper divs plus koboSpan sentence spans.
KEPUB_BODY = (
    "<html><head><title>t</title></head><body>"
    '<div id="book-columns"><div id="book-inner">'
    '<h1><span class="koboSpan" id="kobo.1.1">Chapter One</span></h1>'
    '<p><span class="koboSpan" id="kobo.2.1">First paragraph here.</span></p>'
    '<p><span class="koboSpan" id="kobo.3.1">Second <em>emphasised</em> paragraph.</span></p>'
    "</div></div></body></html>"
)


def canonical(body: str) -> str:
    return parse_document(body).text


class CanonicalTextTest(unittest.TestCase):
    def test_inserts_a_space_between_adjacent_block_elements(self):
        self.assertEqual(canonical("<body><p>end.</p><p>Start</p></body>"), "end. Start")

    def test_joins_inline_elements_without_adding_a_space(self):
        self.assertEqual(canonical("<body><p>Hel<b>lo</b> there</p></body>"), "Hello there")

    def test_collapses_whitespace_runs_into_a_single_space(self):
        self.assertEqual(canonical("<body><p>a \n\t  b</p></body>"), "a b")

    def test_excludes_script_and_style_content(self):
        self.assertEqual(canonical("<body><style>p{}</style><p>kept</p><script>x()</script></body>"), "kept")

    def test_produces_identical_text_for_an_epub_and_its_kepub_markup(self):
        self.assertEqual(canonical(EPUB_BODY), canonical(KEPUB_BODY))


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.doc = parse_document(EPUB_BODY)

    def test_resolves_a_text_offset_to_the_matching_canonical_character(self):
        offset = resolve(self.doc, parse_xpointer("/body/DocFragment[3]/body/p[2]/text().3"))
        self.assertEqual(self.doc.text[offset:offset + 4], "ond ")

    def test_treats_an_omitted_index_the_same_as_an_explicit_first_index(self):
        implicit = resolve(self.doc, parse_xpointer("/body/DocFragment[3]/body/h1/text().8"))
        explicit = resolve(self.doc, parse_xpointer("/body/DocFragment[3]/body/h1[1]/text()[1].8"))
        self.assertEqual(implicit, explicit)

    def test_places_an_element_only_path_at_the_start_of_its_text(self):
        offset = resolve(self.doc, parse_xpointer("/body/DocFragment[3]/body/p[2]"))
        self.assertTrue(self.doc.text[offset:].startswith("Second"))

    def test_rejects_a_path_whose_element_does_not_exist(self):
        with self.assertRaises(XPointerError):
            resolve(self.doc, parse_xpointer("/body/DocFragment[3]/body/p[9]/text().0"))

    def test_rejects_a_string_that_is_not_a_docfragment_xpointer(self):
        with self.assertRaises(XPointerError):
            parse_xpointer("cwng:percentage")

    def test_resolves_a_kobospan_path_to_the_same_offset_as_the_epub_path(self):
        kepub = parse_document(KEPUB_BODY)
        via_kepub = resolve(kepub, parse_xpointer(
            "/body/DocFragment[3]/body/div/div/p[2]/span/text().3"))
        via_epub = resolve(self.doc, parse_xpointer("/body/DocFragment[3]/body/p[2]/text().3"))
        self.assertEqual(via_kepub, via_epub)


class ToXPointerTest(unittest.TestCase):
    def test_builds_an_xpointer_that_resolves_back_to_the_same_offset(self):
        for body in (EPUB_BODY, KEPUB_BODY):
            doc = parse_document(body)
            for offset, ch in enumerate(doc.text):
                if ch == " ":
                    continue
                with self.subTest(body=body[:20], offset=offset):
                    xp = to_xpointer(doc, 3, offset)
                    self.assertEqual(resolve(doc, parse_xpointer(xp)), offset)

    def test_omits_the_index_for_an_element_without_same_name_siblings(self):
        doc = parse_document(EPUB_BODY)
        self.assertEqual(to_xpointer(doc, 3, 0), "/body/DocFragment[3]/body/h1/text().0")

    def test_includes_the_index_for_an_element_with_same_name_siblings(self):
        doc = parse_document(EPUB_BODY)
        offset = doc.text.index("First")
        self.assertIn("/p[1]/", to_xpointer(doc, 3, offset))


def build_epub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        zf.writestr("OEBPS/content.opf",
                    '<package><manifest>'
                    '<item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/><item id="c" href="c.xhtml"/>'
                    '</manifest><spine><itemref idref="a"/><itemref idref="b"/><itemref idref="c"/></spine></package>')
        zf.writestr("OEBPS/a.xhtml", "<body><p>cover</p></body>")
        zf.writestr("OEBPS/b.xhtml", "<body><p>title</p></body>")
        zf.writestr("OEBPS/c.xhtml", body)
    return path


class ResolveAnyTest(unittest.TestCase):
    def test_identifies_the_kepub_when_the_path_contains_kobo_wrapper_markup(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {"epub": build_epub(Path(tmp), "b.epub", EPUB_BODY),
                     "kepub": build_epub(Path(tmp), "b.kepub", KEPUB_BODY)}
            result = resolve_any("/body/DocFragment[3]/body/div/div/p[2]/span/text().3", files)
        self.assertEqual(result.fmt, "kepub")

    def test_identifies_the_epub_when_the_path_fits_plain_markup(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {"epub": build_epub(Path(tmp), "b.epub", EPUB_BODY),
                     "kepub": build_epub(Path(tmp), "b.kepub", KEPUB_BODY)}
            result = resolve_any("/body/DocFragment[3]/body/p[2]/text().3", files)
        self.assertEqual(result.fmt, "epub")


if __name__ == "__main__":
    unittest.main()
