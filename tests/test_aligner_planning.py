import unittest

from concordance.aligner import (alnum_count, drop_untokenizable, estimate_peak_mb,
                                 next_piece, restore_untokenizable, viterbi_mb)


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


class UntokenizableWordTest(unittest.TestCase):
    """A bare numeral romanizes to nothing, and that used to poison the alignment.

    `get_spans` walks the token list positionally, so an empty token is handed a
    span starting at frame 0. A chapter opening usually begins with its number,
    so the first word came back at the start of whatever audio was searched -
    which read as "the chapter starts here" no matter where it really was.
    """

    TOKENS = ["<star>", "", "a n n i e", "l a r d e r", "<star>"]
    TEXTS = ["<star>", "17", "Annie's", "larder", "<star>"]
    ALIGNED = [{"start": 303.2, "end": 303.6, "text": "Annie's", "score": -0.09},
               {"start": 303.6, "end": 304.1, "text": "larder", "score": -0.05}]

    def test_removes_a_word_the_romanizer_emptied(self):
        tokens, _, _ = drop_untokenizable(self.TOKENS, self.TEXTS)
        self.assertNotIn("", tokens)

    def test_keeps_the_star_tokens(self):
        tokens, _, _ = drop_untokenizable(self.TOKENS, self.TEXTS)
        self.assertEqual(tokens.count("<star>"), 2)

    def test_returns_one_entry_per_source_word(self):
        """build_entry pairs output with the text word by word and refuses a mismatch."""
        _, _, kept = drop_untokenizable(self.TOKENS, self.TEXTS)
        out = restore_untokenizable(self.ALIGNED, self.TEXTS, kept)
        self.assertEqual([w["text"] for w in out], ["17", "Annie's", "larder"])

    def test_places_a_restored_word_where_narration_actually_begins(self):
        _, _, kept = drop_untokenizable(self.TOKENS, self.TEXTS)
        out = restore_untokenizable(self.ALIGNED, self.TEXTS, kept)
        self.assertEqual(out[0]["start"], 303.2)

    def test_gives_a_restored_word_no_duration_of_its_own(self):
        _, _, kept = drop_untokenizable(self.TOKENS, self.TEXTS)
        out = restore_untokenizable(self.ALIGNED, self.TEXTS, kept)
        self.assertEqual(out[0]["start"], out[0]["end"])

    def test_leaves_the_mean_score_undisturbed(self):
        """A restored word borrows its neighbour's score rather than inventing one."""
        _, _, kept = drop_untokenizable(self.TOKENS, self.TEXTS)
        out = restore_untokenizable(self.ALIGNED, self.TEXTS, kept)
        self.assertEqual(out[0]["score"], self.ALIGNED[0]["score"])

    def test_restores_a_trailing_word_after_the_last_aligned_one(self):
        tokens = ["<star>", "e n d", "", "<star>"]
        texts = ["<star>", "end", "42", "<star>"]
        aligned = [{"start": 10.0, "end": 11.0, "text": "end", "score": -0.1}]
        _, _, kept = drop_untokenizable(tokens, texts)
        out = restore_untokenizable(aligned, texts, kept)
        self.assertEqual((out[-1]["text"], out[-1]["start"]), ("42", 11.0))


if __name__ == "__main__":
    unittest.main()


class ChunkedWindowEndTest(unittest.TestCase):
    """The group `end` comes from the coarse anchor grid and can land early.

    These drive `align_chunked` with stubs for the model, audio and alignment,
    and a text whose last word always lands flush against the window end, which
    is the signature of a window that stopped before the narration did.
    """

    TEXT = " ".join(f"word{i}" for i in range(12))

    def _run(self, end_slack, budget_mb=1e9):
        from concordance import aligner

        windows = []

        def fake_decode(manifest, w_start, w_end):
            windows.append((w_start, w_end))
            return w_end - w_start

        def fake_emissions(model, wav, slice_seconds=0, batch_size=1):
            return wav, 0.02

        def fake_align(emissions, stride, tokenizer, piece):
            # Every word crammed into the window, the last one flush at the wall.
            n = len(piece.split())
            span = emissions / max(1, n)
            return [{"text": w, "start": i * span, "end": (i + 1) * span, "score": -0.1}
                    for i, w in enumerate(piece.split())]

        originals = (aligner.decode_audio, aligner.sliced_emissions, aligner.align_text)
        aligner.decode_audio, aligner.sliced_emissions, aligner.align_text = (
            fake_decode, fake_emissions, fake_align)
        try:
            stats = {}
            aligner.align_chunked(None, None, [], self.TEXT, 0.0, 1000.0,
                                  budget_mb, 400.0, 100.0, 1, stats, end_slack)
            return windows, stats
        finally:
            aligner.decode_audio, aligner.sliced_emissions, aligner.align_text = originals

    def test_seeks_past_the_group_end_when_the_last_words_hit_the_window_wall(self):
        windows, _ = self._run(end_slack=600.0)
        self.assertGreater(max(w_end for _, w_end in windows), 1000.0)

    def test_never_seeks_further_than_the_slack_allows(self):
        windows, _ = self._run(end_slack=600.0)
        self.assertLessEqual(max(w_end for _, w_end in windows), 1600.0)

    def test_keeps_the_group_end_as_a_hard_boundary_when_no_slack_is_given(self):
        windows, _ = self._run(end_slack=0.0)
        self.assertLessEqual(max(w_end for _, w_end in windows), 1000.0)

    def test_still_claims_the_whole_group_for_the_final_piece(self):
        # A book whose grid is accurate must behave as it did before the change:
        # the last piece reaches `end` even though its own estimate is shorter.
        windows, _ = self._run(end_slack=0.0)
        self.assertEqual(max(w_end for _, w_end in windows), 1000.0)
