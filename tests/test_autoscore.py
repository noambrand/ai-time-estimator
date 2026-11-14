# test_autoscore.py - the Stop hook that scores estimates without anyone running compare.py,
# and the installer steps that register it.
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from fixtures import IsolatedTest, Transcript, TOOLS_DIR
import autoscore

SCENARIO = {"scenarios": [{"name": "only", "reads": 1, "runs": {"python run.py": 1}}]}


def est(eid: str, ts: float) -> dict:
    return {"id": eid, "ts": ts, "project": "c:/work/demo", "model": "claude-test-1", "task": eid,
            "task_type": "other", "counts": SCENARIO, "p10": 0.1, "p50": 0.5, "p90": 1.0, "median_turns": 3}


def session_with_two_estimates(base: float) -> Transcript:
    """E1 shown at +0, 30 s of work; E2 shown at +100, 20 s of work."""
    tr = Transcript(base)
    for eid, start, work in (("E1", base, 3), ("E2", base + 100, 2)):
        tr.t = start
        run = tr.tool("Bash", command="python estimate.py -")
        tr.turn(run, dur=1)
        tr.result(run, gap=0, text=f"Estimate {eid} | P50 ...")
        for _ in range(work):
            read = tr.tool("Read", file_path="a.py")
            tr.turn(read, dur=9)
            tr.result(read, gap=1)
    return tr


class AutoScore(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.base = time.time() - 3600
        self.transcript = self.projects / "C--Work-demo" / "s1.jsonl"

    def write_estimates(self, *rows: dict) -> None:
        (self.data / "estimates.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def actuals(self) -> dict:
        path = self.data / "actuals.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return {r["estimate_id"]: r for r in map(json.loads, lines)}

    def test_superseded_estimate_is_not_scored(self):
        # The estimator re-ran estimate.py (Step 4) before anyone replied: only the second id counts.
        self.write_estimates(est("E1", self.base + 0.5), est("E2", self.base + 100.5))
        session_with_two_estimates(self.base).write(self.transcript)
        rows = autoscore.score_session(self.transcript, now=self.base + 500)
        self.assertEqual([r["estimate_id"] for r in rows], ["E2"])

    def test_each_estimate_window_ends_at_the_next_one(self):
        # E1 was made 1 s before its id shows up in the transcript; E2 likewise. A reply between them.
        self.write_estimates(est("E1", self.base + 0.5), est("E2", self.base + 100.5), est("E-other", self.base))
        tr = session_with_two_estimates(self.base)
        tr.lines.insert(8, dict(tr.lines[0], type="user", message={"role": "user", "content": "ok"},
                                timestamp=tr.lines[7]["timestamp"]))
        tr.write(self.transcript)
        rows = autoscore.score_session(self.transcript, now=self.base + 500)
        self.assertEqual([r["estimate_id"] for r in rows], ["E1", "E2"])
        got = self.actuals()
        self.assertEqual(set(got), {"E1", "E2"})
        self.assertEqual((got["E1"]["actual"]["reads"], got["E2"]["actual"]["reads"]), (3, 2))
        self.assertAlmostEqual(got["E1"]["active_min"] * 60, 30, delta=1)
        self.assertTrue(got["E1"]["exact_session"])

    def test_rescoring_replaces_and_old_estimates_are_final(self):
        self.write_estimates(est("E1", self.base + 0.5))
        session_with_two_estimates(self.base).write(self.transcript)
        autoscore.score_session(self.transcript, now=self.base + 500)
        autoscore.score_session(self.transcript, now=self.base + 600)
        self.assertEqual(len((self.data / "actuals.jsonl").read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(autoscore.score_session(self.transcript, now=self.base + 2 * 86400), [])

    def work(self, tr: Transcript, reads: int) -> None:
        for _ in range(reads):
            read = tr.tool("Read", file_path="a.py")
            tr.turn(read, dur=9)
            tr.result(read, gap=1)

    def test_next_user_message_ends_the_task(self):
        # Estimate, worker proceeds at once (3 reads), then a new request (5 more reads) - not this task.
        self.write_estimates(est("E1", self.base + 0.5))
        tr = Transcript(self.base)
        run = tr.tool("Bash", command="python estimate.py -")
        tr.turn(run, dur=1)
        tr.result(run, gap=0, text="Estimate E1")
        self.work(tr, 3)
        tr.human("now something else", gap=200)
        self.work(tr, 5)
        tr.write(self.transcript)
        row = autoscore.score_session(self.transcript, now=self.base + 900)[0]
        self.assertEqual(row["actual"]["reads"], 3)
        self.assertEqual(row["human_min"], 0)

    def test_wait_for_go_then_work_then_next_request(self):
        self.write_estimates(est("E1", self.base + 0.5))
        tr = Transcript(self.base)
        run = tr.tool("Bash", command="python estimate.py -")
        tr.turn(run, dur=1)
        tr.result(run, gap=0, text="Estimate E1")
        tr.turn(dur=3)                                   # shows the estimate, waits
        tr.human("go", gap=300)
        self.work(tr, 2)
        tr.human("thanks, next thing", gap=60)
        self.work(tr, 4)
        tr.write(self.transcript)
        row = autoscore.score_session(self.transcript, now=self.base + 900)[0]
        self.assertEqual(row["actual"]["reads"], 2)
        self.assertAlmostEqual(row["active_min"] * 60, 20, delta=1)

    def test_estimate_made_by_the_subagent_is_found(self):
        self.write_estimates(est("E-sub", self.base + 0.5))
        tr = Transcript(self.base)
        for _ in range(2):
            read = tr.tool("Read", file_path="a.py")
            tr.turn(read, dur=5)
            tr.result(read, gap=1)
        tr.write(self.transcript)
        side = Transcript(self.base, sidechain=True, agent_id="a1")
        run = side.tool("Bash", command="python estimate.py -")
        side.turn(run, dur=1)
        side.result(run, text="Estimate E-sub | ...")
        side.write(self.transcript.parent / "s1" / "subagents" / "agent-a1.jsonl")
        self.assertEqual([r["estimate_id"] for r in autoscore.score_session(self.transcript, now=self.base + 500)],
                         ["E-sub"])

    def test_main_is_silent_and_logs(self):
        self.write_estimates(est("E1", self.base + 0.5))
        session_with_two_estimates(self.base).write(self.transcript)
        self.write_rates()
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(self.transcript)}))), \
                contextlib.redirect_stdout(out):
            autoscore.main()
        self.assertEqual(out.getvalue(), "")
        self.assertIn("E1", self.actuals())
        self.assertIn("scored E1", (self.data / "autoscore.log").read_text(encoding="utf-8"))

    def test_main_never_raises(self):
        for stdin in ("", "not json", json.dumps({"transcript_path": str(self.tmp / "missing.jsonl")})):
            with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(io.StringIO()):
                autoscore.main()
        self.assertIn("JSONDecodeError", (self.data / "autoscore.log").read_text(encoding="utf-8"))

    def test_recalibrates_only_when_stale(self):
        self.assertTrue(autoscore.recalibrate_if_stale(time.time()))       # no rates.json yet
        self.assertTrue((self.data / "rates.json").exists())
        self.assertFalse(autoscore.recalibrate_if_stale(time.time()))      # fresh now


