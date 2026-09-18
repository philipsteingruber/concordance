import types
import unittest
import unittest.mock
from pathlib import Path

from concordance import chapters as chapters_mod

from concordance.absclient import Chapter
from concordance.chapterdetect import (ChapterStart, _label_parts, _numbered_runs, book_positions,
                                       chapter_number, heading_blocks)
from concordance.chapterprobe import EDGE_SECONDS, estimate, half_window, judge, observed_rate
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

    def test_still_splits_a_chapter_far_longer_than_the_books_median(self):
        """After one split the ratio is near 1, but a leftover slice is still a slice."""
        abs_chapters = chapters((0, 100), (100, 200), (200, 300), (300, 1000))
        times = [10, 150, 250, 400, 600, 800]
        self.assertEqual(regions(abs_chapters, times), [(3, 3)])

    def test_leaves_a_long_chapter_alone_when_nothing_is_inside_it(self):
        abs_chapters = chapters((0, 100), (100, 200), (200, 300), (300, 1000))
        times = [10, 150, 250, 400]
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
    """The cache gate is checked at the looked-up position, not across the entry.

    An entry can align well on average and still be rubbish in one stretch, and a
    chapter start that lands in that stretch must not be trusted. Measured on a
    real book, moving this gate to an entry-wide mean admitted one extra start
    out of 55 and that one was wrong.
    """

    def entry(self, opening_score: float):
        from concordance.cache import Entry, FileFingerprint, GroupKey
        fp = FileFingerprint("x", 1, 2)
        words = [(i, i + 4, 100.0 + i, 100.5 + i, opening_score if i < 200 else -0.1)
                 for i in range(0, 400, 4)]
        return Entry(key=GroupKey(561, "item", "kepub", 14, 14, 6, 7), items=[(14, 0, 400)],
                     audio_start=100.0, audio_end=200.0, book_file=fp, audio_file=fp,
                     aligner={}, words=words)

    def resolve(self, entry, offset):
        cfg = types.SimpleNamespace(abs_audio_root=None, min_align_score=-1.0)
        book = types.SimpleNamespace(
            calibre_id=561, item_id="item", fmt="kepub", book_file=Path("x"),
            manifest=[("a.m4b", 200.0)], positions={14: 1000, -1: 1400},
            starts=[ChapterStart(14, offset, "Chapter 15", "heading")])
        with unittest.mock.patch("concordance.chapters._fresh_entries", return_value=[entry]):
            return chapters_mod.cached_times(cfg, book)

    def test_refuses_a_start_inside_a_badly_aligned_stretch(self):
        self.assertEqual(self.resolve(self.entry(opening_score=-9.0), offset=0), {})

    def test_accepts_a_start_in_a_well_aligned_stretch_of_the_same_entry(self):
        self.assertIn(0, self.resolve(self.entry(opening_score=-9.0), offset=380))

    def test_accepts_a_start_when_the_whole_entry_aligned_well(self):
        self.assertIn(0, self.resolve(self.entry(opening_score=-0.1), offset=0))


class ChapterScoreGateTest(unittest.TestCase):
    """A confirmation is an anchor, so it is held to a stricter score than a write.

    On Misery the 21 correct confirmations scored between -0.01 and -0.23 and the
    two wrong ones -0.92 and -0.93. The wrong pair cleared the -1.0 write gate,
    placed their chapters about 85 s early, and every later estimate interpolated
    from them.
    """

    GOOD = [-0.01, -0.03, -0.05, -0.16, -0.23]
    BAD = [-0.92, -0.93]

    def test_is_stricter_than_the_write_gate(self):
        self.assertGreater(chapters_mod.CHAPTER_MIN_SCORE, -1.0)

    def test_accepts_every_score_a_correct_confirmation_produced(self):
        self.assertTrue(all(s >= chapters_mod.CHAPTER_MIN_SCORE for s in self.GOOD))

    def test_rejects_the_scores_the_wrong_confirmations_produced(self):
        self.assertTrue(all(s < chapters_mod.CHAPTER_MIN_SCORE for s in self.BAD))

    def test_refuses_a_cached_time_that_only_clears_the_write_gate(self):
        from concordance.cache import Entry, FileFingerprint, GroupKey
        fp = FileFingerprint("x", 1, 2)
        words = [(i, i + 4, 100.0 + i, 100.5 + i, -0.93) for i in range(0, 400, 4)]
        entry = Entry(key=GroupKey(561, "item", "kepub", 14, 14, 6, 7), items=[(14, 0, 400)],
                      audio_start=100.0, audio_end=200.0, book_file=fp, audio_file=fp,
                      aligner={}, words=words)
        cfg = types.SimpleNamespace(abs_audio_root=None, min_align_score=-1.0)
        book = types.SimpleNamespace(
            calibre_id=561, item_id="item", fmt="kepub", book_file=Path("x"),
            manifest=[("a.m4b", 200.0)], positions={14: 1000, -1: 1400},
            starts=[ChapterStart(14, 0, "Chapter 15", "heading")])
        with unittest.mock.patch("concordance.chapters._fresh_entries", return_value=[entry]):
            self.assertEqual(chapters_mod.cached_times(cfg, book), {})


