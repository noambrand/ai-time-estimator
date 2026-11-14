# test_effort.py - turn times per reasoning effort, and estimates that follow the effort (and model)
# of the session that asks.
from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from fixtures import MODEL, IsolatedTest, Transcript, make_rates
import calibrate
import cc_log
import estimate


def effort_session(base: float, effort: str, dur: float, turns: int = 25) -> Transcript:
    tr = Transcript(base)
    tr.human("go", typed=True)
    for _ in range(turns):
        read = tr.tool("Read", file_path="a.py")
        tr.turn(read, dur=dur, effort=effort)
        tr.result(read, gap=1)
    return tr


class Calibrate(IsolatedTest):
    def test_turn_times_are_kept_per_effort(self):
        acc = calibrate.Accumulator()
        for i, (effort, dur) in enumerate((("max", 10.0), ("high", 4.0))):
            path = effort_session(1_000_000.0 + i * 10_000, effort, dur).write(self.projects / "p" / f"s{i}.jsonl")
            stream = cc_log.load_streams(path)["main"]
            acc.add_stream(stream["cwd"], stream["events"], True)
        m = acc.finish()["models"][MODEL]
        self.assertEqual({e: v["median_turn_seconds"] for e, v in m["by_effort"].items()}, {"high": 4.0, "max": 10.0})
        self.assertEqual(m["turns"], 50)

    def test_rare_effort_is_left_to_the_pooled_rates(self):
        acc = calibrate.Accumulator()
        path = effort_session(1_000_000.0, "medium", 6.0, turns=5).write(self.projects / "p" / "s.jsonl")
        stream = cc_log.load_streams(path)["main"]
        acc.add_stream(stream["cwd"], stream["events"], True)
        path2 = effort_session(2_000_000.0, "", 6.0, turns=20).write(self.projects / "p" / "s2.jsonl")
        stream = cc_log.load_streams(path2)["main"]
        acc.add_stream(stream["cwd"], stream["events"], True)
        self.assertEqual(acc.finish()["models"][MODEL]["by_effort"], {})


def rates_with_efforts() -> dict:
    rates = make_rates()
    rates["models"][MODEL]["by_effort"] = {
        "max": {"turns": 40, "median_turn_seconds": 20.0, "turn_seconds_by_ctx": {"0": [20.0] * 20, "25000": [20.0] * 20}},
        "high": {"turns": 40, "median_turn_seconds": 5.0, "turn_seconds_by_ctx": {"0": [5.0] * 20, "25000": [5.0] * 20}}}
    return rates


class Simulate(unittest.TestCase):
    def p50_seconds(self, effort: str) -> float:
        rates = rates_with_efforts()
        counts = {"scenarios": [{"reads": 2}], "worker_effort": effort}
        return estimate.simulate(counts, rates, rates["models"][MODEL], "c:/x")["p50"] * 60

    def test_estimate_follows_the_effort(self):
        # 2 reads -> 3 turns: + 2 s of reads
        self.assertAlmostEqual(self.p50_seconds("max"), 3 * 20 + 2)
        self.assertAlmostEqual(self.p50_seconds("high"), 3 * 5 + 2)

    def test_unknown_or_missing_effort_uses_all_efforts(self):
        self.assertAlmostEqual(self.p50_seconds("xhigh"), 3 * 10 + 2)
        self.assertAlmostEqual(self.p50_seconds(""), 3 * 10 + 2)
        rates = rates_with_efforts()
        res = estimate.simulate({"scenarios": [{}], "worker_effort": "xhigh"}, rates, rates["models"][MODEL], "c:/x")
        self.assertTrue(any("effort 'xhigh' not measured" in n for n in res["notes"]))


class Session(IsolatedTest):
    def write(self, folder: str, name: str, cwd: str, effort: str, model: str = MODEL, age: float = 0.0):
        tr = effort_session(time.time() - 3600, effort, 5.0, turns=2)
        tr.cwd = cwd
        for line in tr.lines:
            line["cwd"] = cwd
            if line["type"] == "assistant":
                line["message"]["model"] = model
        path = tr.write(self.projects / folder / name)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def test_newest_transcript_of_the_project_wins(self):
        self.write("C--Work-demo", "old.jsonl", r"C:\Work\demo", "high", age=500)
        self.write("C--Work-demo", "new.jsonl", r"C:\Work\demo", "max", model="claude-test-2", age=10)
        self.write("D--other", "newest.jsonl", r"D:\other", "low", age=0)        # another project
        self.assertEqual(cc_log.latest_turn_settings("/c/Work/demo"), {"model": "claude-test-2", "effort": "max",
                                                                       "ctx": 10_000})
        self.assertEqual(cc_log.latest_turn_settings("c:/nowhere"), {"model": "", "effort": "", "ctx": 0})

    def test_worker_continues_with_the_asking_sessions_context(self):
        path = self.write("C--Work-demo", "s.jsonl", r"C:\Work\demo", "max")
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
        for line in lines:
            if line["type"] == "assistant":
                line["message"]["usage"]["cache_read_input_tokens"] = 300_000
        path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
        (self.data / "rates.json").write_text(json.dumps(rates_with_efforts()), encoding="utf-8")
        for extra, expected in (({}, 300_010), ({"fresh_worker": True}, None), ({"start_context_tokens": 5}, 5)):
            counts = dict({"project": "/c/Work/demo", "scenarios": [{"reads": 1}]}, **extra)
            with mock.patch("sys.argv", ["estimate.py", "-"]), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps(counts))), contextlib.redirect_stdout(io.StringIO()):
                estimate.main()
            record = json.loads((self.data / "estimates.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["counts"].get("start_context_tokens"), expected, extra)

    def test_estimate_uses_the_asking_session(self):
        self.write("C--Work-demo", "s.jsonl", r"C:\Work\demo", "max")
        (self.data / "rates.json").write_text(json.dumps(rates_with_efforts()), encoding="utf-8")
        counts = {"task": "t", "project": "/c/Work/demo", "scenarios": [{"reads": 2}]}
        out = io.StringIO()
        with mock.patch("sys.argv", ["estimate.py", "-"]), mock.patch("sys.stdin", io.StringIO(json.dumps(counts))), \
                contextlib.redirect_stdout(out):
            estimate.main()
        record = json.loads((self.data / "estimates.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((record["model"], record["effort"]), (MODEL, "max"))
        self.assertIn("at effort max", out.getvalue())
        self.assertAlmostEqual(record["p50"], round((3 * 20 + 2) / 60, 2))

    def test_explicit_worker_settings_win(self):
        self.write("C--Work-demo", "s.jsonl", r"C:\Work\demo", "max")
        (self.data / "rates.json").write_text(json.dumps(rates_with_efforts()), encoding="utf-8")
        counts = {"project": "/c/Work/demo", "worker_model": "test", "worker_effort": "high", "scenarios": [{"reads": 2}]}
        with mock.patch("sys.argv", ["estimate.py", "-"]), mock.patch("sys.stdin", io.StringIO(json.dumps(counts))), \
                contextlib.redirect_stdout(io.StringIO()):
            estimate.main()
        record = json.loads((self.data / "estimates.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["effort"], "high")


if __name__ == "__main__":
    unittest.main()