def load_install():
    spec = importlib.util.spec_from_file_location("install", TOOLS_DIR.parent / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Install(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.install = load_install()
        self.install.claude = self.tmp / ".claude"
        self.install.data = self.install.claude / "ai-time-estimator"
        self.install.claude.mkdir()
        self.settings = self.install.claude / "settings.json"
        self.other = {"type": "command", "command": "node other.js", "timeout": 15}
        self.settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [self.other]}]}}),
                                 encoding="utf-8")

    def stop_hooks(self) -> list[dict]:
        stop = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]["Stop"]
        return [h for group in stop for h in group["hooks"]]

    def test_register_hook_once_and_keep_other_settings(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.install.register_hook()
            self.install.register_hook()
        hooks = self.stop_hooks()
        self.assertEqual(hooks[0], self.other)
        auto = [h for h in hooks if "autoscore.py" in h["command"]]
        self.assertEqual(len(auto), 1)
        self.assertTrue(auto[0]["async"])
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))["model"], "opus")
        self.assertTrue(self.settings.with_suffix(".json.bak").exists())

    def test_register_hook_updates_a_moved_command(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.install.register_hook()
            settings = json.loads(self.settings.read_text(encoding="utf-8"))
            settings["hooks"]["Stop"][-1]["hooks"][0]["command"] = '"old/python" "old/autoscore.py"'
            self.settings.write_text(json.dumps(settings), encoding="utf-8")
            self.install.register_hook()
        auto = [h for h in self.stop_hooks() if "autoscore.py" in h["command"]]
        self.assertEqual(len(auto), 1)
        self.assertNotIn("old/", auto[0]["command"])

    def test_rule_added_once_and_replaced_in_place(self):
        md = self.install.claude / "CLAUDE.md"
        md.write_text("## Mine\n- keep me\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.install.add_rule()
            self.install.add_rule()
            text = md.read_text(encoding="utf-8")
            md.write_text(text.replace("Never give minutes", "OLD WORDING"), encoding="utf-8")
            self.install.add_rule()
        text = md.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("## Mine\n- keep me\n"))
        self.assertEqual(text.count(self.install.RULE_START), 1)
        self.assertNotIn("OLD WORDING", text)


if __name__ == "__main__":
    unittest.main()
