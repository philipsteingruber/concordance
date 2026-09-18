import types
import unittest
import unittest.mock
from pathlib import Path

from concordance import chapters as chapters_mod

from concordance.absclient import Chapter
from concordance.chapterdetect import ChapterStart, _label_parts, _numbered_runs, chapter_number, heading_blocks
from concordance.chapterprobe import MIN_HALF_WINDOW, estimate, half_window, judge
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


class RenumberCacheTest(unittest.TestCase):
    """The rename that keeps an aligned group valid after its chapters are renumbered."""

    def setUp(self):
        import tempfile
        from concordance.cache import AlignmentCache, Entry, FileFingerprint, GroupKey
        fingerprint = FileFingerprint("x", 1, 2)
        self.dir = tempfile.TemporaryDirectory()
        self.cache = AlignmentCache(Path(self.dir.name))
        self.key = GroupKey(561, "item", "kepub", 10, 10, 1, 3)
        self.entry = Entry(key=self.key, items=[(10, 0, 100)], audio_start=0.0, audio_end=13000.0,
                           book_file=fingerprint, audio_file=fingerprint, aligner={}, words=[(0, 4, 1.0, 1.5, -0.1)])
        self.cache.save(self.entry)

    def tearDown(self):
        self.dir.cleanup()

    def renumber(self, new_chapters):
        book = types.SimpleNamespace(calibre_id=561, item_id="item")
        with unittest.mock.patch("concordance.chapters.AlignmentCache", return_value=self.cache):
            return chapters_mod.renumber_cache(None, book, new_chapters)

    def test_renames_an_entry_to_the_new_chapter_numbers(self):
        new = [{"title": f"Chapter {i}", "start": i * 500.0, "end": (i + 1) * 500.0} for i in range(30)]
        renamed = self.renumber(new)
        self.assertEqual(renamed, ["561/kepub-sp10-10-ch1-3.json.gz -> 561/kepub-sp10-10-ch1-26.json.gz"])
        self.assertTrue((Path(self.dir.name) / "561/kepub-sp10-10-ch1-26.json.gz").exists())
        self.assertFalse((Path(self.dir.name) / "561/kepub-sp10-10-ch1-3.json.gz").exists())

    def test_leaves_an_entry_alone_when_its_numbers_do_not_change(self):
        new = [{"title": "a", "start": 0.0, "end": 6000.0}, {"title": "b", "start": 6000.0, "end": 9000.0},
               {"title": "c", "start": 9000.0, "end": 14000.0}]
        self.assertEqual(self.renumber(new), [])
        self.assertTrue((Path(self.dir.name) / "561/kepub-sp10-10-ch1-3.json.gz").exists())


class CacheLookupTest(unittest.TestCase):
    """Regression tests for a propose run that re-aligned audio already in the cache.

    The lookup gated on the mean word score in a window around the looked-up
    time. That window is one-sided at an entry's first word, and a chapter
    opening the narrator doesn't read scores terribly, so a start at offset 0
    was rejected while the entry it came from had aligned perfectly well.
    """

    def entry(self, opening_score: float):
        from concordance.cache import Entry, FileFingerprint, GroupKey
        fp = FileFingerprint("x", 1, 2)
        words = [(i, i + 4, 100.0 + i, 100.5 + i, opening_score if i < 20 else -0.1)
                 for i in range(0, 400, 4)]
        return Entry(key=GroupKey(561, "item", "kepub", 14, 14, 6, 7), items=[(14, 0, 400)],
                     audio_start=100.0, audio_end=200.0, book_file=fp, audio_file=fp,
                     aligner={}, words=words)

    def book(self):
        return types.SimpleNamespace(
            calibre_id=561, item_id="item", fmt="kepub", book_file=Path("x"),
            manifest=[("a.m4b", 200.0)], positions={14: 1000, -1: 1400},
            starts=[ChapterStart(14, 0, "Chapter 15", "heading")])

    def resolve(self, entry):
        cfg = types.SimpleNamespace(abs_audio_root=None, min_align_score=-1.0)
        with unittest.mock.patch("concordance.chapters._fresh_entries", return_value=[entry]):
            return chapters_mod.cached_times(cfg, self.book())

    def test_resolves_a_chapter_starting_at_the_first_word_of_an_entry(self):
        bad_opening = self.entry(opening_score=-9.0)
        self.assertIn(0, self.resolve(bad_opening))

    def test_still_refuses_an_entry_that_did_not_align(self):
        with unittest.mock.patch.object(chapters_mod, "_fresh_entries"):
            self.assertEqual(self.resolve(self.entry(opening_score=-40.0)), {})

    def test_scores_the_whole_entry_rather_than_the_looked_up_position(self):
        e = self.entry(opening_score=-9.0)
        self.assertLess(e.mean_score(e.words[0][2]), -1.0)
        self.assertGreater(e.overall_score(), -1.0)


