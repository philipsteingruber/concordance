import tempfile
import unittest
import zipfile
from pathlib import Path

from concordance.decide import Decision
from concordance.writer import DEVICE_NAME, WritePolicy, gate, plan_abs_write, plan_cwa_write
from concordance.xpointer import parse_document, parse_xpointer, resolve, spine_documents


def make_book(directory: Path, ext: str) -> Path:
    path = directory / f"Book.{ext}"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
        zf.writestr("c.opf", '<package><manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/>'
                             '</manifest><spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
        zf.writestr("a.xhtml", "<body><h1>Title</h1></body>")
        zf.writestr("b.xhtml", "<body><p>" + " ".join(f"word{i}" for i in range(200)) + "</p></body>")
    return path


class AbsPlanTest(unittest.TestCase):
    def test_sends_current_time_with_duration_and_progress(self):
        plan = plan_abs_write(Decision("to_abs", "r", tier="interpolated", abs_seconds=1800.0), 1, 3600.0)
        self.assertEqual(plan.payload, {"currentTime": 1800.0, "duration": 3600.0, "progress": 0.5})

    def test_marks_a_finished_book_finished_at_full_duration(self):
        plan = plan_abs_write(Decision("to_abs", "r", tier="finished", abs_finished=True), 1, 3600.0)
        self.assertEqual((plan.payload["isFinished"], plan.payload["currentTime"]), (True, 3600.0))

    def test_never_includes_is_finished_false(self):
        plan = plan_abs_write(Decision("to_abs", "r", tier="interpolated", abs_seconds=10.0), 1, 3600.0)
        self.assertNotIn("isFinished", plan.payload)

    def test_plans_nothing_when_the_decision_points_at_cwa(self):
        self.assertIsNone(plan_abs_write(Decision("to_cwa", "r"), 1, 3600.0))


class CwaPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.kepub = make_book(self.dir, "kepub")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sends_the_percentage_as_a_fraction_lowered_by_the_margin(self):
        d = Decision("to_cwa", "r", tier="interpolated", cwa_percentage=42.5, cwa_spine_index=2,
                     cwa_item_fraction=0.5)
        self.assertEqual(plan_cwa_write(d, 7, self.dir, "kepub").payload["percentage"], 0.405)

    def test_never_writes_a_percentage_below_zero(self):
        d = Decision("to_cwa", "r", tier="interpolated", cwa_percentage=1.0, cwa_spine_index=2,
                     cwa_item_fraction=0.01)
        self.assertEqual(plan_cwa_write(d, 7, self.dir, "kepub").payload["percentage"], 0.0)

    def test_uses_a_device_name_distinct_from_the_kobo(self):
        d = Decision("to_cwa", "r", tier="interpolated", cwa_percentage=42.5, cwa_spine_index=2,
                     cwa_item_fraction=0.5)
        self.assertEqual(plan_cwa_write(d, 7, self.dir, "kepub").payload["device"], DEVICE_NAME)

    def test_builds_an_xpointer_that_resolves_inside_the_target_spine_item(self):
        d = Decision("to_cwa", "r", tier="interpolated", cwa_percentage=42.5, cwa_spine_index=2,
                     cwa_item_fraction=0.5)
        xp = plan_cwa_write(d, 7, self.dir, "kepub").payload["progress"]
        doc = parse_document(spine_documents(self.kepub)[2])
        offset = resolve(doc, parse_xpointer(xp))
        self.assertAlmostEqual(offset / len(doc.text), 0.5, delta=0.02)

    def test_marks_a_finished_book_at_one_hundred_percent(self):
        d = Decision("to_cwa", "r", tier="finished", cwa_finished=True)
        self.assertEqual(plan_cwa_write(d, 7, self.dir, "kepub").payload["percentage"], 1.0)

    def test_blocks_the_write_when_no_spine_position_is_known(self):
        d = Decision("to_cwa", "r", tier="percentage", cwa_percentage=40.0)
        self.assertFalse(plan_cwa_write(d, 7, self.dir, "kepub").allowed)


class GateTest(unittest.TestCase):
    decision = Decision("to_abs", "r", tier="interpolated", abs_seconds=100.0)

    def plan(self):
        return plan_abs_write(self.decision, 463, 1000.0)

    def test_blocks_every_write_in_a_dry_run(self):
        policy = WritePolicy(apply=False, allowlist=frozenset({463}))
        self.assertIn("dry run (no --apply)", gate(self.plan(), self.decision, policy).blocked_by)

    def test_blocks_a_book_missing_from_the_allowlist(self):
        policy = WritePolicy(apply=True, allowlist=frozenset({1}))
        self.assertFalse(gate(self.plan(), self.decision, policy).allowed)

    def test_blocks_a_tier_that_misses_the_precision_bar(self):
        coarse = Decision("to_abs", "r", tier="anchor", abs_seconds=100.0)
        policy = WritePolicy(apply=True, allowlist=frozenset({463}))
        self.assertFalse(gate(plan_abs_write(coarse, 463, 1000.0), coarse, policy).allowed)

    def test_allows_an_allowlisted_precise_write_when_applying(self):
        policy = WritePolicy(apply=True, allowlist=frozenset({463}))
        self.assertTrue(gate(self.plan(), self.decision, policy).allowed)


if __name__ == "__main__":
    unittest.main()
