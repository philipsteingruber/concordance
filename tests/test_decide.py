import unittest

from concordance.absclient import AbsProgress, Chapter
from concordance.anchor import build_alignment
from concordance.calibre import SpineItem
from concordance.cwa import CwaProgress
from concordance.decide import SAME_CHAPTER_DEADBAND_SECONDS, _already_further, decide

# Three equal chapters of 1000 s, three equal spine items: a clean 1:1 book.
SPINE = [SpineItem(index=i, href=f"c{i}.xhtml", chars=20_000) for i in (1, 2, 3)]
CHAPTERS = [Chapter(index=i, title=f"{i}", start=(i - 1) * 1000.0, end=i * 1000.0) for i in (1, 2, 3)]
ALIGNMENT = build_alignment(SPINE, CHAPTERS)
DURATION = 3000.0


def ebook(spine_index: int, percentage: float) -> CwaProgress:
    return CwaProgress(calibre_book_id=1, title="", authors=[], percentage=percentage,
                       xpointer=f"/body/DocFragment[{spine_index}]/body/p/text().0", last_modified=None)


def audio(seconds: float, finished: bool = False) -> AbsProgress:
    return AbsProgress("item", seconds, DURATION, seconds / DURATION, finished, None)


class InterpolationTest(unittest.TestCase):
    def test_places_a_resolved_ebook_position_proportionally_inside_its_chapter(self):
        d = decide(ebook(2, 45.0), None, ALIGNMENT, rewind_seconds=0, book_duration=DURATION,
                   item_fraction=0.5)
        self.assertAlmostEqual(d.abs_seconds, 1500.0)

    def test_reports_the_interpolated_tier_when_the_xpointer_resolved(self):
        d = decide(ebook(2, 45.0), None, ALIGNMENT, 0, DURATION, item_fraction=0.5)
        self.assertEqual(d.tier, "interpolated")

    def test_falls_back_to_chapter_start_without_a_resolved_offset(self):
        d = decide(ebook(2, 45.0), None, ALIGNMENT, 0, DURATION, item_fraction=None)
        self.assertEqual((d.tier, d.abs_seconds), ("anchor", 1000.0))

    def test_applies_the_rewind_to_an_interpolated_position(self):
        d = decide(ebook(2, 45.0), None, ALIGNMENT, rewind_seconds=150, book_duration=DURATION,
                   item_fraction=0.5)
        self.assertAlmostEqual(d.abs_seconds, 1350.0)


class SameChapterTest(unittest.TestCase):
    def test_treats_positions_within_the_deadband_as_in_sync(self):
        d = decide(ebook(2, 45.0), audio(1500.0 - SAME_CHAPTER_DEADBAND_SECONDS + 1), ALIGNMENT,
                   0, DURATION, item_fraction=0.5)
        self.assertEqual(d.direction, "in_sync")

    def test_writes_to_audio_when_the_ebook_is_ahead_within_the_chapter(self):
        d = decide(ebook(2, 45.0), audio(1100.0), ALIGNMENT, 0, DURATION, item_fraction=0.5)
        self.assertEqual(d.direction, "to_abs")

    def test_writes_to_the_ebook_when_the_audio_is_ahead_within_the_chapter(self):
        d = decide(ebook(2, 34.0), audio(1900.0), ALIGNMENT, 0, DURATION, item_fraction=0.05)
        self.assertEqual(d.direction, "to_cwa")

    def test_keeps_same_chapter_positions_in_sync_when_the_offset_is_unknown(self):
        d = decide(ebook(2, 45.0), audio(1900.0), ALIGNMENT, 0, DURATION, item_fraction=None)
        self.assertEqual(d.direction, "in_sync")


class AudioLeadsTest(unittest.TestCase):
    def test_labels_an_anchored_audio_position_as_interpolated(self):
        d = decide(None, audio(2250.0), ALIGNMENT, 0, DURATION)
        self.assertEqual(d.tier, "interpolated")

    def test_carries_the_fraction_through_the_spine_item_for_an_xpointer_write(self):
        d = decide(None, audio(2250.0), ALIGNMENT, 0, DURATION)
        self.assertEqual((d.cwa_spine_index, round(d.cwa_item_fraction, 3)), (3, 0.25))

    def test_never_writes_an_ebook_percentage_below_the_current_one(self):
        d = decide(ebook(3, 95.0), audio(2100.0), ALIGNMENT, 0, DURATION, item_fraction=0.9)
        self.assertNotEqual(d.direction, "to_cwa")


class AlignedTest(unittest.TestCase):
    def test_uses_the_aligned_time_instead_of_the_proportional_estimate(self):
        d = decide(ebook(2, 45.0), None, ALIGNMENT, 0, DURATION, item_fraction=0.5,
                   aligned_ebook_time=1432.0)
        self.assertEqual((d.tier, d.abs_seconds), ("aligned", 1432.0))

    def test_writes_the_aligned_fraction_to_the_ebook(self):
        d = decide(None, audio(2250.0), ALIGNMENT, 0, DURATION, aligned_audio_fraction=(3, 0.3))
        self.assertEqual((d.tier, d.cwa_item_fraction), ("aligned", 0.3))

    def test_treats_aligned_positions_within_twenty_seconds_as_in_sync(self):
        d = decide(ebook(2, 45.0), audio(1440.0), ALIGNMENT, 0, DURATION, item_fraction=0.5,
                   aligned_ebook_time=1432.0)
        self.assertEqual(d.direction, "in_sync")

    def test_rewinds_an_aligned_write_toward_audio(self):
        d = decide(ebook(2, 45.0), None, ALIGNMENT, 150, DURATION, aligned_ebook_time=1432.0)
        self.assertEqual(d.abs_seconds, 1282.0)


