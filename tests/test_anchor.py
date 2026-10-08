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


# Sisters of Scandal (Calibre 602): the KEPUB's content items as (spine index,
# chars) and its substantive ABS chapters as (index, start, end). Twenty-odd
# chapters of similar length, then a bibliography and image credits worth ~18%
# of the text that no recording reads.
SISTERS_ITEMS = [
    (9, 8000), (15, 9561), (17, 2787), (20, 9933), (22, 2009), (25, 3273), (27, 5909),
    (29, 3124), (34, 8603), (36, 1733), (38, 4232), (41, 3866), (43, 3868), (45, 5418),
    (49, 7058), (51, 7706), (55, 2837), (57, 6597), (60, 1950), (65, 4600), (67, 10707),
    (71, 5421), (73, 2197), (75, 4014), (78, 4892), (80, 10466), (83, 7206), (85, 4618),
    (90, 8516), (92, 2323), (95, 5743), (97, 4073), (99, 6606), (102, 3467), (104, 8080),
    (107, 2799), (109, 4317), (111, 4056), (116, 4620), (118, 1943), (119, 6539),
    (123, 6789), (125, 5109), (127, 3142), (130, 6439), (132, 4627), (134, 3223),
    (138, 12289), (141, 2991), (143, 4057), (145, 4789), (147, 5822), (150, 1635),
]
SISTERS_BACK_MATTER = [(153, 20669), (154, 41122)]
SISTERS_CHAPTERS = [
    (4, 42, 653), (6, 663, 1565), (7, 1565, 2415), (8, 2415, 3271), (10, 3282, 4377),
    (11, 4377, 5383), (12, 5383, 6459), (13, 6459, 7344), (15, 7355, 8525),
    (16, 8525, 9427), (17, 9427, 10676), (18, 10676, 11586), (20, 11597, 12372),
    (21, 12372, 13609), (22, 13609, 14492), (23, 14492, 15346), (25, 15359, 16398),
    (26, 16398, 17543), (27, 17543, 18663), (28, 18663, 19613), (29, 19613, 20523),
    (30, 20523, 20966),
]


class BookEdgeTest(unittest.TestCase):
    def setUp(self):
        self.chapters = [Chapter(index=i, title=str(i), start=float(a), end=float(b))
                         for i, a, b in SISTERS_CHAPTERS]

    def test_pairs_each_chapter_with_its_own_text_when_unnarrated_back_matter_follows(self):
        spine = ([item(i, chars) for i, chars in SISTERS_ITEMS]
                 + [item(i, chars, narrated=False) for i, chars in SISTERS_BACK_MATTER])
        alignment = build_alignment(spine, self.chapters)
        self.assertEqual(
            [(g.first_chapter, g.first_spine) for g in alignment.groups],
            [(4, 9), (6, 15), (7, 20), (8, 25), (10, 34), (11, 41), (12, 49), (13, 55),
             (15, 65), (16, 71), (17, 78), (18, 83), (20, 90), (21, 95), (22, 102),
             (23, 107), (25, 116), (26, 123), (27, 130), (28, 138), (29, 141), (30, 147)])

    def test_keeps_an_unnarrated_item_between_chapters_in_the_match(self):
        spine = [item(1), item(2), item(3, 2_000, narrated=False), item(4), item(5)]
        chapters = [Chapter(index=i, title=str(i), start=(i - 1) * 1000.0, end=i * 1000.0)
                    for i in (1, 2, 3, 4)]
        alignment = build_alignment(spine, chapters)
        self.assertIn(3, [p.spine_index for p in alignment.points])


class NeverNarratedTest(unittest.TestCase):
    def test_recognises_a_navigation_document_by_its_declared_role(self):
        self.assertTrue(_never_narrated(b'<html><body><nav epub:type="toc">'))

    def test_recognises_a_copyright_page_by_its_heading(self):
        self.assertTrue(_never_narrated(b"<html><body><h1>Copyright</h1><p>All rights reserved."))

    def test_recognises_a_bibliography_by_its_heading(self):
        self.assertTrue(_never_narrated(b'<html><body><h1><span class="koboSpan">BIBLIOGRAPHY</span></h1>'))

    def test_recognises_image_credits_by_their_heading(self):
        self.assertTrue(_never_narrated(b"<html><body><h1>Image Credits</h1><p>Page ii"))

    def test_treats_an_ordinary_chapter_as_narrated(self):
        self.assertFalse(_never_narrated(b'<html><body><section epub:type="chapter"><h1>CHAPTER 1</h1>'))

    def test_ignores_a_structural_role_declared_deep_inside_a_chapter(self):
        self.assertFalse(_never_narrated(b"x" * 5_000 + b'<a epub:type="toc">back</a>'))
