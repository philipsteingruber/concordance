import os
import tempfile
import time
import unittest
from pathlib import Path

from concordance.cache import AlignmentCache, CacheError, Entry, GroupKey, build_entry

KEY = GroupKey(calibre_book_id=7, library_item_id="li", fmt="kepub",
               first_spine=4, last_spine=5, first_chapter=2, last_chapter=3)
ITEMS = [(4, "One two three."), (5, "Four five.")]
ALIGNER = {"name": "ctc-forced-aligner", "model": "mms-300m", "commit": "abc"}


def aligned(texts_and_times):
    return [{"text": t, "start": s, "end": e, "score": -0.1} for t, s, e in texts_and_times]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.book = base / "book.kepub"
        self.audio = base / "book.m4b"
        self.book.write_bytes(b"book")
        self.audio.write_bytes(b"audio")
        # Group text: "One two three. Four five."; words aligned from t=0 in the slice.
        self.words = aligned([("One", 0.0, 0.5), ("two", 1.0, 1.5), ("three.", 2.0, 3.0),
                              ("Four", 5.0, 5.5), ("five.", 6.0, 7.0)])
        self.entry = build_entry(KEY, ITEMS, self.words, audio_start=100.0, audio_end=110.0,
                                 book_path=self.book, audio_path=self.audio, aligner=ALIGNER)

    def tearDown(self):
        self.tmp.cleanup()


class BuildEntryTest(Fixture):
    def test_shifts_word_times_by_the_slice_start_to_absolute_book_time(self):
        self.assertEqual(self.entry.words[0][2], 100.0)

    def test_records_each_spine_item_start_within_the_group_text(self):
        self.assertEqual(self.entry.items, [(4, 0, 14), (5, 15, 10)])

    def test_rejects_aligner_output_with_a_different_word_count(self):
        with self.assertRaises(CacheError):
            build_entry(KEY, ITEMS, self.words[:-1], 100.0, 110.0, self.book, self.audio, ALIGNER)

    def test_rejects_aligner_output_whose_words_do_not_match_the_text(self):
        bad = aligned([("One", 0, 1), ("TWO", 1, 2), ("three.", 2, 3), ("Four", 5, 6), ("five.", 6, 7)])
        with self.assertRaises(CacheError):
            build_entry(KEY, ITEMS, bad, 100.0, 110.0, self.book, self.audio, ALIGNER)


class LookupTest(Fixture):
    def test_interpolates_a_time_inside_a_word(self):
        seconds, _ = self.entry.time_for(4, 10)       # "three." spans chars 8-14 over 102-103s
        self.assertAlmostEqual(seconds, 102.0 + 2 / 6)

    def test_snaps_a_position_between_words_to_the_next_word_start(self):
        seconds, _ = self.entry.time_for(4, 3)        # the space after "One"
        self.assertEqual(seconds, 101.0)

    def test_resolves_an_offset_in_the_second_spine_item_of_a_group(self):
        seconds, _ = self.entry.time_for(5, 0)        # "Four"
        self.assertEqual(seconds, 105.0)

    def test_maps_an_audio_time_back_to_its_spine_item_and_offset(self):
        spine, offset, _ = self.entry.position_for(106.2)   # during "five."
        self.assertEqual((spine, offset), (5, 5))

    def test_maps_a_time_before_the_first_word_to_the_group_start(self):
        spine, offset, _ = self.entry.position_for(50.0)
        self.assertEqual((spine, offset), (4, 0))

    def test_averages_word_scores_near_a_time_for_the_write_gate(self):
        self.assertAlmostEqual(self.entry.mean_score(103.0, window=5.0), -0.1)


class FreshnessTest(Fixture):
    def test_is_fresh_when_files_and_aligner_are_unchanged(self):
        self.assertTrue(self.entry.is_fresh(self.book, self.audio, ALIGNER))

    def test_is_stale_when_the_audio_file_changes(self):
        time.sleep(0.01)
        self.audio.write_bytes(b"re-encoded audio")
        self.assertFalse(self.entry.is_fresh(self.book, self.audio, ALIGNER))

    def test_is_fresh_when_the_same_file_is_seen_under_a_different_mount_path(self):
        from concordance.cache import FileFingerprint
        fp = FileFingerprint.of(self.audio)
        self.assertEqual(fp, FileFingerprint("/container/path/book.m4b", fp.size, fp.mtime_ns))

    def test_is_stale_when_the_aligner_version_changes(self):
        self.assertFalse(self.entry.is_fresh(self.book, self.audio, {**ALIGNER, "commit": "def"}))


class StorageTest(Fixture):
    def test_loads_back_an_entry_identical_to_the_one_saved(self):
        cache = AlignmentCache(Path(self.tmp.name) / "cache")
        cache.save(self.entry)
        self.assertEqual(cache.load(KEY).to_json(), self.entry.to_json())

    def test_treats_a_corrupt_entry_as_a_cache_miss(self):
        cache = AlignmentCache(Path(self.tmp.name) / "cache")
        path = cache.path(KEY)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not gzip")
        self.assertIsNone(cache.load(KEY))

    def test_leaves_no_temporary_files_after_saving(self):
        cache = AlignmentCache(Path(self.tmp.name) / "cache")
        cache.save(self.entry)
        self.assertEqual([p.name for p in cache.path(KEY).parent.iterdir()], [cache.path(KEY).name])


if __name__ == "__main__":
    unittest.main()
