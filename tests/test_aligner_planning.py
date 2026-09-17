import unittest

from concordance.aligner import alnum_count, estimate_peak_mb, next_piece, viterbi_mb


class MemoryModelTest(unittest.TestCase):
    def test_reproduces_the_measured_peak_for_a_short_chapter_within_five_percent(self):
        self.assertAlmostEqual(estimate_peak_mb(358.2, 3628), 2438, delta=2438 * 0.05)

    def test_reproduces_the_measured_peak_for_a_forty_minute_chapter_within_ten_percent(self):
        # The safety factor on the Viterbi term makes this deliberately high, never low.
        estimate = estimate_peak_mb(2418.0, 25782)
        self.assertGreaterEqual(estimate, 3024)
        self.assertLess(estimate, 3024 * 1.10)

    def test_matches_the_measured_viterbi_backpointer_memory_within_five_percent(self):
        self.assertAlmostEqual(viterbi_mb(2418.0, 25782), 1178, delta=1178 * 0.05)

    def test_puts_a_seventy_five_minute_chapter_over_a_four_gigabyte_budget(self):
        self.assertGreater(estimate_peak_mb(75 * 60, int(25782 * 75 / 40.3)), 4000)

    def test_counts_only_letters_and_digits_as_aligner_tokens(self):
        self.assertEqual(alnum_count("It's 2 o'clock."), 10)


class NextPieceTest(unittest.TestCase):
    spans = [(0, 4), (5, 9), (10, 14), (15, 19), (20, 24)]    # five 4-char words

    def test_stops_before_the_word_that_would_exceed_the_target(self):
        # 1 s per char: words end at 4, 9, 14 s ... a 10 s target keeps the first two.
        self.assertEqual(next_piece(self.spans, 0, 1.0, 10.0), 2)

    def test_always_takes_at_least_one_word(self):
        self.assertEqual(next_piece(self.spans, 3, 1.0, 0.5), 4)

    def test_takes_all_remaining_words_when_they_fit(self):
        self.assertEqual(next_piece(self.spans, 1, 1.0, 1000.0), 5)

    def test_absorbs_a_leftover_shorter_than_a_quarter_of_the_target(self):
        # A 20 s target stops after the fourth word; the 4 s last word is under 5 s, so it joins.
        self.assertEqual(next_piece(self.spans, 0, 1.0, 20.0), 5)


if __name__ == "__main__":
    unittest.main()
