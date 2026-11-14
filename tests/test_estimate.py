# test_estimate.py - the Monte Carlo calculator, checked against hand-computed totals.
# With fixtures.make_rates(): turn 10 s (<25k ctx) / 20 s (>=25k), Read 1 s, Grep 0.5 s, Edit 2 s,
# 'python run.py' 30 s, 1 call per turn, start ctx 20k, +1000 per turn, 10 tokens/line, 100 tok/s.
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from fixtures import MODEL, IsolatedTest, make_rates
import estimate

RUN = "python run.py"
A = {"reads": 2, "edits": 1, "runs": {RUN: 1}}                  # 4 calls -> 5 turns: 50 + 4 + 30 = 84 s
B = dict(A, fix_loops=1)                                          # 7 calls -> 8 turns: 110 + 8 + 60 = 178 s


def run(scenarios=None, rates=None, project="c:/other", **top) -> dict:
    rates = rates or make_rates()
    counts = dict({"commands": [{"cmd": RUN, "verify": True}], "scenarios": scenarios}, **top)
    if scenarios is None:
        counts.pop("scenarios")
    return estimate.simulate(counts, rates, rates["models"][MODEL], project)


class HandComputed(unittest.TestCase):
    def assertMinutes(self, res: dict, seconds: float) -> None:
        for q in ("p10", "p50", "p90"):
            self.assertAlmostEqual(res[q] * 60, seconds, places=6, msg=q)

    def test_simple_scenario(self):
        res = run([A])
        self.assertMinutes(res, 84)
        self.assertEqual(res["median_parts"]["turns"], 5)

    def test_fix_loop_adds_calls_edits_and_a_verify_rerun(self):
        self.assertMinutes(run([B]), 178)  # 3 turns cross into the 25k bucket

    def test_long_write_time_beyond_typical_turn_output(self):
        # 1 Write -> 2 turns (20 s) + edit 2 s + (100 lines*10 tok - 500 typical) / 100 tok/s = 5 s
        res = run([{"edits": 1, "write_lines": 100}])
        self.assertMinutes(res, 27)
        self.assertAlmostEqual(res["median_parts"]["writes"], 5.0)

    def test_empty_scenario_is_one_turn(self):
        self.assertMinutes(run([{}]), 10)

    def test_read_lines_push_turns_into_slower_bucket(self):
        self.assertGreater(run([dict(A, read_lines=5000)])["p50"], run([A])["p50"])

    def test_baseline_is_likely_calls_times_seconds_per_call(self):
        res = run([A])
        self.assertEqual(res["likely_calls"], 4)
        self.assertAlmostEqual(res["baseline"], 4 * 15 / 60)

    def test_deterministic(self):
        sc = [dict(A, reads={"low": 0, "likely": 2, "high": 10}), dict(B, weight=0.5)]
        self.assertEqual(run(sc)["p50"], run(sc)["p50"])


class Scenarios(unittest.TestCase):
    def test_weights_normalized_and_by_scenario_medians(self):
        res = run([dict(A, name="a", weight=2), dict(B, name="b", weight=6)])
        names = [(n, round(w, 2), round(m * 60)) for n, w, m in res["by_scenario"]]
        self.assertEqual(names, [("a", 0.25, 84), ("b", 0.75, 178)])
        self.assertLessEqual(res["p10"] * 60, 84 + 1e-6)
        self.assertAlmostEqual(res["p90"] * 60, 178)

    def test_missing_weight_defaults_to_equal(self):
        self.assertEqual([s["weight"] for s in estimate.scenarios_of({"scenarios": [{}, {}]})], [0.5, 0.5])

    def test_ranges_spread_the_percentiles(self):
        res = run([dict(A, reads={"low": 0, "likely": 2, "high": 30})])
        self.assertLess(res["p10"], res["p90"])

    def test_v10_flat_format(self):
        flat = {"reads": 2, "edits": 1, "commands": [{"cmd": RUN, "runs": 1}]}
        rates = make_rates()
        res = estimate.simulate(flat, rates, rates["models"][MODEL], "c:/other")
        self.assertAlmostEqual(res["p50"] * 60, 84)
        self.assertEqual(estimate.scenarios_of(flat)[0]["name"], "single")

    def test_likely_counts(self):
        sc = {"reads": {"low": 1, "likely": 3, "high": 9}, "fix_loops": 2, "runs": {RUN: 1, "x": 2},
              "tools": {"WebFetch": 2, "Agent": 1}}
        got = estimate.likely_counts(sc)
        self.assertEqual((got["reads"], got["commands"], got["other_calls"], got["bookkeeping"]), (3, 5, 3, 0))
        got = estimate.likely_counts({"tools": {"TaskUpdate": 4, "TaskCreate": 2, "WebFetch": 1}})
        self.assertEqual((got["other_calls"], got["bookkeeping"]), (1, 6))

    def test_expected_scenario_is_heaviest(self):
        sc = estimate.scenarios_of({"scenarios": [{"name": "x", "weight": 1}, {"name": "y", "weight": 3}]})
        self.assertEqual(estimate.expected_scenario(sc)["name"], "y")


