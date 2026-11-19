# test_known_waits.py - a command the caller marks as a known wait (fixed_seconds, or remaining_seconds for a
# job already running) is a constant: never sampled, never scaled by the speed spread or the self-correction,
# so P10 can never fall below it.
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from fixtures import MODEL, IsolatedTest, make_rates
import estimate

SPREAD = {"n": 40, "bands": [{"label": "< 5 min", "max_p50": 5.0, "factors": [0.5, 2.0]},
                             {"label": "5-20 min", "max_p50": 20.0, "factors": [0.5, 2.0]},
                             {"label": "> 20 min", "max_p50": None, "factors": [0.5, 2.0]}]}
WAIT = "wait for the running build"


def run(command: dict, runs: int = 1, spread: bool = True, **scenario) -> dict:
    rates = make_rates()
    if spread:
        rates["speed_factors"] = SPREAD
    counts = {"commands": [dict(command, cmd=WAIT)], "scenarios": [dict({"runs": {WAIT: runs}}, **scenario)]}
    return estimate.simulate(counts, rates, rates["models"][MODEL], "c:/x")


class KnownWaits(unittest.TestCase):
    def test_remaining_seconds_is_added_after_the_speed_factor(self):
        # 1 call -> 2 turns = 20 s, scaled by 0.5 or 2.0 (10 or 40 s); the 3600 s wait is never scaled.
        res = run({"remaining_seconds": 3600})
        self.assertAlmostEqual(res["p10"] * 60, 3600 + 10)
        self.assertAlmostEqual(res["p90"] * 60, 3600 + 40)
        self.assertAlmostEqual(res["floor"] * 60, 3600)
        self.assertTrue(any("KNOWN WAIT 3600 s" in n for n in res["notes"]))

    def test_fixed_seconds_counts_once_per_run(self):
        res = run({"fixed_seconds": 60}, runs=3, spread=False)            # 3 calls -> 4 turns = 40 s
        self.assertAlmostEqual(res["p50"] * 60, 40 + 3 * 60)
        self.assertAlmostEqual(res["floor"] * 60, 180)

    def test_known_wait_as_background_job_sets_the_wall_clock(self):
        counts = {"commands": [{"cmd": WAIT, "remaining_seconds": 1800}], "scenarios": [{"background": {WAIT: 1}}]}
        rates = make_rates()
        res = estimate.simulate(counts, rates, rates["models"][MODEL], "c:/x")
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 1)                # AI time: 2 turns + the launch
        self.assertAlmostEqual(res["wall"][10] * 60, 1800)

    def test_measured_command_without_a_known_wait_is_still_sampled_and_scaled(self):
        rates = make_rates()
        rates["speed_factors"] = SPREAD
        counts = {"commands": [{"cmd": "python run.py"}], "scenarios": [{"runs": {"python run.py": 1}}]}
        res = estimate.simulate(counts, rates, rates["models"][MODEL], "c:/x")
        self.assertAlmostEqual(res["p10"] * 60, (20 + 30) * 0.5)
        self.assertAlmostEqual(res["p90"] * 60, (20 + 30) * 2.0)
        self.assertEqual(res.get("floor", 0), 0)


class Main(IsolatedTest):
    def run_main(self, counts: dict) -> str:
        out = io.StringIO()
        with mock.patch("sys.argv", ["estimate.py", "-"]), mock.patch("sys.stdin", io.StringIO(json.dumps(counts))), \
                contextlib.redirect_stdout(out):
            estimate.main()
        return out.getvalue()

    def test_self_correction_never_pushes_p10_below_the_known_wait(self):
        self.write_rates()
        # Ten past estimates that came in at a tenth of the prediction: shift 0.1.
        rows = [{"active_min": 0.1, "p10": 0.5, "p50": 1.0, "p90": 2.0} for _ in range(12)]
        (self.data / "actuals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        counts = {"task": "t", "worker_model": "test", "commands": [{"cmd": WAIT, "remaining_seconds": 3540}],
                  "scenarios": [{"runs": {WAIT: 1}}]}
        self.run_main(counts)
        record = json.loads((self.data / "estimates.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["correction"]["shift"], 0.1)
        self.assertGreaterEqual(record["p10"], 59.0)                      # 3540 s = 59 min, never less


if __name__ == "__main__":
    unittest.main()
