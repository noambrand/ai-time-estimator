# test_correction.py - shown ranges that correct themselves from scored estimates, and the track record.
from __future__ import annotations

import contextlib
import io
import json
import math
import unittest
from unittest import mock

from fixtures import IsolatedTest, make_rates
import estimate


def row(actual: float, p50: float, low: float = 0.5, high: float = 2.0, raw: bool = True) -> dict:
    """A scored estimate: raw range p50*low .. p50*high, shown range the same."""
    r = {"active_min": actual, "p10": p50 * low, "p50": p50, "p90": p50 * high}
    if raw:
        r.update(raw_p10=p50 * low, raw_p50=p50, raw_p90=p50 * high)
    return r


class Correction(unittest.TestCase):
    def test_needs_ten_scored_estimates(self):
        self.assertIsNone(estimate.correction([row(2, 1)] * (estimate.MIN_SCORED - 1)))

    def test_consistent_underestimate_shifts_without_widening(self):
        corr = estimate.correction([row(2.0, 1.0)] * 12)
        self.assertEqual((corr["n"], corr["shift"], corr["width"]), (12, 2.0, 0.5))

    def test_scattered_results_widen_just_enough(self):
        # real = 1x or 4x the prediction -> shift 2.5; needs w*ln2 >= ln(2.5) -> w = 1.35 (first step >= 1.32)
        corr = estimate.correction([row(1.0, 1.0), row(4.0, 1.0)] * 6)
        self.assertEqual(corr["shift"], 2.5)
        self.assertEqual(corr["width"], 1.35)

    def test_only_the_newest_twenty_count(self):
        corr = estimate.correction([row(50.0, 1.0)] * 30 + [row(2.0, 1.0)] * estimate.RECENT_SCORED)
        self.assertEqual(corr["shift"], 2.0)

    def test_rows_without_raw_numbers_use_the_shown_ones(self):
        self.assertEqual(estimate.correction([row(3.0, 1.0, raw=False)] * 10)["shift"], 3.0)

    def test_corrected_range(self):
        p10, p50, p90 = estimate.corrected(1.0, 2.0, 4.0, {"shift": 1.5, "width": 2.0})
        self.assertAlmostEqual(p50, 3.0)
        self.assertAlmostEqual(p10, 3.0 * math.exp(-2 * math.log(2)))
        self.assertAlmostEqual(p90, 3.0 * math.exp(2 * math.log(2)))

    def test_track_record(self):
        self.assertEqual(estimate.track_record([]), "no scored estimates yet")
        rows = [row(1.0, 1.0), row(9.0, 1.0), row(0.6, 1.0)]
        self.assertEqual(estimate.track_record(rows), "2 of 3 past estimates landed inside their range")


class Main(IsolatedTest):
    def run_main(self) -> str:
        counts = {"task": "t", "worker_model": "test", "scenarios": [{"reads": 2, "edits": 1, "runs": {"python run.py": 1}}],
                  "commands": [{"cmd": "python run.py"}]}           # 84 s with make_rates()
        out = io.StringIO()
        with mock.patch("sys.argv", ["estimate.py", "-"]), mock.patch("sys.stdin", io.StringIO(json.dumps(counts))), \
                contextlib.redirect_stdout(out):
            estimate.main()
        return out.getvalue()

    def test_estimate_prints_and_logs_corrected_and_raw_numbers(self):
        self.write_rates()
        (self.data / "actuals.jsonl").write_text("".join(json.dumps(row(2.0, 1.0)) + "\n" for _ in range(12)),
                                                 encoding="utf-8")
        printed = self.run_main()
        record = json.loads((self.data / "estimates.jsonl").read_text(encoding="utf-8"))
        self.assertAlmostEqual(record["raw_p50"], 1.4)
        self.assertAlmostEqual(record["p50"], 2.8)
        self.assertEqual(record["correction"]["n"], 12)
        self.assertIn("P50 2.8 min", printed)
        self.assertIn("Corrected from the last 12 scored estimates", printed)
        self.assertIn("Track record: 12 of 12 past estimates landed inside their range.", printed)

    def test_no_correction_before_enough_scores(self):
        self.write_rates()
        printed = self.run_main()
        record = json.loads((self.data / "estimates.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((record["p50"], record["raw_p50"], record["correction"]), (1.4, 1.4, None))
        self.assertNotIn("Corrected", printed)
        self.assertIn("Track record: no scored estimates yet.", printed)


if __name__ == "__main__":
    unittest.main()