class EntryAnchorTest(unittest.TestCase):
    """Both ends of an aligned spine item must be anchors.

    Audio between two items can carry a part announcement with no book text at
    all, and interpolating a character-to-time rate across it put one chapter's
    estimate ~390 s early, outside any window the probe would search.
    """

    def anchors(self):
        from concordance.cache import Entry, FileFingerprint, GroupKey
        fp = FileFingerprint("x", 1, 2)
        # Item 14 is narrated from 900 s; item 13 ends at 500 s. The 400 s between
        # them is announcement, and holds no characters.
        words = [(i, i + 4, 900.0 + i, 900.5 + i, -0.1) for i in range(0, 400, 4)]
        entry = Entry(key=GroupKey(561, "item", "kepub", 14, 14, 6, 7), items=[(14, 0, 400)],
                      audio_start=900.0, audio_end=1300.0, book_file=fp, audio_file=fp,
                      aligner={}, words=words)
        book = types.SimpleNamespace(calibre_id=561, item_id="item", fmt="kepub",
                                     book_file=Path("x"), manifest=[("a.m4b", 1400.0)],
                                     positions={13: 600, 14: 1000, -1: 1400}, starts=[])
        cfg = types.SimpleNamespace(abs_audio_root=None, min_align_score=-1.0)
        with unittest.mock.patch("concordance.chapters._fresh_entries", return_value=[entry]):
            return chapters_mod.entry_anchors(cfg, book)

    def test_anchors_both_ends_of_an_aligned_item(self):
        self.assertEqual(len(self.anchors()), 2)

    def test_pins_the_items_first_character_to_when_narration_starts(self):
        self.assertIn((1000, 900.0), self.anchors())

    def test_keeps_an_estimate_at_the_item_start_out_of_the_silent_gap(self):
        from concordance.chapterprobe import estimate
        with_bounds = sorted([(600, 100.0), (1400, 1400.0)] + self.anchors())
        without = sorted([(600, 100.0), (1400, 1400.0)])
        self.assertAlmostEqual(estimate(with_bounds, 1000)[0], 900.0)
        self.assertGreater(900.0 - estimate(without, 1000)[0], 100.0)


class DropCrowdedTest(unittest.TestCase):
    """Starts a few hundred characters apart are numbered switches, not chapters."""

    def starts(self, *offsets):
        return [ChapterStart(1, o, str(o), "heading") for o in offsets]

    def test_drops_a_start_too_close_to_the_one_before_it(self):
        kept = chapters_mod.drop_crowded(self.starts(0, 353, 1721), {1: 0})
        self.assertEqual([s.offset for s in kept], [0, 1721])

    def test_measures_the_gap_from_the_last_kept_start_not_the_last_seen(self):
        """Otherwise a slow drip of sub-threshold steps would all survive."""
        kept = chapters_mod.drop_crowded(self.starts(0, 1000, 2000), {1: 0})
        self.assertEqual([s.offset for s in kept], [0, 2000])

    def test_keeps_starts_that_are_a_real_chapter_apart(self):
        kept = chapters_mod.drop_crowded(self.starts(0, 20_000), {1: 0})
        self.assertEqual(len(kept), 2)


class ProbeTest(unittest.TestCase):
    def test_interpolates_between_the_nearest_known_points(self):
        self.assertEqual(estimate([(0, 0.0), (1000, 500.0), (2000, 600.0)], 1500), (550.0, 500.0, 600.0))

    def test_widens_the_window_for_a_longer_gap_between_known_points(self):
        self.assertGreater(half_window(0.0, 20_000.0), half_window(0.0, 2_000.0))

    def test_sizes_the_window_from_the_gap_not_from_the_estimate_within_it(self):
        self.assertGreater(half_window(0.0, 10_000.0), MIN_HALF_WINDOW)

    def test_accepts_a_well_scored_start_away_from_the_window_edges(self):
        words = [{"start": 40.0, "score": -0.1}] * 5
        self.assertEqual(judge(words, 1000.0, 1120.0, -1.0)[:3], (True, 1040.0, -0.1))

    def test_rejects_a_start_pressed_against_the_window_edge(self):
        words = [{"start": 0.5, "score": -0.1}] * 5
        self.assertFalse(judge(words, 1000.0, 1120.0, -1.0)[0])

    def test_rejects_a_well_scored_match_pinned_to_the_span_it_was_given(self):
        """A good score says the words match where they were put, not that they belong there."""
        words = [{"start": 0.1, "score": -0.02}] * 5
        ok, _, _, why = judge(words, 1000.0, 1500.0, -1.0)
        self.assertEqual((ok, why), (False, "pinned to the start of the search span"))

    def test_rejects_a_start_at_or_before_the_previous_chapter(self):
        words = [{"start": 40.0, "score": -0.1}] * 5
        ok, _, _, why = judge(words, 1000.0, 1120.0, -1.0, floor=1040.0)
        self.assertEqual((ok, why), (False, "at or before the previous chapter"))

    def test_rejects_a_poorly_scored_match(self):
        words = [{"start": 40.0, "score": -3.0}] * 5
        self.assertFalse(judge(words, 1000.0, 1120.0, -1.0)[0])


if __name__ == "__main__":
    unittest.main()