class FinishedTest(unittest.TestCase):
    def test_syncs_a_finished_ebook_as_finished_audio(self):
        d = decide(ebook(3, 99.5), None, ALIGNMENT, 150, DURATION)
        self.assertTrue(d.abs_finished)

    def test_reports_in_sync_when_both_sides_are_finished(self):
        d = decide(ebook(3, 100.0), audio(DURATION, finished=True), ALIGNMENT, 150, DURATION)
        self.assertEqual(d.direction, "in_sync")


class RepeatedWriteTest(unittest.TestCase):
    """A position Concordance already wrote must not be written again next sync.

    Neither side stores back exactly what was sent - ABS rounds seconds to one
    decimal - so comparing a freshly computed position against the stored one
    for strict inequality reads as "still ahead" forever.
    """

    def test_stops_rewriting_an_audio_position_after_abs_rounded_it(self):
        stored = decide(ebook(3, 68.0), audio(1500.0), ALIGNMENT, rewind_seconds=150,
                        book_duration=DURATION, item_fraction=0.12341)
        self.assertEqual(stored.direction, "to_abs")
        again = decide(ebook(3, 68.0), audio(round(stored.abs_seconds, 1)), ALIGNMENT,
                       rewind_seconds=150, book_duration=DURATION, item_fraction=0.12341)
        self.assertEqual(again.direction, "in_sync")

    def test_still_writes_when_the_ebook_has_moved_a_real_distance_ahead(self):
        d = decide(ebook(3, 68.0), audio(1973.4), ALIGNMENT, rewind_seconds=150,
                   book_duration=DURATION, item_fraction=0.3)
        self.assertEqual(d.direction, "to_abs")


class FirstProgressFloorTest(unittest.TestCase):
    """A side stored at exactly 0 is not "no progress" for floor purposes.

    Early in a book the rewind clamps the translated ebook position to 0. The
    audiobook then stores 0, which reads as absent, so the branch for "only the
    ebook has progress" ran without a floor and rewrote 0 on every sync.
    """

    def test_does_not_write_a_zero_audio_position_the_rewind_clamped(self):
        d = decide(ebook(1, 0.7), audio(0.0), ALIGNMENT, rewind_seconds=150,
                   book_duration=DURATION, item_fraction=0.02)
        self.assertEqual(d.direction, "in_sync")

    def test_writes_once_the_ebook_moves_past_the_rewind(self):
        d = decide(ebook(1, 17.0), audio(0.0), ALIGNMENT, rewind_seconds=150,
                   book_duration=DURATION, item_fraction=0.5)
        self.assertEqual(d.direction, "to_abs")

    def test_still_writes_to_a_fresh_audiobook_with_no_progress_record(self):
        d = decide(ebook(2, 45.0), None, ALIGNMENT, rewind_seconds=150,
                   book_duration=DURATION, item_fraction=0.5)
        self.assertEqual(d.direction, "to_abs")

    def test_stops_rewriting_an_ebook_position_that_translates_below_the_margin(self):
        again = decide(ebook(1, 0.0), audio(5.0), ALIGNMENT, rewind_seconds=0,
                       book_duration=DURATION, item_fraction=0.0)
        self.assertEqual(again.direction, "in_sync")

    def test_still_writes_to_an_ebook_sitting_at_zero_when_the_audio_is_well_ahead(self):
        d = decide(ebook(1, 0.0), audio(1900.0), ALIGNMENT, rewind_seconds=0,
                   book_duration=DURATION, item_fraction=0.0)
        self.assertEqual(d.direction, "to_cwa")


class AlreadyFurtherTest(unittest.TestCase):
    """The ebook-side guard behind the CWA floor.

    A CWA percentage is written CWA_PERCENT_MARGIN points below the position it
    points at, so the percentage alone can never show that the ebook reached the
    point Concordance sent it to. The resolved spine and offset can.
    """

    def test_counts_a_later_spine_item_as_already_further(self):
        self.assertTrue(_already_further(2, 0.9, ebook_spine=3, ebook_fraction=0.0))

    def test_counts_the_same_offset_in_the_same_spine_item_as_already_further(self):
        self.assertTrue(_already_further(3, 0.25, ebook_spine=3, ebook_fraction=0.25))

    def test_does_not_count_an_earlier_offset_in_the_same_spine_item(self):
        self.assertFalse(_already_further(3, 0.6, ebook_spine=3, ebook_fraction=0.25))

    def test_decides_nothing_without_a_resolved_ebook_offset(self):
        self.assertFalse(_already_further(3, 0.25, ebook_spine=3, ebook_fraction=None))


if __name__ == "__main__":
    unittest.main()
