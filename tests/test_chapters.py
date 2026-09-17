import unittest

from concordance.absclient import Chapter
from concordance.chapterdetect import ChapterStart, _label_parts, _numbered_runs, chapter_number, heading_blocks
from concordance.chapterprobe import estimate, half_window, judge
from concordance.chapters import build_chapters, regions
from concordance.xpointer import parse_document


class ChapterNumberTest(unittest.TestCase):
    def test_reads_a_bare_digit(self):
        self.assertEqual(chapter_number("12"), 12)

    def test_reads_a_roman_numeral(self):
        self.assertEqual(chapter_number("XII"), 12)

    def test_reads_a_spelled_number_after_a_chapter_prefix(self):
        self.assertEqual(chapter_number("Chapter Twenty-One"), 21)

    def test_reads_a_number_followed_by_a_chapter_title(self):
        self.assertEqual(chapter_number("Chapter 12: The Storm"), 12)

    def test_ignores_a_sentence_that_starts_with_a_number_word(self):
        self.assertIsNone(chapter_number("Two men walked in"))

    def test_ignores_a_heading_longer_than_a_chapter_title(self):
        self.assertIsNone(chapter_number("1 " + "word " * 30))


class NumberedHeadingsTest(unittest.TestCase):
    def blocks(self, body):
        return heading_blocks(parse_document(f"<html><body>{body}</body></html>"))

    def test_finds_consecutive_numbered_blocks_between_paragraphs(self):
        body = "".join(f'<div class="cst">{n}</div><p>{"text " * 50}</p>' for n in (1, 2, 3))
        runs = _numbered_runs(self.blocks(body))
        self.assertEqual([n for _, _, n in runs[0]], [1, 2, 3])

    def test_does_not_count_a_number_that_breaks_the_sequence(self):
        body = '<div>1</div><p>a</p><div>2</div><p>b</p><div>1987</div><p>c</p><div>3</div>'
        runs = _numbered_runs(self.blocks(body))
        self.assertEqual([n for _, _, n in runs[0]], [1, 2, 3])


class PartLabelTest(unittest.TestCase):
    def test_prefixes_the_part_name_when_chapter_numbers_restart(self):
        starts = [ChapterStart(10, 0, "Chapter 1", "heading"), ChapterStart(10, 900, "Chapter 2", "heading"),
                  ChapterStart(12, 0, "Chapter 1", "heading")]
        labelled = _label_parts(starts, {10: "Part One", 12: "Part Two"})
        self.assertEqual([s.title for s in labelled],
                         ["Part One, Chapter 1", "Part One, Chapter 2", "Part Two, Chapter 1"])

    def test_leaves_titles_alone_when_they_are_already_unique(self):
        starts = [ChapterStart(10, 0, "Chapter 1", "heading"), ChapterStart(10, 900, "Chapter 2", "heading")]
        self.assertEqual(_label_parts(starts, {10: "Part One"}), starts)


def chapters(*bounds):
    return [Chapter(i, f"Slice {i}", a, b) for i, (a, b) in enumerate(bounds, start=1)]


class RegionsTest(unittest.TestCase):
    def test_merges_adjacent_abs_chapters_that_each_hold_several_starts(self):
        abs_chapters = chapters((0, 100), (100, 200), (200, 300))
        times = [0, 40, 80, 110, 150, 250]
        self.assertEqual(regions(abs_chapters, times), [(0, 1)])

    def test_rebuilds_an_unsplit_slice_sitting_between_split_ones(self):
        abs_chapters = chapters((0, 100), (100, 200), (200, 300))
        times = [0, 40, 80, 150, 210, 260]
        self.assertEqual(regions(abs_chapters, times), [(0, 2)])

    def test_splits_nothing_when_abs_already_has_about_as_many_chapters(self):
        abs_chapters = chapters((0, 100), (100, 200), (200, 300), (300, 400))
        times = [10, 90, 150, 250, 350]
        self.assertEqual(regions(abs_chapters, times), [])


class BuildChaptersTest(unittest.TestCase):
    def test_replaces_a_region_with_the_found_chapters(self):
        abs_chapters = chapters((0, 100), (100, 200))
        new = build_chapters(abs_chapters, [(0, 0)], [(0.3, "One"), (60.5, "Two")])
        self.assertEqual([(c["title"], c["start"], c["end"]) for c in new],
                         [("One", 0, 60.0), ("Two", 60.0, 100), ("Slice 2", 100, 200)])

    def test_keeps_the_chapter_in_progress_before_the_first_found_start(self):
        abs_chapters = chapters((0, 100), (100, 200))
        new = build_chapters(abs_chapters, [(1, 1)], [(140.5, "Three"), (170.5, "Four")], {1: "Two (continued)"})
        self.assertEqual([c["title"] for c in new], ["Slice 1", "Two (continued)", "Three", "Four"])

    def test_leaves_a_region_unchanged_when_nothing_was_found(self):
        abs_chapters = chapters((0, 100), (100, 200))
        new = build_chapters(abs_chapters, [(0, 1)], [])
        self.assertEqual([c["title"] for c in new], ["Slice 1", "Slice 2"])


class ProbeTest(unittest.TestCase):
    def test_interpolates_between_the_nearest_known_points(self):
        self.assertEqual(estimate([(0, 0.0), (1000, 500.0), (2000, 600.0)], 1500), (550.0, 500.0, 600.0))

    def test_widens_the_window_with_distance_from_the_last_known_point(self):
        self.assertGreater(half_window(3000.0, 0.0), half_window(300.0, 0.0))

    def test_accepts_a_well_scored_start_away_from_the_window_edges(self):
        words = [{"start": 40.0, "score": -0.1}] * 5
        self.assertEqual(judge(words, 1000.0, 1120.0, -1.0), (True, 1040.0, -0.1))

    def test_rejects_a_start_pressed_against_the_window_edge(self):
        words = [{"start": 0.5, "score": -0.1}] * 5
        self.assertFalse(judge(words, 1000.0, 1120.0, -1.0)[0])

    def test_rejects_a_poorly_scored_match(self):
        words = [{"start": 40.0, "score": -3.0}] * 5
        self.assertFalse(judge(words, 1000.0, 1120.0, -1.0)[0])


if __name__ == "__main__":
    unittest.main()
