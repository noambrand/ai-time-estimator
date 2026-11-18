# test_jobs.py - measured durations of named long jobs (a test suite, a corpus run, a release build) recorded
# with estimate.py --record-job, used for a declared command with that name instead of fallback_seconds.
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from fixtures import MODEL, IsolatedTest, make_rates
import estimate


def simulate(command: dict, jobs: dict | None) -> dict:
    rates = make_rates()
    counts = {"commands": [command], "scenarios": [{"runs": {command["cmd"]: 1}}]}
    extra = {} if jobs is None else {"jobs": jobs}
    return estimate.simulate(counts, rates, rates["models"][MODEL], "c:/x", **extra)


class Lookup(unittest.TestCase):
    def test_recorded_job_replaces_fallback_seconds(self):
        res = simulate({"cmd": "full-suite", "fallback_seconds": 7200}, {"full-suite": [300.0, 300.0]})
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 300)            # 1 call -> 2 turns + the job
        self.assertTrue(any("MEASURED job 'full-suite' n=2" in n for n in res["notes"]))

    def test_job_field_names_the_job_of_any_command_text(self):
        res = simulate({"cmd": "bash run_suite.sh --all", "job": "Full-Suite"}, {"full-suite": [120.0]})
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 120)

    def test_known_wait_beats_a_recorded_job(self):
        res = simulate({"cmd": "full-suite", "remaining_seconds": 60}, {"full-suite": [300.0]})
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 60)

    def test_without_a_record_the_fallback_stays(self):
        res = simulate({"cmd": "full-suite", "fallback_seconds": 45}, None)
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 45)
        self.assertTrue(any("UNMEASURED (fallback_seconds)" in n for n in res["notes"]))


class Cli(IsolatedTest):
    def run_main(self, *argv: str, stdin: str = "") -> str:
        out = io.StringIO()
        with mock.patch("sys.argv", ["estimate.py", *argv]), mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out):
            estimate.main()
        return out.getvalue()

    def test_record_list_and_use_a_job(self):
        self.write_rates()
        self.assertIn("Recorded job 'full-suite': 280 s", self.run_main("--record-job", "full-suite", "280"))
        self.run_main("--record-job", "full-suite", "320")
        rows = [json.loads(x) for x in (self.data / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["seconds"] for r in rows], [280.0, 320.0])
        self.assertIn("full-suite: n=2 | median 300 s", self.run_main("--jobs"))
        counts = {"task": "t", "worker_model": "test", "commands": [{"cmd": "full-suite", "fallback_seconds": 9000}],
                  "scenarios": [{"runs": {"full-suite": 1}}], "speed_spread": False}
        printed = self.run_main("-", stdin=json.dumps(counts))
        self.assertIn("MEASURED job 'full-suite' n=2", printed)
        record = json.loads((self.data / "estimates.jsonl").read_text(encoding="utf-8"))
        self.assertLess(record["p90"], 10)                                  # minutes, not the 150-min fallback

    def test_bad_job_input_is_refused(self):
        for argv in (("--record-job", "x", "-5"), ("--record-job", " ", "10"), ()):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.run_main(*argv)
        self.assertFalse((self.data / "jobs.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
