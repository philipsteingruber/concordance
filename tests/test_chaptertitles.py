import unittest

from concordance.absclient import Chapter
from concordance.chaptertitles import (CREDITS_MAX_SECONDS, END_CREDITS, MATCH_LEAD_IN_SECONDS,
                                       NEAR_MISS_SCORE, OPENING_CREDITS, candidates, display_title,
                                       fill_gaps, is_generic, match_starts, merge_repeats,
                                       renamed_titles)


def named(*spans):
    """ABS chapters named like a release's files: 001, 002, ..."""
    return [Chapter(i, f"{i:03}", a, b) for i, (a, b) in enumerate(spans, start=1)]


class IsGenericTest(unittest.TestCase):
    def test_treats_numbered_file_names_as_generic(self):
        self.assertTrue(is_generic(["001", "002", "003"]))

    def test_treats_book_title_plus_number_as_generic(self):
        self.assertTrue(is_generic(["A Novel (Unabridged) - 001", "A Novel (Unabridged) - 002"]))

    def test_treats_names_carrying_timestamps_as_generic(self):
        self.assertTrue(is_generic(["Chapter 001  - 00:43:43", "Chapter 002  - 01:10:02"]))

    def test_does_not_treat_titled_chapter_lists_as_generic(self):
        self.assertFalse(is_generic(["Prologue", "Chapter 1: The Harbour", "Chapter 2: The Storm"]))

    def test_leaves_plain_chapter_numbers_alone_when_the_ebook_has_only_numbers(self):
        self.assertFalse(is_generic(["Chapter 1", "Chapter 2"], ["Chapter 1", "Chapter 2"]))

    def test_renames_plain_chapter_numbers_when_abs_has_more_chapters_than_the_ebook(self):
        self.assertTrue(is_generic(["Chapter 1", "Chapter 2", "Chapter 3"], ["Chapter 1", "Chapter 2"]))

    def test_leaves_plain_chapter_numbers_alone_when_the_ebook_has_no_titles(self):
        self.assertFalse(is_generic(["Chapter 1", "Chapter 2", "Chapter 3"], [None, None]))

    def test_renames_plain_chapter_numbers_when_the_ebook_has_subtitles(self):
        self.assertTrue(is_generic(["Chapter 1", "Chapter 2"],
                                   ["Chapter 1: The Harbour", "Chapter 2: The Storm"]))


class DisplayTitleTest(unittest.TestCase):
    def test_converts_spelled_out_chapter_numbers_to_digits(self):
        self.assertEqual(display_title("CHAPTER ONE"), "Chapter 1")

    def test_joins_a_chapter_number_and_subtitle_with_a_colon(self):
        self.assertEqual(display_title("Chapter One • The Lighthouse"), "Chapter 1: The Lighthouse")

    def test_reads_a_bare_numbered_title_as_a_chapter(self):
        self.assertEqual(display_title("1. The Long Winter"), "Chapter 1: The Long Winter")

    def test_joins_a_named_section_and_subtitle_with_a_colon(self):
        self.assertEqual(display_title("Prologue • Before the Flood"), "Prologue: Before the Flood")

    def test_keeps_unnumbered_titles_as_written(self):
        self.assertEqual(display_title("Across the Bay"), "Across the Bay")

    def test_does_not_split_an_abbreviation_from_the_rest_of_a_title(self):
        self.assertEqual(display_title("Mr. Brown Goes Home"), "Mr. Brown Goes Home")

    def test_keeps_a_part_heading_with_its_subtitle_untouched(self):
        self.assertEqual(display_title("Part I: The Long Road"), "Part I: The Long Road")


class MatchStartsTest(unittest.TestCase):
    def test_matches_an_opening_heard_shortly_after_the_chapter_starts(self):
        abs_chapters = named((0, 600), (600, 1200))
        self.assertEqual(match_starts(abs_chapters, {0: 30.0, 1: 640.0}), {0: 0, 1: 1})

    def test_rejects_a_match_outside_the_lead_in_tolerance(self):
        abs_chapters = named((0, 600), (600, 1200))
        late = 600 + MATCH_LEAD_IN_SECONDS + 1
        self.assertEqual(match_starts(abs_chapters, {1: late}), {})

    def test_never_matches_an_earlier_ebook_chapter_after_a_later_one(self):
        abs_chapters = named((0, 600), (600, 1200))
        self.assertEqual(match_starts(abs_chapters, {5: 10.0, 3: 610.0}), {0: 5})


class CandidatesTest(unittest.TestCase):
    def test_picks_the_untried_start_nearest_each_unmatched_chapter(self):
        abs_chapters = named((0, 600), (600, 1200))
        pairs = candidates(abs_chapters, [0, 1], [5.0, 590.0, 900.0], {0, 1, 2}, {}, set())
        self.assertEqual(pairs, [(0, 0), (1, 1)])

    def test_skips_a_pair_that_was_already_probed(self):
        abs_chapters = named((0, 600), (600, 1200))
        pairs = candidates(abs_chapters, [1], [5.0, 590.0, 900.0], {0, 1, 2}, {}, {(1, 1)})
        self.assertEqual(pairs, [(1, 2)])

    def test_only_considers_starts_between_the_neighbouring_matches(self):
        abs_chapters = named((0, 600), (600, 1200), (1200, 1800))
        pairs = candidates(abs_chapters, [0, 1, 2], [0.0, 700.0, 1150.0, 1300.0], {0, 1, 2, 3},
                           {0: 0, 2: 2}, set())
        self.assertEqual(pairs, [(1, 1)])

    def test_never_offers_a_start_without_a_title_of_its_own(self):
        abs_chapters = named((0, 600))
        self.assertEqual(candidates(abs_chapters, [0], [0.0], set(), {}, set()), [])


