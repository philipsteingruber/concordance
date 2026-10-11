import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from concordance.cache import GroupKey, build_entry
from concordance.cli import lookup_aligned

ALIGNER = {"name": "ctc-forced-aligner", "model": "mms-300m", "commit": "abc"}
MIN_SCORE = -1.0


def key(first_spine, last_spine, chapter):
    return GroupKey(calibre_book_id=7, library_item_id="li", fmt="kepub", first_spine=first_spine,
                    last_spine=last_spine, first_chapter=chapter, last_chapter=chapter)


def words(score):
    return [{"text": t, "start": s, "end": e, "score": score}
            for t, s, e in [("One", 0.0, 0.5), ("two", 1.0, 1.5), ("three.", 2.0, 3.0)]]


class LookupAlignedTest(unittest.TestCase):
    """An old, wrongly-aligned group left over from a re-key sorts first and covers
    the same spine item and audio as the good group that replaced it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        book, audio = base / "book.kepub", base / "book.m4b"
        book.write_bytes(b"book")
        audio.write_bytes(b"audio")
        items = [(90, "One two three.")]
        self.stale = build_entry(key(85, 95, 17), items, words(-3.4), 100.0, 110.0,
                                 book, audio, ALIGNER)
        self.good = build_entry(key(90, 92, 20), items, words(-0.2), 100.0, 110.0,
                                book, audio, ALIGNER)
        self.located = SimpleNamespace(fmt="kepub", fragment=90, offset=4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ebook_lookup_passes_over_a_low_scoring_group_to_a_good_one(self):
        ebook_time, _ = lookup_aligned([self.stale, self.good], self.located, None, MIN_SCORE)
        self.assertAlmostEqual(ebook_time, 101.0)

    def test_audio_lookup_passes_over_a_low_scoring_group_to_a_good_one(self):
        _, audio_fraction = lookup_aligned([self.stale, self.good], None, 101.2, MIN_SCORE)
        self.assertIsNotNone(audio_fraction)

    def test_returns_nothing_when_every_covering_group_scores_low(self):
        self.assertEqual(lookup_aligned([self.stale], self.located, 101.2, MIN_SCORE), (None, None))


if __name__ == "__main__":
    unittest.main()
