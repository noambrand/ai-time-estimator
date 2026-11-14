# test_backtest.py - request windows, the extra fields measure() returns, human waits inside a request,
# and replaying past requests through the time model.
from __future__ import annotations

import contextlib
import io
import time
import unittest

from fixtures import MODEL, IsolatedTest, Transcript, make_rates
import backtest
import cc_log
import estimate
import compare


class Requests(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.base = time.time() - 3600
        self.path = self.projects / "C--Work-demo" / "s1.jsonl"

    def two_requests(self) -> Transcript:
        tr = Transcript(self.base)
        tr.human("first request", typed=True)
        read = tr.tool("Read", file_path="a.py")
        tr.turn(read, dur=5, ctx=40_000)
        tr.result(read, gap=1)
        tr.notification(gap=10)                          # not a request boundary
        run = tr.tool("Bash", command="python run.py -v")
        todo = tr.tool("TaskUpdate", taskId="1", status="completed")
        tr.turn(run, todo, dur=4, ctx=42_000)
        tr.result(run, gap=3)
        tr.result(todo, gap=0)
        tr.human("second request", gap=200, typed=True)
        tr.turn(dur=6, ctx=45_000)
        return tr

    def test_windows_run_from_one_typed_message_to_the_next(self):
        events = cc_log.load_streams(self.two_requests().write(self.path))["main"]["events"]
        reqs = compare.requests(events)
        self.assertEqual([r["text"] for r in reqs], ["first request", "second request"])
        self.assertLess(reqs[0]["until"], reqs[1]["start"])
        self.assertEqual(reqs[1]["until"], float("inf"))
        first = compare.measure(events, [], reqs[0]["start"], reqs[0]["until"])
        self.assertAlmostEqual(first["active_min"] * 60, 5 + 1 + 10 + 4 + 3, places=3)
        self.assertEqual(first["runs"], {"python run.py -v": 1})
        self.assertEqual(first["tools"], {"TaskUpdate": 1})
        self.assertEqual((first["first_ctx"], first["model"], first["turns"]), (40_000, MODEL, 2))
        second = compare.measure(events, [], reqs[1]["start"], reqs[1]["until"])
        self.assertAlmostEqual(second["active_min"] * 60, 6, places=3)

    def test_answering_a_question_and_approving_a_tool_are_human_waits(self):
        tr = Transcript(self.base)
        tr.human("go", typed=True)
        ask = tr.tool("AskUserQuestion", questions=[])
        tr.turn(ask, dur=3)
        tr.result(ask, gap=90)                           # the user thinks for 90 s
        edit = tr.tool("Edit", new_string="x")
        tr.turn(edit, dur=2)
        tr.result(edit, gap=45)                          # waited 45 s on a permission prompt
        tr.turn(dur=2)
        events = cc_log.load_streams(tr.write(self.path))["main"]["events"]
        act = compare.measure(events, [], self.base, float("inf"))
        self.assertAlmostEqual(act["human_min"] * 60, 135, places=3)
        self.assertAlmostEqual(act["active_min"] * 60, 3 + 2 + 2, places=3)

    def test_request_counts_are_the_real_calls(self):
        events = cc_log.load_streams(self.two_requests().write(self.path))["main"]["events"]
        req = compare.requests(events)[0]
        counts = backtest.request_counts(compare.measure(events, [], req["start"], req["until"]))
        scenario = counts["scenarios"][0]
        self.assertEqual((scenario["reads"], scenario["edits"], scenario.get("fix_loops", 0)), (1, 0, 0))
        self.assertEqual(scenario["runs"], {"python run.py -v": 1})
        self.assertEqual(scenario["tools"], {"TaskUpdate": 1})
        self.assertEqual(counts["start_context_tokens"], 40_000)
        self.assertEqual(counts["commands"], [{"cmd": "python run.py -v"}])

    def test_replay_and_report(self):
        self.two_requests().write(self.path)
        rows = backtest.replay(make_rates(), self.base - 60, sims=50, min_seconds=0)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["actual"] * 60, 23, places=2)
        self.assertGreater(rows[0]["p50"], 0)
        self.assertEqual(rows[0]["text"], "first request")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            backtest.report(rows, worst=1)
        self.assertIn("inside P10-P90", out.getvalue())
        self.assertIn("miss:", out.getvalue())

    def test_replay_skips_short_requests_and_unknown_models(self):
        tr = self.two_requests()
        tr.human("third", gap=50, typed=True)
        tr.turn(dur=6, model="claude-unknown-9")
        tr.write(self.path)
        rows = backtest.replay(make_rates(), self.base - 60, sims=50, min_seconds=10)   # drops the 6 s request
        self.assertEqual([r["text"] for r in rows], ["first request"])

    def test_project_filter_and_time_window(self):
        self.two_requests().write(self.path)
        rates = make_rates()
        self.assertEqual(backtest.replay(rates, self.base - 60, project="d:/other", sims=50, min_seconds=0), [])
        self.assertEqual(len(backtest.replay(rates, self.base - 60, project="c:/work/demo", sims=50, min_seconds=0)), 2)
        # until just after the first request: the second one is outside the window
        self.assertEqual(len(backtest.replay(rates, self.base - 60, self.base + 100, sims=50, min_seconds=0)), 1)


class SpeedSpread(unittest.TestCase):
    def rows(self, n: int, ratio: float, p50: float) -> list[dict]:
        return [{"actual": p50 * ratio, "p50": p50} for _ in range(n)]

    def test_too_few_requests_give_no_spread(self):
        self.assertIsNone(backtest.speed_factors(self.rows(backtest.MIN_FACTORS - 1, 1.0, 2.0)))

    def test_factors_per_predicted_size_band(self):
        rows = self.rows(25, 0.5, 2.0) + self.rows(25, 2.0, 10.0) + self.rows(5, 3.0, 40.0)
        spread = backtest.speed_factors(rows)
        self.assertEqual(spread["n"], 55)
        small, medium, large = spread["bands"]
        self.assertEqual(set(small["factors"]), {0.5})
        self.assertEqual(set(medium["factors"]), {2.0})
        self.assertEqual(set(large["factors"]), {0.5, 2.0, 3.0})          # too few: uses all bands
        self.assertIsNone(large["max_p50"])

    def test_simulate_draws_one_factor_per_run(self):
        rates = make_rates()
        rates["speed_factors"] = {"n": 40, "bands": [
            {"label": "< 5 min", "max_p50": 5.0, "factors": [0.5, 2.0]},
            {"label": "5-20 min", "max_p50": 20.0, "factors": [1.0]},
            {"label": "> 20 min", "max_p50": None, "factors": [1.0]}]}
        counts = {"scenarios": [{"reads": 2, "edits": 1, "runs": {"python run.py": 1}}]}  # 84 s per run
        res = estimate.simulate(counts, rates, rates["models"][MODEL], "c:/x")
        self.assertAlmostEqual(res["p10"] * 60, 42)
        self.assertAlmostEqual(res["p90"] * 60, 168)
        self.assertTrue(any("speed spread of 40" in n for n in res["notes"]))
        off = estimate.simulate(dict(counts, speed_spread=False), rates, rates["models"][MODEL], "c:/x")
        self.assertAlmostEqual(off["p50"] * 60, 84)


if __name__ == "__main__":
    unittest.main()
