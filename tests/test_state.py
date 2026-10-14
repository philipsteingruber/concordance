import json
import tempfile
import unittest
from pathlib import Path

from concordance.cli import save_state
from concordance.report import PairReport, Report


def applied(book_id: int, target: str, payload: dict, description: str) -> PairReport:
    entry = PairReport(book_id, "li", "Book", "isbn", 1.0, True, 10, 10, 20.0, 30.0, "high",
                       False, 0.0, None)
    entry.direction = f"to_{target}"
    entry.planned_write = f"{target}: {description}"
    entry.write_payload = payload
    entry.write_status = 200
    return entry


def logged(*entries: PairReport) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        save_state(Report("2026-10-14T06:30:01+00:00", False, 90, list(entries)), state)
        return [json.loads(line) for line in (state / "writes.jsonl").read_text().splitlines()]


class WritesLogPositionTest(unittest.TestCase):
    def test_it_logs_the_xpointer_a_cwa_write_sent(self):
        xp = "/body/DocFragment[8]/body/p[3]/text().120"
        [row] = logged(applied(1, "cwa", {"progress": xp, "percentage": 0.013}, "1.3% at DocFragment[8] (kepub)"))
        self.assertEqual(row["position"], xp)

    def test_it_tells_apart_cwa_writes_whose_clamped_percentage_matches(self):
        rows = logged(
            applied(1, "cwa", {"progress": "/body/DocFragment[8]/body/p[1]/text().0", "percentage": 0.013},
                    "1.3% at DocFragment[8] (kepub)"),
            applied(1, "cwa", {"progress": "/body/DocFragment[8]/body/p[40]/text().5", "percentage": 0.013},
                    "1.3% at DocFragment[8] (kepub)"),
        )
        self.assertEqual(rows[0]["write"], rows[1]["write"])
        self.assertNotEqual(rows[0]["position"], rows[1]["position"])

    def test_it_logs_the_seconds_an_abs_write_sent(self):
        [row] = logged(applied(1, "abs", {"currentTime": 499.4, "duration": 9000.0}, "currentTime 0h08m19s"))
        self.assertEqual(row["position"], 499.4)


if __name__ == "__main__":
    unittest.main()