class RenamedTitlesTest(unittest.TestCase):
    def test_names_a_short_unmatched_leading_chapter_opening_credits(self):
        abs_chapters = named((0, 30), (30, 900))
        self.assertEqual(renamed_titles(abs_chapters, {1: "Chapter 1"}),
                         {0: OPENING_CREDITS, 1: "Chapter 1"})

    def test_names_a_short_unmatched_trailing_chapter_end_credits(self):
        abs_chapters = named((0, 900), (900, 924))
        self.assertEqual(renamed_titles(abs_chapters, {0: "Chapter 1"}),
                         {0: "Chapter 1", 1: END_CREDITS})

    def test_keeps_the_old_name_on_a_long_unmatched_leading_chapter(self):
        abs_chapters = named((0, CREDITS_MAX_SECONDS + 3000), (CREDITS_MAX_SECONDS + 3000, 6000))
        self.assertNotIn(0, renamed_titles(abs_chapters, {1: "Chapter 2"}))

    def test_keeps_the_old_name_on_an_unmatched_chapter_between_matches(self):
        abs_chapters = named((0, 900), (900, 910), (910, 1800))
        self.assertNotIn(1, renamed_titles(abs_chapters, {0: "Chapter 1", 2: "Chapter 3"}))

    def test_renames_nothing_when_no_chapter_was_matched(self):
        self.assertEqual(renamed_titles(named((0, 30), (30, 900)), {}), {})


class FillGapsTest(unittest.TestCase):
    """Three ABS chapters; the outer two matched ebook starts 0 and 2."""
    TITLES = ["Chapter 3", "Chapter 4", "Chapter 5"]

    def setUp(self):
        self.abs_chapters = named((0, 600), (600, 1200), (1200, 1800))

    def fill(self, attempts, titles=None, matched=None):
        return fill_gaps(self.abs_chapters, {0, 1, 2}, matched or {0: 0, 2: 2}, titles or self.TITLES, attempts)

    def test_names_a_lone_chapter_between_matches_after_a_near_miss(self):
        self.assertEqual(self.fill({(1, 1): (-0.58, 640.0)}), {1: "Chapter 4"})

    def test_leaves_a_lone_chapter_unnamed_when_its_probe_scored_below_the_near_miss_bar(self):
        self.assertEqual(self.fill({(1, 1): (NEAR_MISS_SCORE - 0.5, 640.0)}), {})

    def test_leaves_a_lone_chapter_unnamed_when_the_near_miss_was_heard_outside_the_tolerance(self):
        self.assertEqual(self.fill({(1, 1): (-0.58, 900.0)}), {})

    def test_names_a_chapter_with_no_opening_of_its_own_as_the_previous_one_continued(self):
        matched = {0: 0, 2: 1}
        self.assertEqual(self.fill({(1, 1): (-2.23, 610.0)}, matched=matched), {1: "Chapter 3 (continued)"})

    def test_calls_a_chapter_continued_without_any_probe_of_its_own(self):
        matched = {0: 0, 2: 1}
        self.assertEqual(self.fill({}, matched=matched), {1: "Chapter 3 (continued)"})

    def test_does_not_call_a_chapter_continued_when_an_untitled_start_lies_between_the_neighbours(self):
        matched = {0: 0, 2: 2}
        self.assertEqual(self.fill({}, titles=["Chapter 3", None, "Chapter 5"], matched=matched), {})

    def test_leaves_a_run_of_two_unmatched_chapters_alone(self):
        abs_chapters = named((0, 600), (600, 1200), (1200, 1800), (1800, 2400))
        self.assertEqual(fill_gaps(abs_chapters, {0, 1, 2, 3}, {0: 0, 3: 3}, ["a", "b", "c", "d"],
                                   {(1, 1): (-0.6, 640.0), (2, 2): (-0.6, 1240.0)}), {})


if __name__ == "__main__":
    unittest.main()


class MergeRepeatsTest(unittest.TestCase):
    def test_joins_adjacent_chapters_with_the_same_title_into_one_span(self):
        merged = merge_repeats([{"title": "Opening Credits", "start": 0, "end": 17},
                                {"title": "Opening Credits", "start": 17, "end": 26},
                                {"title": "Chapter 1", "start": 26, "end": 900}])
        self.assertEqual(merged, [{"title": "Opening Credits", "start": 0, "end": 26},
                                  {"title": "Chapter 1", "start": 26, "end": 900}])

    def test_keeps_chapters_with_the_same_title_apart_when_something_sits_between_them(self):
        chapters = [{"title": "Interlude", "start": 0, "end": 60},
                    {"title": "Chapter 1", "start": 60, "end": 900},
                    {"title": "Interlude", "start": 900, "end": 960}]
        self.assertEqual(merge_repeats(chapters), chapters)