class Lookups(unittest.TestCase):
    def test_command_scope_and_unmeasured(self):
        rates = make_rates()
        self.assertIn("this project", estimate.command_samples(rates, "c:/work/demo", "npm test", 5)[1])
        self.assertIn("other projects", estimate.command_samples(rates, "c:/work/demo", "python run.py -v", 5)[1])
        samples, source = estimate.command_samples(rates, "c:/x", "make all", 7)
        self.assertEqual((samples, source), ([7.0], "UNMEASURED (fallback_seconds)"))

    def test_unmeasured_command_uses_fallback_in_total(self):
        res = run([{"runs": {"make all": 1}}], commands=[{"cmd": "make all", "fallback_seconds": 7}])
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 7)  # 1 call -> 2 turns
        self.assertTrue(any("UNMEASURED" in n for n in res["notes"]))

    def test_task_alias_finds_agent_samples_and_unmeasured_tool_fallback(self):
        res = run([{"tools": {"Task": 1, "WebSearch": 1}}], tools=[{"tool": "WebSearch", "fallback_seconds": 9}])
        self.assertAlmostEqual(res["p50"] * 60, 3 * 10 + 60 + 9)
        self.assertTrue(any("measured 'Agent'" in n for n in res["notes"]))

    def test_pick_model(self):
        rates = make_rates()
        rates["models"]["claude-test-small"] = dict(rates["models"][MODEL], turns=5)
        self.assertEqual(estimate.pick_model(rates, "TEST")[0], MODEL)  # case-insensitive, most turns wins
        with self.assertRaises(SystemExit):
            estimate.pick_model(rates, "gpt")

    def test_no_usable_bucket_pools_samples_and_notes_it(self):
        rates = make_rates(turn_seconds_by_ctx={"0": [10.0] * 3})
        res = run([A], rates=rates)
        self.assertAlmostEqual(res["p50"] * 60, 84)
        self.assertTrue(any("without context adjustment" in n for n in res["notes"]))


class Fallbacks(unittest.TestCase):
    def test_measured_zero_fix_loop_size_is_used(self):
        # A fix loop of 0 calls = straight rerun: 5 calls -> 6 turns (50 + 20) + tools 4 + commands 60 = 134 s.
        rates = make_rates()
        rates["fix_loop_calls"]["median"] = 0
        res = run([B], rates=rates)
        self.assertAlmostEqual(res["p50"] * 60, 134)
        self.assertFalse(any("fix-loop size not measured" in n for n in res["notes"]))

    def test_unmeasured_fix_loop_size_falls_back_to_2(self):
        rates = make_rates()
        rates["fix_loop_calls"]["median"] = None
        res = run([B], rates=rates)
        self.assertAlmostEqual(res["p50"] * 60, 178)
        self.assertTrue(any("fix-loop size not measured" in n for n in res["notes"]))

    def test_unmeasured_model_rates_use_defaults(self):
        rates = make_rates(start_context_tokens=None, context_growth_per_turn=None, tokens_per_written_line=None)
        res = run([{"edits": 1, "write_lines": 100}], rates=rates)
        # 12 tok/line -> 1200 - 500 = 700 tokens at 100 tok/s = 7 s; 20k start ctx, 1500 growth -> 10 s turns
        self.assertAlmostEqual(res["p50"] * 60, 20 + 2 + 7)

    def test_measured_zero_growth_is_kept(self):
        rates = make_rates(start_context_tokens=24_000, context_growth_per_turn=0)
        # growth 0 keeps every turn in the 10 s bucket (with the 1500 default turn 2 would cross 25k).
        self.assertAlmostEqual(run([{"reads": 1}], rates=rates)["p50"] * 60, 2 * 10 + 1)


class Main(IsolatedTest):
    def run_main(self, counts: dict) -> str:
        out = io.StringIO()
        with mock.patch("sys.argv", ["estimate.py", "-"]), mock.patch("sys.stdin", io.StringIO(json.dumps(counts))), \
                contextlib.redirect_stdout(out):
            estimate.main()
        return out.getvalue()

    def test_logs_each_estimate_with_unique_id(self):
        self.write_rates()
        counts = {"task": "t", "task_type": "bugfix", "project": r"C:\Work\Demo", "worker_model": "test",
                  "commands": [{"cmd": RUN, "verify": True}], "scenarios": [A]}
        first, second = self.run_main(counts), self.run_main(counts)
        self.assertIn("P10 1.4 min | P50 1.4 min | P90 1.4 min", first)
        rows = [json.loads(x) for x in (self.data / "estimates.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertIn(rows[1]["id"], second)
        self.assertEqual((rows[0]["project"], rows[0]["model"], rows[0]["p50"]), ("c:/work/demo", MODEL, 1.4))

    def test_missing_rates_exits(self):
        with self.assertRaises(SystemExit):
            self.run_main({"scenarios": [A]})


if __name__ == "__main__":
    unittest.main()
