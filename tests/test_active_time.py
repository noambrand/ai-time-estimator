# test_active_time.py - background work is not busy time. A request that starts a background run (a background
# command, an async or named agent) spends only the launch; the idle wait for the completion notice is neither
# active time nor a human wait, and estimates count the run time on a separate wall-clock line.
from __future__ import annotations

import contextlib
import io
import json
import time
import unittest
from unittest import mock

from fixtures import MODEL, IsolatedTest, Transcript, make_rates
import backtest
import calibrate
import cc_log
import compare
import estimate

BUILD = "npm run build"


def background_request(tr: Transcript, text: str = "continue", run_s: float = 600.0) -> dict:
    """A typed request that starts a background build, ends its turn, and is woken by the notice."""
    tr.human(text, gap=5, typed=True)
    run = tr.tool("Bash", command=BUILD, run_in_background=True)
    tr.turn(run, dur=4)
    tr.result(run, gap=0.5, text="Command running in background with ID: b1")
    tr.turn(dur=3)                                       # "the build runs in the background"
    tr.notification(run, gap=run_s)
    tr.turn(dur=6)                                       # reads the build result
    return run


class Measure(IsolatedTest):
    def events(self, tr: Transcript) -> list[dict]:
        return cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl"))["main"]["events"]

    def test_idle_wait_for_a_background_command_is_not_active_time(self):
        tr = Transcript(1_000_000.0)
        background_request(tr)
        act = compare.measure(self.events(tr), [], 1_000_000.0, float("inf"))
        self.assertAlmostEqual(act["background_wait_min"] * 60, 600, places=3)
        self.assertAlmostEqual(act["active_min"] * 60, 4 + 0.5 + 3 + 6, places=3)
        self.assertEqual((act["background"], act["runs"], act["counts"]["commands"]), ({BUILD: 1}, {}, 1))
        self.assertEqual(act["background_commands"], [BUILD])

    def test_foreground_command_stays_active_time(self):
        tr = Transcript(1_000_000.0)
        tr.human("go", typed=True)
        run = tr.tool("Bash", command=BUILD)
        tr.turn(run, dur=4)
        tr.result(run, gap=600)                          # the session waited on its own command
        tr.turn(dur=6)
        act = compare.measure(self.events(tr), [], 1_000_000.0, float("inf"))
        self.assertAlmostEqual(act["active_min"] * 60, 4 + 600 + 6, places=3)
        self.assertEqual(act.get("background_wait_min", 0), 0)
        self.assertEqual(act["runs"], {BUILD: 1})

    def test_launch_text_alone_marks_an_agent_as_background(self):
        tr = Transcript(1_000_000.0)
        tr.human("go", typed=True)
        agent = tr.tool("Agent", prompt="fix it", description="fix")   # no run_in_background flag
        tr.turn(agent, dur=3)
        tr.result(agent, gap=1, text="Async agent launched successfully. agentId: a1")
        tr.turn(dur=2)
        tr.notification(agent, gap=300)
        tr.turn(dur=4)
        act = compare.measure(self.events(tr), [], 1_000_000.0, float("inf"))
        self.assertAlmostEqual(act["background_wait_min"] * 60, 300, places=3)
        self.assertEqual(act["background"], {"Agent": 1})


class Calibrate(IsolatedTest):
    def finish(self, tr: Transcript) -> dict:
        acc = calibrate.Accumulator()
        for name, stream in cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl")).items():
            acc.add_stream(stream["cwd"], stream["events"], name == "main")
        return acc.finish()

    def test_session_rate_counts_the_launch_not_the_background_run(self):
        tr = Transcript(1_000_000.0)
        tr.human("go", typed=True)
        for _ in range(12):                              # 12 reads: 2 s turn + 1 s tool each
            read = tr.tool("Read", file_path="a.py")
            tr.turn(read, dur=2)
            tr.result(read, gap=1)
        background_request(tr, run_s=900)
        for _ in range(20):                              # enough turns for the model to be kept
            tr.turn(dur=2)
        rates = self.finish(tr)
        self.assertEqual(rates["commands"]["*"].get(BUILD), None)          # one sample: under the 2 kept
        self.assertEqual(rates["background_launch_seconds"], None)         # one launch: under the 3 needed
        spc = rates["models"][MODEL]["seconds_per_call"]
        self.assertLess(spc, 10)                         # 900 s of background run would give ~70 s per call

    def test_launch_seconds_measured_and_no_sample_without_a_notice(self):
        tr = Transcript(1_000_000.0)
        for gap in (0.4, 0.6, 0.8):
            run = tr.tool("Bash", command=BUILD, run_in_background=True)
            tr.turn(run, dur=2)
            tr.result(run, gap=gap, text="Command running in background with ID: b2")
        rates = self.finish(tr)
        self.assertAlmostEqual(rates["background_launch_seconds"], 0.6, places=2)
        self.assertNotIn(BUILD, rates["commands"].get("*", {}))           # a launch is no run-time sample


