import unittest

from concordance.absclient import Chapter
from concordance.anchor import build_alignment, trim_unnarrated_edges
from concordance.calibre import SpineItem, _never_narrated


def item(index, chars=20_000, narrated=True):
    return SpineItem(index=index, href=f"c{index}.xhtml", chars=chars, narrated=narrated)


class TrimUnnarratedEdgesTest(unittest.TestCase):
    def test_drops_a_table_of_contents_from_the_front_of_a_group(self):
        items = [item(1, 2_000, narrated=False), item(2), item(3)]
        self.assertEqual([i.index for i in trim_unnarrated_edges(items)], [2, 3])

    def test_drops_bonus_material_from_the_back_of_a_group(self):
        items = [item(1), item(2), item(3, 4_000, narrated=False)]
        self.assertEqual([i.index for i in trim_unnarrated_edges(items)], [1, 2])

    def test_keeps_an_interior_item_even_when_it_is_never_narrated(self):
        items = [item(1), item(2, 500, narrated=False), item(3)]
        self.assertEqual([i.index for i in trim_unnarrated_edges(items)], [1, 2, 3])

    def test_keeps_every_item_when_none_of_them_is_narrated(self):
        items = [item(1, narrated=False), item(2, narrated=False)]
        self.assertEqual([i.index for i in trim_unnarrated_edges(items)], [1, 2])

    def test_keeps_a_group_that_needs_no_trimming_unchanged(self):
        items = [item(1), item(2)]
        self.assertEqual([i.index for i in trim_unnarrated_edges(items)], [1, 2])


class GroupBoundsTest(unittest.TestCase):
    """A front-matter item absorbed into the first group, as Jade City has."""

    def setUp(self):
        self.spine = [item(1, 2_000, narrated=False)] + [item(i) for i in (2, 3, 4, 5)]
        self.chapters = [Chapter(index=i, title=str(i), start=(i - 1) * 1000.0, end=i * 1000.0)
                         for i in (1, 2, 3, 4)]
        self.alignment = build_alignment(self.spine, self.chapters)

    def test_starts_the_first_group_at_the_first_narrated_item(self):
        self.assertEqual(self.alignment.groups[0].first_spine, 2)

    def test_keeps_the_audio_window_covering_the_seconds_the_trimmed_text_would_have_claimed(self):
        self.assertEqual(self.alignment.groups[0].audio_start, 0.0)


class NeverNarratedTest(unittest.TestCase):
    def test_recognises_a_navigation_document_by_its_declared_role(self):
        self.assertTrue(_never_narrated(b'<html><body><nav epub:type="toc">'))

    def test_recognises_a_copyright_page_by_its_heading(self):
        self.assertTrue(_never_narrated(b"<html><body><h1>Copyright</h1><p>All rights reserved."))

    def test_treats_an_ordinary_chapter_as_narrated(self):
        self.assertFalse(_never_narrated(b'<html><body><section epub:type="chapter"><h1>CHAPTER 1</h1>'))

    def test_ignores_a_structural_role_declared_deep_inside_a_chapter(self):
        self.assertFalse(_never_narrated(b"x" * 5_000 + b'<a epub:type="toc">back</a>'))
