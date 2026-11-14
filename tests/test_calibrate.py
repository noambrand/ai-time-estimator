# test_calibrate.py - rates measured from synthetic transcripts.
from __future__ import annotations

import contextlib
import io
import json
import time
import unittest
from unittest import mock

from fixtures import MODEL, IsolatedTest, Transcript
import calibrate
import cc_log


def work_session(base: float, loops: int = 5, big_writes: int = 5) -> Transcript:
    """`loops` x (Bash fails -> Read -> Edit -> Bash passes), then `big_writes` turns writing 40 lines."""
    tr = Transcript(base)
    tr.human("go")
    ctx = 20_000
    for _ in range(loops):
        for name, inp, err in (("Bash", {"command": "python run.py"}, True), ("Read", {"file_path": "a.py"}, False),
                               ("Edit", {"new_string": "x\ny"}, False), ("Bash", {"command": "python run.py"}, False)):
            tool = tr.tool(name, **inp)
            tr.turn(tool, dur=5.0, ctx=ctx, out=200)
            tr.result(tool, gap=4.0 if name == "Bash" else 1.0, error=err)
            ctx += 1000
    for _ in range(big_writes):
        tool = tr.tool("Write", content="\n".join(["line"] * 40))
        tr.turn(tool, dur=10.0, ctx=ctx, out=2000)
        tr.result(tool, gap=1.0)
        ctx += 1000
    return tr


class Accumulate(IsolatedTest):
    def finish(self, *transcripts: Transcript) -> dict:
        acc = calibrate.Accumulator()
        for i, tr in enumerate(transcripts):
            path = tr.write(self.projects / "p" / f"s{i}.jsonl")
            for name, stream in cc_log.load_streams(path).items():
                acc.add_stream(stream["cwd"], stream["events"], name == "main")
        return acc.finish()

    def test_model_rates(self):
        m = self.finish(work_session(1_000_000.0))["models"][MODEL]
        self.assertEqual(m["turns"], 25)
        self.assertEqual(m["calls_per_turn"], 1.0)
        self.assertEqual(m["median_turn_seconds"], 5.0)
        self.assertEqual(m["start_context_tokens"], 20_000)
        self.assertEqual(m["context_growth_per_turn"], 1000)
        self.assertEqual(m["tokens_per_second"], 200.0)        # 2000 tokens in 10 s
        self.assertEqual(m["tokens_per_written_line"], 50.0)   # 2000 tokens / 40 lines
        self.assertEqual(m["median_output_tokens"], 200)       # big writes excluded
        # Big-write turns are left out of the context buckets (timed by output speed instead).
        self.assertEqual(sum(len(v) for v in m["turn_seconds_by_ctx"].values()), 20)
        # all-in seconds per call: (20*5 + 5*10 turn s) + (10*4 + 15*1 tool s) over 25 calls
        self.assertEqual(m["seconds_per_call"], round((150 + 55) / 25, 2))

    def test_tools_commands_and_fix_loops(self):
        r = self.finish(work_session(1_000_000.0))
        self.assertEqual(r["tools"]["Read"]["median"], 1.0)
        self.assertEqual(r["tools"]["Read"]["n"], 5)
        self.assertNotIn("Bash", r["tools"])
        self.assertEqual(r["commands"]["c:/work/demo"]["python run.py"], [4.0] * 10)
        self.assertEqual(r["commands"]["*"]["python run.py"], [4.0] * 10)
        self.assertEqual(r["fix_loop_calls"], {"n": 5, "median": 2})

    def test_too_few_turns_drops_model(self):
        self.assertEqual(self.finish(work_session(1_000_000.0, loops=2, big_writes=0))["models"], {})

    def test_human_tools_slow_quick_tools_and_synthetic_model_ignored(self):
        tr = work_session(1_000_000.0)
        ask = tr.tool("AskUserQuestion", questions=[])
        tr.turn(ask, dur=2.0)
        tr.result(ask, gap=300.0)                     # human answer time, never a tool rate
        slow = tr.tool("Read", file_path="b.py")
        tr.turn(slow, dur=2.0)
        tr.result(slow, gap=120.0)                    # waited on a permission prompt
        tr.turn(dur=2.0, model="<synthetic>")
        r = self.finish(tr)
        self.assertNotIn("AskUserQuestion", r["tools"])
        self.assertEqual(r["tools"]["Read"]["n"], 5)
        self.assertEqual(list(r["models"]), [MODEL])

    def test_stalled_turn_not_a_sample(self):
        tr = work_session(1_000_000.0)
        tr.turn(dur=cc_log.STALL_SECONDS + 1)
        self.assertEqual(self.finish(tr)["models"][MODEL]["turns"], 25)

    def test_subagent_stream_is_not_a_session_start_nor_a_baseline_rate(self):
        side = work_session(1_000_000.0)
        side.extra = {"isSidechain": True, "agentId": "x"}
        for line in side.lines:
            line.update(side.extra)
        m = self.finish(side)["models"][MODEL]
        self.assertIsNone(m["start_context_tokens"])
        self.assertIsNone(m["seconds_per_call"])

    def test_parallel_calls_count_their_slowest_call_once(self):
        tr = work_session(1_000_000.0)
        a, b = tr.tool("Read", file_path="x"), tr.tool("Bash", command="sleep-ish job")
        tr.turn(a, b, dur=4.0)
        tr.result(a, gap=1.0)
        tr.result(b, gap=99.0)          # ran alongside the Read: 100 s after the call, not 101
        m = self.finish(tr)["models"][MODEL]
        self.assertAlmostEqual(m["seconds_per_call"], round((150 + 55 + 4 + 100) / 27, 2))

    def test_baseline_rate_is_median_over_sessions(self):
        fast, slow, slower = work_session(1_000_000.0), work_session(2_000_000.0), work_session(3_000_000.0)
        for tr, extra in ((slow, 500.0), (slower, 2000.0)):   # one long command in the session
            long_run = tr.tool("Bash", command="make all")
            tr.turn(long_run, dur=1.0)
            tr.result(long_run, gap=extra)
        m = self.finish(fast, slow, slower)["models"][MODEL]
        self.assertAlmostEqual(m["seconds_per_call"], round((150 + 55 + 1 + 500) / 26, 2))  # the middle one


