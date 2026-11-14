# test_compare.py - finding the session of an estimate, measuring what happened, scoring, report.
from __future__ import annotations

import contextlib
import io
import json
import time
import unittest
from datetime import datetime
from unittest import mock

from fixtures import IsolatedTest, Transcript
import compare

EST_ID = "E20260101-000000-abc"


def estimate_record(base: float) -> dict:
    scenarios = [{"name": "smooth", "weight": 0.3, "reads": 1, "edits": 1, "runs": {"python run.py": 1}},
                 {"name": "rough", "weight": 0.7, "reads": 8, "edits": 9, "fix_loops": 4,
                  "runs": {"python run.py": 1}}]
    return {"id": EST_ID, "ts": base, "project": "c:/work/demo", "model": "claude-test-1", "task": "t",
            "task_type": "bugfix", "counts": {"scenarios": scenarios}, "p10": 0.5, "p50": 1.5, "p90": 3.0,
            "median_turns": 9, "baseline": 0.8}


def worked_session(base: float, with_id: bool = True) -> Transcript:
    """'go' at +10, Read, failing Bash, Edit, passing Bash (ends +60), 100 s human wait, one more turn (+170)."""
    tr = Transcript(base)
    tr.human("go", gap=10)
    read = tr.tool("Read", file_path="a.py")
    tr.turn(read, dur=10)
    tr.result(read, gap=1, text=f"Estimate {EST_ID} | ..." if with_id else "ok")
    b1 = tr.tool("Bash", command="python run.py")
    tr.turn(b1, dur=9)
    tr.result(b1, gap=10, error=True)
    edit = tr.tool("Edit", new_string="x\ny")
    tr.turn(edit, dur=5)
    tr.result(edit, gap=1)
    b2 = tr.tool("Bash", command="python run.py")
    tr.turn(b2, dur=4)
    tr.result(b2, gap=10)
    tr.human("thanks, one more thing", gap=100)
    tr.turn(dur=10)
    return tr


