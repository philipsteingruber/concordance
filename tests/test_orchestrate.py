import dataclasses
from datetime import datetime
import unittest
import unittest.mock
from pathlib import Path

from concordance.absclient import AbsProgress, Chapter
from concordance.anchor import build_alignment
from concordance.calibre import SpineItem
from concordance.cwa import CwaProgress
from concordance.cache import GroupKey
from concordance.config import Config, ConfigError
from concordance.orchestrate import (Job, aligner_stats, alignment_status, current_group_index, docker_command,
                                     fits_before_deadline, group_span, in_progress, memory_wait_until,
                                     parse_deadline, select_groups)

# Four equal 1000 s chapters, four equal spine items (5-8): a clean 1:1 book.
SPINE = [SpineItem(index=i, href=f"c{i}.xhtml", chars=20_000) for i in (5, 6, 7, 8)]
CHAPTERS = [Chapter(index=i, title=str(i), start=(i - 1) * 1000.0, end=i * 1000.0) for i in (1, 2, 3, 4)]
ALIGNMENT = build_alignment(SPINE, CHAPTERS)


class GroupSelectionTest(unittest.TestCase):
    def test_spans_a_group_from_its_first_to_last_audio_second(self):
        self.assertEqual(group_span(ALIGNMENT, 1), (1000.0, 2000.0))

    def test_picks_the_group_of_the_ebook_position(self):
        self.assertEqual(current_group_index(ALIGNMENT, ebook_spine=7, audio_time=None), 2)

    def test_picks_the_further_group_when_audio_is_ahead_of_the_ebook(self):
        self.assertEqual(current_group_index(ALIGNMENT, ebook_spine=5, audio_time=3500.0), 3)

    def test_queues_the_current_group_and_the_lookahead_groups(self):
        self.assertEqual(select_groups(ALIGNMENT, 6, None, lookahead=2), [1, 2, 3])

    def test_stops_the_lookahead_at_the_last_group(self):
        self.assertEqual(select_groups(ALIGNMENT, 8, None, lookahead=2), [3])

    def test_queues_nothing_when_no_position_falls_in_a_group(self):
        self.assertEqual(select_groups(ALIGNMENT, None, None, lookahead=2), [])


class InProgressTest(unittest.TestCase):
    def ebook(self, pct):
        return CwaProgress(calibre_book_id=1, title="", authors=[], percentage=pct, xpointer=None,
                           last_modified=None)

    def audio(self, t, finished=False):
        return AbsProgress("x", t, 4000.0, t / 4000.0, finished, None)

    def test_counts_a_partly_read_ebook_as_in_progress(self):
        self.assertTrue(in_progress(self.ebook(40.0), None))

    def test_ignores_a_book_finished_on_both_sides(self):
        self.assertFalse(in_progress(self.ebook(100.0), self.audio(4000.0, finished=True)))

    def test_counts_a_started_unfinished_audiobook_as_in_progress(self):
        self.assertTrue(in_progress(None, self.audio(10.0)))


class AlignmentStatusTest(unittest.TestCase):
    def test_reads_the_stats_line_after_other_output(self):
        out = 'loading model\n{"words": 800, "mean_score": -0.07}\n'
        self.assertEqual(aligner_stats(out)["words"], 800)

    def test_returns_no_stats_when_the_worker_printed_none(self):
        self.assertEqual(aligner_stats("Traceback ...\n"), {})

    def test_flags_a_clean_run_whose_score_suggests_the_wrong_audio(self):
        self.assertEqual(alignment_status(0, {"mean_score": -5.9}, -1.0), "aligned-low-score")

    def test_accepts_a_clean_run_with_a_good_score(self):
        self.assertEqual(alignment_status(0, {"mean_score": -0.07}, -1.0), "aligned")

    def test_labels_exit_137_as_out_of_memory(self):
        self.assertEqual(alignment_status(137, {}, -1.0), "killed-oom")


class DockerCommandTest(unittest.TestCase):
    cfg = Config(cwa_url="", cwa_user="", cwa_password="", abs_url="", abs_api_key="",
                 calibre_db="/srv/calibre/metadata.db", calibre_root="/srv/calibre", rewind_seconds=150,
                 abs_audio_root=("/audiobooks", "/srv/media/audiobooks"))
    job = Job("Book", GroupKey(7, "item", "kepub", 3, 3, 2, 2), "/srv/calibre/Author/Book (7)/Book.kepub",
              10.0, 20.0, [("/audiobooks/Book/book.m4b", 30.0)], "current")

    def command(self, cfg=None):
        return docker_command(self.job, cfg or self.cfg, Path("/state"), "item.json")

    def test_mounts_audio_at_the_path_audiobookshelf_reports(self):
        cmd = self.command()
        self.assertIn("/srv/media/audiobooks:/audiobooks:ro", cmd)

    def test_passes_the_book_path_as_seen_inside_the_container(self):
        cmd = self.command()
        self.assertEqual(cmd[cmd.index("--book-file") + 1], "/library/Author/Book (7)/Book.kepub")

    def test_forwards_worker_tuning_set_in_the_environment(self):
        with unittest.mock.patch.dict("os.environ", {"CONCORDANCE_ALIGN_CHUNK_SECONDS": "900"}):
            self.assertIn("CONCORDANCE_ALIGN_CHUNK_SECONDS=900", self.command())

    def test_forwards_no_worker_tuning_that_is_unset(self):
        with unittest.mock.patch.dict("os.environ", {"CONCORDANCE_ALIGN_CHUNK_SECONDS": ""}):
            self.assertFalse(any(arg.startswith("CONCORDANCE_ALIGN_CHUNK_SECONDS") for arg in self.command()))

    def test_refuses_to_build_a_command_without_an_audio_path_map(self):
        with self.assertRaises(ConfigError):
            self.command(dataclasses.replace(self.cfg, abs_audio_root=None))


class DeadlineTest(unittest.TestCase):
    now = datetime(2026, 9, 17, 0, 5)

    def test_runs_without_a_deadline_when_none_is_set(self):
        self.assertIsNone(parse_deadline("", self.now))

    def test_places_a_deadline_later_the_same_night(self):
        self.assertEqual(parse_deadline("02:00", self.now), datetime(2026, 9, 17, 2, 0))

    def test_rolls_a_deadline_already_past_over_to_the_next_day(self):
        self.assertEqual(parse_deadline("00:00", self.now), datetime(2026, 9, 18, 0, 0))

    def test_rejects_a_deadline_that_is_not_hours_and_minutes(self):
        with self.assertRaises(ConfigError):
            parse_deadline("2am", self.now)

    def test_starts_a_group_that_fits_before_the_deadline(self):
        deadline = datetime(2026, 9, 17, 2, 0)
        self.assertTrue(fits_before_deadline(60 * 60, self.now, deadline))

    def test_skips_a_group_that_would_still_be_running_at_the_deadline(self):
        deadline = datetime(2026, 9, 17, 1, 0)
        self.assertFalse(fits_before_deadline(4 * 60 * 60, self.now, deadline))

    def test_starts_any_group_when_no_deadline_is_set(self):
        self.assertTrue(fits_before_deadline(40 * 60 * 60, self.now, None))

    def test_waits_for_memory_up_to_the_wait_limit_without_a_deadline(self):
        self.assertEqual(memory_wait_until(self.now, 60, None), datetime(2026, 9, 17, 1, 5))

    def test_stops_waiting_for_memory_at_an_earlier_deadline(self):
        deadline = datetime(2026, 9, 17, 0, 30)
        self.assertEqual(memory_wait_until(self.now, 60, deadline), deadline)


if __name__ == "__main__":
    unittest.main()