class Main(IsolatedTest):
    def run_main(self, *argv: str) -> str:
        out = io.StringIO()
        with mock.patch("sys.argv", ["calibrate.py", *argv]), contextlib.redirect_stdout(out):
            calibrate.main()
        return out.getvalue()

    def test_writes_rates_json(self):
        work_session(time.time() - 5000).write(self.projects / "p" / "s.jsonl")
        printed = self.run_main()
        rates = json.loads((self.data / "rates.json").read_text(encoding="utf-8"))
        self.assertIn(MODEL, rates["models"])
        self.assertEqual((rates["days"], rates["files"]), (60, 1))
        self.assertIn(f"{MODEL}: 25 turns", printed)
        self.assertNotIn("FORMAT CHECK FAILED", printed)

    def test_days_window_excludes_old_activity(self):
        work_session(time.time() - 5 * 86400).write(self.projects / "p" / "s.jsonl")  # mtime is now
        self.run_main("--days", "1")
        rates = json.loads((self.data / "rates.json").read_text(encoding="utf-8"))
        self.assertEqual(rates["models"], {})

    def test_format_check_when_no_turns_recognized(self):
        tr = Transcript(time.time() - 100)
        tr.human("hello")
        tr.human("again", gap=5)
        tr.write(self.projects / "p" / "s.jsonl")
        self.assertIn("FORMAT CHECK FAILED: transcripts exist but no model turns", self.run_main())


if __name__ == "__main__":
    unittest.main()