class EntryAnchorTest(unittest.TestCase):
    """Both ends of an aligned spine item must be anchors.

    Audio between two items can carry a part announcement with no book text at
    all, and interpolating a character-to-time rate across it put one chapter's
    estimate ~390 s early, outside any window the probe would search.
    """

    def anchors(self, opening_score=-0.1):
        from concordance.cache import Entry, FileFingerprint, GroupKey
        fp = FileFingerprint("x", 1, 2)
        # Item 14 is narrated from 900 s; item 13 ends at 500 s. The 400 s between
        # them is announcement, and holds no characters.
        words = [(i, i + 4, 900.0 + i, 900.5 + i, opening_score if i < 200 else -0.1)
                 for i in range(0, 400, 4)]
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

    def test_refuses_an_end_whose_alignment_is_not_trustworthy(self):
        """cached_times won't report that position, so it must not be an anchor either."""
        kept = self.anchors(opening_score=-9.0)
        self.assertNotIn(1000, [pos for pos, _ in kept])

    def test_keeps_the_sound_end_of_a_partly_bad_item(self):
        """Half an entry being wrong is no reason to throw away the half that isn't."""
        self.assertEqual(len(self.anchors(opening_score=-9.0)), 1)

    def test_pins_the_items_first_character_to_when_narration_starts(self):
        self.assertIn((1000, 900.0), self.anchors())

    def test_keeps_an_estimate_at_the_item_start_out_of_the_silent_gap(self):
        from concordance.chapterprobe import estimate
        with_bounds = sorted([(600, 100.0), (1400, 1400.0)] + self.anchors())
        without = sorted([(600, 100.0), (1400, 1400.0)])
        self.assertAlmostEqual(estimate(with_bounds, 1000)[0], 900.0)
        self.assertGreater(900.0 - estimate(without, 1000)[0], 100.0)


class BookPositionTest(unittest.TestCase):
    """Short items between the content ones are narrated, and must be counted.

    A part divider is a few hundred characters and about a minute and a half of
    narration. Leaving Misery's "PART FOUR GODDESS" epigraph out of the map put
    all of Part Four ~160 s early, past the reach of a doubled search window.
    """

    def docs(self, *sizes):
        from concordance.xpointer import parse_document
        return {i: parse_document(f"<html><body><p>{'x' * n}</p></body></html>".encode())
                for i, n in enumerate(sizes, start=1)}

    def test_counts_a_short_item_between_two_substantial_ones(self):
        pos = chapters_mod.book_positions(self.docs(5000, 800, 5000))
        self.assertIn(2, pos)

    def test_leaves_a_gap_the_size_of_that_item_before_the_next(self):
        pos = chapters_mod.book_positions(self.docs(5000, 800, 5000))
        self.assertGreater(pos[3] - pos[1], 5000 + 800)

    def test_drops_short_items_before_the_first_substantial_one(self):
        """Front matter is short and usually unread, and sits where dropping it is free."""
        pos = chapters_mod.book_positions(self.docs(300, 5000, 5000))
        self.assertNotIn(1, pos)

    def test_drops_short_items_after_the_last_substantial_one(self):
        pos = chapters_mod.book_positions(self.docs(5000, 5000, 300))
        self.assertNotIn(3, pos)

    def test_reports_a_total_even_with_nothing_substantial(self):
        self.assertEqual(chapters_mod.book_positions(self.docs(10, 20)), {-1: 0})