class Compare(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.base = time.time() - 3600
        self.est = estimate_record(self.base)
        (self.data / "estimates.jsonl").write_text(json.dumps(self.est) + "\n", encoding="utf-8")
        self.session = self.projects / "C--Work-demo" / "sess1.jsonl"

    def run_main(self, *argv: str) -> str:
        out = io.StringIO()
        with mock.patch("sys.argv", ["compare.py", *argv]), contextlib.redirect_stdout(out):
            compare.main()
        return out.getvalue()

    def test_measure_excludes_human_wait(self):
        worked_session(self.base).write(self.session)
        main, side = compare.session_streams(self.session, self.base)
        act = compare.measure(main, side, self.base, float("inf"))
        self.assertAlmostEqual(act["active_min"] * 60, 60)
        self.assertAlmostEqual(act["human_min"] * 60, 100)
        self.assertAlmostEqual(act["model_min"] * 60, 10 + 9 + 5 + 4 + 10)
        self.assertEqual(act["turns"], 5)
        self.assertEqual(act["counts"], {"reads": 1, "searches": 0, "edits": 1, "other_calls": 0, "bookkeeping": 0,
                                         "commands": 2, "fix_loops": 1, "write_lines": 2})

    def test_piped_failure_counts_as_fix_loop_and_bookkeeping_is_separate(self):
        tr = Transcript(self.base)
        tr.human("go", gap=1)
        for text in ("FAILED (failures=1)", "OK"):
            todo = tr.tool("TaskUpdate", taskId="1", status="in_progress")
            run = tr.tool("Bash", command="python -m unittest discover -s tests 2>&1 | tail -5")
            tr.turn(todo, run, dur=3)
            tr.result(todo, gap=0.1)
            tr.result(run, gap=2, text=text)          # exit status 0: tail succeeded
            if text != "OK":
                fix = tr.tool("Edit", new_string="fixed")
                tr.turn(fix, dur=2)
                tr.result(fix, gap=0.5)
        tr.write(self.session)
        main, side = compare.session_streams(self.session, self.base)
        counts = compare.measure(main, side, self.base, float("inf"))["counts"]
        self.assertEqual((counts["fix_loops"], counts["bookkeeping"], counts["other_calls"]), (1, 2, 0))

    def test_report_accepts_rows_from_before_bookkeeping_field(self):
        old = {"estimate_id": "E-old", "task_type": "bugfix", "p10": 1, "p50": 2, "p90": 4, "active_min": 2,
               "predicted": {f: 1 for f in compare.FIELDS if f != "bookkeeping"},
               "actual": {f: 1 for f in compare.FIELDS if f != "bookkeeping"}}
        (self.data / "actuals.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
        self.assertIn("bookkeeping 1.00", " ".join(self.run_main("--report").split()))

    def test_until_cuts_off_later_activity(self):
        worked_session(self.base).write(self.session)
        main, side = compare.session_streams(self.session, self.base)
        act = compare.measure(main, side, self.base, self.base + 100)
        self.assertAlmostEqual(act["active_min"] * 60, 50)
        self.assertEqual(act["turns"], 4)

    def test_find_session_by_id(self):
        worked_session(self.base, with_id=False).write(self.projects / "C--Work-demo" / "other.jsonl")
        worked_session(self.base).write(self.session)
        self.assertEqual(compare.find_session(self.est), (self.session, True))

    def test_id_only_in_subagent_maps_to_parent_session(self):
        worked_session(self.base, with_id=False).write(self.session)
        side = Transcript(self.base + 15, sidechain=True, agent_id="a1")
        read = side.tool("Read", file_path="x")
        side.turn(read, dur=2)
        side.result(read, text=EST_ID)
        side.write(self.session.parent / "sess1" / "subagents" / "agent-a1.jsonl")
        self.assertEqual(compare.find_session(self.est), (self.session, True))
        main, sides = compare.session_streams(self.session, self.base)
        act = compare.measure(main, sides, self.base, float("inf"))
        self.assertEqual((act["subagent_turns"], act["subagent_calls"]), (1, 1))

    def test_guess_by_project_when_id_missing(self):
        worked_session(self.base, with_id=False).write(self.session)
        Transcript(self.base, cwd=r"D:\elsewhere").write(self.projects / "D--elsewhere" / "x.jsonl")
        self.assertEqual(compare.find_session(self.est), (self.session, False))

    def test_closest_scenario(self):
        worked_session(self.base).write(self.session)
        main, side = compare.session_streams(self.session, self.base)
        act = compare.measure(main, side, self.base, float("inf"))
        self.assertEqual(compare.closest_scenario(self.est, act["counts"]), "smooth")

    def test_main_scores_logs_and_rescoring_replaces(self):
        worked_session(self.base).write(self.session)
        printed = self.run_main("--id", EST_ID)
        self.assertIn("matched by estimate id", printed)
        self.assertIn("Actual AI time 1.0 min -> inside P10-P90", printed)
        self.assertIn("most like scenario 'smooth' (you gave it 30%)", printed)
        self.run_main()  # latest estimate, scored again
        rows = (self.data / "actuals.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1)
        row = json.loads(rows[0])
        self.assertEqual((row["main_scenario"], row["closest_scenario"], row["exact_session"]),
                         ("rough", "smooth", True))
        # an estimate from before self-correction: its raw numbers are the shown ones
        self.assertEqual((row["raw_p10"], row["raw_p50"], row["raw_p90"]), (0.5, 1.5, 3.0))

    def test_main_until_option_uses_local_time(self):
        worked_session(self.base).write(self.session)
        until = datetime.fromtimestamp(self.base + 100).isoformat(timespec="seconds")
        self.run_main("--until", until)
        row = json.loads((self.data / "actuals.jsonl").read_text(encoding="utf-8"))
        self.assertAlmostEqual(row["active_min"] * 60, 50, delta=1)

    def test_report(self):
        worked_session(self.base).write(self.session)
        self.run_main()
        printed = self.run_main("--report")
        self.assertIn("Comparisons: 1 | inside P10-P90: 1/1", printed)
        self.assertIn("Runs that did not follow the main scenario: 1/1", printed)
        self.assertIn("full model closer in", printed)

    def test_report_without_rows_exits(self):
        with self.assertRaises(SystemExit):
            self.run_main("--report")

    def test_unknown_id_exits(self):
        with self.assertRaises(SystemExit):
            self.run_main("--id", "E-nope")

    def test_work_before_a_later_human_message_is_counted(self):
        # In-session flow: the worker proceeds right after the estimate, a human message comes later.
        tr = Transcript(self.base)
        for _ in range(5):
            read = tr.tool("Read", file_path="a.py")
            tr.turn(read, dur=10)
            tr.result(read, gap=2)            # 60 s of work, no human message yet
        tr.human("follow-up question", gap=300)
        tr.turn(dur=5)
        tr.write(self.session)
        main, side = compare.session_streams(self.session, self.base)
        act = compare.measure(main, side, self.base, float("inf"))
        self.assertEqual(act["counts"]["reads"], 5)
        self.assertAlmostEqual(act["start"], self.base, places=3)
        self.assertAlmostEqual(act["human_min"] * 60, 300, places=3)
        self.assertAlmostEqual(act["active_min"] * 60, 65, places=3)

    def test_presenting_the_estimate_then_waiting_for_go_is_not_work(self):
        # Estimate shown (a text turn, maybe a task-list update), then a long wait, then "go".
        tr = Transcript(self.base)
        tr.turn(dur=8)
        todo = tr.tool("TaskUpdate", taskId="1", status="completed")
        tr.turn(todo, dur=2)
        tr.result(todo, gap=0.5)
        tr.human("go", gap=600)
        read = tr.tool("Read", file_path="a.py")
        tr.turn(read, dur=10)
        tr.result(read, gap=1)
        tr.write(self.session)
        main, side = compare.session_streams(self.session, self.base)
        act = compare.measure(main, side, self.base, float("inf"))
        self.assertAlmostEqual(act["start"], self.base + 10.5 + 600, places=3)
        self.assertAlmostEqual(act["active_min"] * 60, 11, places=3)
        self.assertEqual(act["counts"]["reads"], 1)


if __name__ == "__main__":
    unittest.main()