class Simulate(unittest.TestCase):
    def test_background_agent_costs_its_launch_and_shows_a_wall_clock(self):
        rates = make_rates()                             # Agent samples 60 s; no measured launch -> 1 s
        res = estimate.simulate({"scenarios": [{"background": {"Agent": 1}}]}, rates, rates["models"][MODEL], "c:/x")
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 1)                # 1 call -> 2 turns + launch
        self.assertAlmostEqual(res["wall"][50] * 60, 60)
        self.assertTrue(any("background tool 'Agent'" in n for n in res["notes"]))

    def test_background_command_uses_its_measured_run_time_for_the_wall_clock(self):
        rates = make_rates()
        rates["background_launch_seconds"] = 2.0
        counts = {"commands": [{"cmd": "python run.py"}], "scenarios": [{"background": {"python run.py": 1}}]}
        res = estimate.simulate(counts, rates, rates["models"][MODEL], "c:/x")
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 2)
        self.assertAlmostEqual(res["wall"][50] * 60, 30)

    def test_foreground_only_estimate_has_no_wall_line(self):
        rates = make_rates()
        res = estimate.simulate({"scenarios": [{"tools": {"Agent": 1}}]}, rates, rates["models"][MODEL], "c:/x")
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 60)               # a foreground agent blocks the session
        self.assertIsNone(res["wall"])

    def test_likely_counts_put_background_calls_in_their_field(self):
        got = estimate.likely_counts({"background": {"Agent": 2, BUILD: 1}, "runs": {"x": 1}}, {BUILD, "x"})
        self.assertEqual((got["other_calls"], got["commands"]), (2, 2))


class Replay(IsolatedTest):
    def test_continue_request_is_predicted_by_its_launch_not_the_background_run(self):
        base = time.time() - 3600
        tr = Transcript(base)
        background_request(tr, "continue", run_s=600)
        tr.human("next thing", gap=30, typed=True)
        tr.write(self.projects / "C--Work-demo" / "s1.jsonl")
        rates = make_rates()
        rates["commands"]["*"][BUILD] = [600.0] * 3      # the build takes 10 minutes
        row = backtest.replay(rates, base - 60, sims=50, min_seconds=0)[0]
        self.assertAlmostEqual(row["actual"] * 60, 4 + 0.5 + 3 + 6, places=2)
        self.assertLess(row["p50"] * 60, 60)             # was 10+ minutes: the build counted as busy time

    def test_request_counts_route_background_calls(self):
        tr = Transcript(1_000_000.0)
        background_request(tr)
        events = cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl"))["main"]["events"]
        counts = backtest.request_counts(compare.measure(events, [], 1_000_000.0, float("inf")))
        self.assertEqual(counts["scenarios"][0]["background"], {BUILD: 1})
        self.assertEqual(counts["scenarios"][0]["runs"], {})
        self.assertEqual(counts["commands"], [{"cmd": BUILD}])


class Main(IsolatedTest):
    def test_estimate_prints_the_wall_clock_line_and_logs_it(self):
        self.write_rates()
        counts = {"task": "t", "worker_model": "test", "scenarios": [{"background": {"Agent": 1}}]}
        out = io.StringIO()
        with mock.patch("sys.argv", ["estimate.py", "-"]), mock.patch("sys.stdin", io.StringIO(json.dumps(counts))), \
                contextlib.redirect_stdout(out):
            estimate.main()
        self.assertIn("Wall clock until the last background result: P10 1.0 | P50 1.0 | P90 1.0 min", out.getvalue())
        record = json.loads((self.data / "estimates.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["wall"], {"p10": 1.0, "p50": 1.0, "p90": 1.0})


if __name__ == "__main__":
    unittest.main()
