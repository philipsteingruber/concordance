import tempfile
import unittest
import unittest.mock
from pathlib import Path

from concordance.allowlist import add, read_ids, remove
from concordance.writer import WritePolicy


class AllowlistFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "allowlist"

    def tearDown(self):
        self.dir.cleanup()

    def test_treats_a_missing_file_as_an_empty_allowlist(self):
        self.assertEqual(read_ids(self.path), set())

    def test_writes_the_title_as_a_comment_after_the_id(self):
        add(self.path, 561, "Misery")
        self.assertEqual(self.path.read_text(), "561  # Misery\n")

    def test_reports_an_id_that_is_already_allowed(self):
        add(self.path, 561)
        self.assertFalse(add(self.path, 561))

    def test_removes_only_the_named_id(self):
        self.path.write_text("# my books\n561  # Misery\n581  # The Escape Room\n")
        remove(self.path, 561)
        self.assertEqual(self.path.read_text(), "# my books\n581  # The Escape Room\n")

    def test_reports_removing_an_id_that_was_not_there(self):
        self.path.write_text("581\n")
        self.assertFalse(remove(self.path, 561))

    def test_ignores_comments_and_lines_without_an_id(self):
        self.path.write_text("# header\n561  # Misery\nnot-a-number\n\n  581\n")
        self.assertEqual(read_ids(self.path), {561, 581})

    def test_write_policy_allows_ids_from_the_file_and_the_environment(self):
        self.path.write_text("561\n")
        env = {"CONCORDANCE_WRITE_ALLOWLIST_FILE": str(self.path), "CONCORDANCE_WRITE_ALLOWLIST": "463"}
        with unittest.mock.patch.dict("os.environ", env):
            self.assertEqual(WritePolicy.from_env(True).allowlist, frozenset({463, 561}))


if __name__ == "__main__":
    unittest.main()