class DropCrowdedTest(unittest.TestCase):
    """Starts a few hundred characters apart are numbered switches, not chapters."""

    def starts(self, *offsets):
        return [ChapterStart(1, o, str(o), "heading") for o in offsets]

    def drop(self, *offsets):
        """A book of 10 characters per second, so the 60 s floor is 600 characters."""
        return chapters_mod.drop_crowded(self.starts(*offsets), {1: 0},
                                         total_chars=36_000, duration=3_600.0)

    def test_drops_a_start_too_close_to_the_one_before_it(self):
        self.assertEqual([s.offset for s in self.drop(0, 353, 1721)], [0, 1721])

    def test_measures_the_gap_from_the_last_kept_start_not_the_last_seen(self):
        """Otherwise a slow drip of sub-threshold steps would all survive."""
        self.assertEqual([s.offset for s in self.drop(0, 400, 800)], [0, 800])

    def test_keeps_starts_that_are_a_real_chapter_apart(self):
        self.assertEqual(len(self.drop(0, 20_000)), 2)

    def test_converts_the_floor_with_the_books_own_reading_rate(self):
        """700 characters is a minute of slow narration but seconds of fast narration.

        The floor is a duration, so the same character gap survives in the book
        read slowly and is dropped in the book read quickly.
        """
        slow = chapters_mod.drop_crowded(self.starts(0, 700), {1: 0},
                                         total_chars=36_000, duration=3_600.0)   # 10 chars/s
        fast = chapters_mod.drop_crowded(self.starts(0, 700), {1: 0},
                                         total_chars=360_000, duration=3_600.0)  # 100 chars/s
        self.assertEqual((len(slow), len(fast)), (2, 1))


class ClampSlackTest(unittest.TestCase):
    """A chapter may begin a second or two after the previous known point.

    The window is clamped to the known points, and the edge test then throws out
    anything landing within three seconds of one. Misery's Part Two chapter 15
    begins 2.4 s after the previous spine item's narration ends, so it was refused
    for being exactly where it belongs.
    """

    def test_stands_the_lower_clamp_back_from_the_known_point(self):
        from concordance.chapterprobe import CLAMP_SLACK
        self.assertGreater(CLAMP_SLACK, EDGE_SECONDS)

    def test_accepts_a_start_just_after_the_previous_known_point(self):
        words = [{"start": 17.4, "score": -0.03}] * 5
        lo = 1000.0 - 15.0
        self.assertTrue(judge(words, lo, lo + 120.0, -0.5)[0])


class TailEstimateTest(unittest.TestCase):
    """Past the last measured anchor, extrapolate; don't pin to a fictional endpoint.

    The end of the audio used to be an anchor, which asserts the ebook's last
    character is spoken at the last second. Misery ends with a preview of another
    novel - 3.3% of the text, about twenty minutes of narration that does not
    exist - so every estimate after the final confirmed chapter was dragged early
    enough that none could be found.
    """

    ANCHORS = [(0, 0.0), (1000, 100.0), (2000, 200.0), (3000, 300.0)]

    def test_extrapolates_past_the_last_anchor_at_the_measured_rate(self):
        self.assertAlmostEqual(estimate(self.ANCHORS, 4000, rate=10.0, limit=9999.0)[0], 400.0)

    def test_does_not_pin_a_later_position_to_the_last_known_time(self):
        self.assertGreater(estimate(self.ANCHORS, 4000, rate=10.0, limit=9999.0)[0], 300.0)

    def test_never_estimates_past_the_end_of_the_audio(self):
        self.assertEqual(estimate(self.ANCHORS, 999_999, rate=10.0, limit=500.0)[0], 500.0)

    def test_still_interpolates_between_two_anchors(self):
        self.assertAlmostEqual(estimate(self.ANCHORS, 1500, rate=10.0, limit=9999.0)[0], 150.0)

    def test_takes_the_median_gap_rather_than_the_most_recent_one(self):
        """One slow gap at the end would otherwise skew the whole tail."""
        anchors = [(0, 0.0), (1000, 100.0), (2000, 200.0), (2400, 300.0)]
        self.assertAlmostEqual(observed_rate(anchors, 99.0), 10.0)

    def test_falls_back_when_no_pair_of_anchors_can_give_a_rate(self):
        self.assertEqual(observed_rate([(0, 0.0)], 14.4), 14.4)


class ProbeTest(unittest.TestCase):
    def test_interpolates_between_the_nearest_known_points(self):
        self.assertEqual(estimate([(0, 0.0), (1000, 500.0), (2000, 600.0)], 1500), (550.0, 500.0, 600.0))

    def test_widens_the_window_with_distance_from_the_last_known_point(self):
        self.assertGreater(half_window(3000.0, 0.0), half_window(300.0, 0.0))

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
