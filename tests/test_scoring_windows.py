# test_scoring_windows.py - every estimate is scored on the window that holds its work, once:
# work done by a background agent on that agent's own transcript, work spread over sessions left unscored,
# a running window kept open (not learned from) until it closes, one log line per change, and rows marked
# invalid by hand ignored by the self-correction, the track record and the report.
from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from fixtures import IsolatedTest, Transcript
import autoscore
import compare
import estimate


def est(eid: str, ts: float, task: str = "t", p50: float = 1.0, **counts) -> dict:
    return {"id": eid, "ts": ts, "project": "c:/work/demo", "model": "claude-test-1", "task": task,
            "task_type": "other", "counts": dict({"scenarios": [{"reads": 5}]}, **counts),
            "p10": p50 / 2, "p50": p50, "p90": p50 * 2, "median_turns": 6}


class Windows(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.base = time.time() - 3600
        self.session = self.projects / "C--Work-demo" / "s1.jsonl"

    def write_estimates(self, *rows: dict) -> None:
        (self.data / "estimates.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def lead(self, *ids: str, then_human: bool = False) -> Transcript:
        """The lead shows the estimates, launches an agent in the background and ends its turn."""
        tr = Transcript(self.base)
        run = tr.tool("Bash", command="python estimate.py -")
        tr.turn(run, dur=1)
        tr.result(run, gap=0, text=" ".join(f"Estimate {i}" for i in ids))
        agent = tr.tool("Agent", prompt="fix it", run_in_background=True)
        agent["input"]["name"] = "fixer"
        tr.turn(agent, dur=3)
        tr.result(agent, gap=1, text="Spawned successfully.")
        tr.turn(dur=2)
        if then_human:
            tr.human("next thing", gap=600, typed=True)
            tr.turn(dur=2)
        tr.write(self.session)
        return tr

    def agent(self, name: str = "fixer", reads: int = 5, start: float = 10, finished: bool = True,
              agent_id: str = "a1", description: str = "Fix the parser") -> None:
        tr = Transcript(self.base + start, sidechain=True, agent_id=agent_id)
        tr.human(f'<teammate-message teammate_id="team-lead">{description}</teammate-message>')
        for _ in range(reads):
            read = tr.tool("Read", file_path="a.py")
            tr.turn(read, dur=9)
            tr.result(read, gap=1)
        folder = self.session.parent / self.session.stem / "subagents"
        tr.write(folder / f"agent-{agent_id}.jsonl")
        meta = {"agentType": "general-purpose", "name": name, "description": description, "finished": finished}
        (folder / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def actuals(self) -> dict:
        path = self.data / "actuals.jsonl"
        return {r["estimate_id"]: r for r in map(json.loads, path.read_text(encoding="utf-8").splitlines())} \
            if path.exists() else {}

    def run_hook(self) -> str:
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(self.session)}))), \
                contextlib.redirect_stdout(io.StringIO()):
            autoscore.main()
        log = self.data / "autoscore.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    # --- the right window ------------------------------------------------------------------------------
    def test_work_of_a_named_agent_is_measured_on_its_own_transcript(self):
        self.write_estimates(est("E-ag", self.base + 0.5, worker="agent:fixer"))
        self.lead("E-ag")
        self.agent()
        row = autoscore.score_session(self.session, now=self.base + 3000)[0]
        self.assertEqual((row["window"], row["status"], row["actual"]["reads"]), ("agent", "final", 5))
        self.assertAlmostEqual(row["active_min"] * 60, 50, delta=1)

    def test_estimate_named_after_the_agent_it_launched_uses_that_agent(self):
        self.write_estimates(est("E-name", self.base + 0.5, task="fixer"))
        self.lead("E-name")
        self.agent()
        row = autoscore.score_session(self.session, now=self.base + 3000)[0]
        self.assertEqual((row["window"], row["actual"]["reads"]), ("agent", 5))

    def test_agent_matched_by_launch_description_too(self):
        self.write_estimates(est("E-desc", self.base + 0.5, worker="agent:Fix the parser"))
        self.lead("E-desc")
        self.agent(name="")
        self.assertEqual(autoscore.score_session(self.session, now=self.base + 3000)[0]["window"], "agent")

    def test_named_agent_without_a_transcript_is_unscored_after_a_grace_period(self):
        self.write_estimates(est("E-gone", self.base + 0.5, worker="agent:nobody"))
        self.lead("E-gone")
        self.assertEqual(autoscore.score_session(self.session, now=self.base + 600), [])       # may still start
        row = autoscore.score_session(self.session, now=self.base + 2 * compare.UNMATCHED_GRACE_SECONDS)[0]
        self.assertEqual(row["status"], "unscored")
        self.assertIn("no transcript of agent 'nobody'", row["reason"])
        self.assertNotIn("active_min", row)

    def test_multi_session_work_is_unscored_unless_an_end_is_given(self):
        self.write_estimates(est("E-big", self.base + 0.5, p50=compare.MULTI_SESSION_MIN * 3),
                             est("E-days", self.base + 0.6, worker="multi_session"))
        self.lead("E-big", "E-days", then_human=True)
        rows = {r["estimate_id"]: r for r in autoscore.score_session(self.session, now=self.base + 900)}
        self.assertEqual({r["status"] for r in rows.values()}, {"unscored"})
        self.assertIn("spans several sessions", rows["E-big"]["reason"])

    def test_explicit_end_scores_a_multi_session_estimate(self):
        self.write_estimates(est("E-big", self.base + 0.5, p50=compare.MULTI_SESSION_MIN * 3))
        self.lead("E-big", then_human=True)
        until = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.base + 300))
        with mock.patch("sys.argv", ["compare.py", "--id", "E-big", "--until", until]), \
                contextlib.redirect_stdout(io.StringIO()):
            compare.main()
        self.assertEqual(self.actuals()["E-big"]["status"], "final")

    def test_estimates_for_different_agents_do_not_supersede_each_other(self):
        self.write_estimates(est("E-a", self.base + 0.5, worker="agent:fixer"),
                             est("E-b", self.base + 0.6, worker="agent:tester"))
        self.lead("E-a", "E-b")
        self.agent()
        self.agent(name="tester", agent_id="a2", description="Write the tests", reads=3)
        rows = {r["estimate_id"]: r for r in autoscore.score_session(self.session, now=self.base + 3000)}
        self.assertEqual((rows["E-a"]["actual"]["reads"], rows["E-b"]["actual"]["reads"]), (5, 3))

    def test_plain_estimate_still_uses_the_asking_session(self):
        self.write_estimates(est("E-own", self.base + 0.5))
        self.lead("E-own", then_human=True)
        self.agent()                                     # an agent exists, but the estimate does not name it
        row = autoscore.score_session(self.session, now=self.base + 3000)[0]
        self.assertEqual((row.get("window", "session"), row.get("status", "final"), row["actual"]["reads"]),
                         ("session", "final", 0))

    def test_rescore_command_moves_an_old_row_to_the_right_window(self):
        # A row stored by an older version measured the lead's own short window for an agent's work.
        self.write_estimates(est("E-old", self.base + 0.5, task="fixer"))
        self.lead("E-old", then_human=True)
        self.agent()
        old = {"estimate_id": "E-old", "session": str(self.session), "active_min": 0.1, "p10": 0.5, "p50": 1.0,
               "p90": 2.0, "predicted": {}, "actual": {}}
        (self.data / "actuals.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
        out = io.StringIO()
        later = self.base + 9 * 86400                    # long after the hook's 24 h window
        with mock.patch("sys.argv", ["compare.py", "--rescore"]), mock.patch("time.time", lambda: later), \
                contextlib.redirect_stdout(out):
            compare.main()
        row = self.actuals()["E-old"]
        self.assertEqual((row["window"], row["status"], row["actual"]["reads"]), ("agent", "final", 5))
        self.assertIn("E-old: final | agent window", out.getvalue())

    # --- once, quietly -----------------------------------------------------------------------------------
    def test_closed_window_is_scored_and_logged_once(self):
        self.write_estimates(est("E1", self.base + 0.5))
        self.lead("E1", then_human=True)
        self.write_rates()
        for _ in range(3):
            log = self.run_hook()
        self.assertEqual(log.count("scored E1"), 1)
        self.assertEqual(self.actuals()["E1"]["status"], "final")

    def test_open_window_updates_silently_until_it_closes(self):
        self.write_estimates(est("E1", self.base + 0.5))
        self.write_rates()
        tr = self.lead("E1")
        self.run_hook()
        log = self.run_hook()
        self.assertEqual((log.count("scored E1"), self.actuals()["E1"]["status"]), (1, "open"))
        tr.human("next thing", gap=600, typed=True)      # the user's next message closes the window
        tr.write(self.session)
        log = self.run_hook()
        self.assertEqual((log.count("scored E1"), self.actuals()["E1"]["status"]), (2, "final"))

    def test_open_window_of_a_quiet_session_becomes_final(self):
        self.write_estimates(est("E1", self.base + 0.5))
        self.lead("E1")
        autoscore.score_session(self.session, now=self.base + 900)
        quiet = time.time() - 2 * autoscore.QUIET_SECONDS
        os.utime(self.session, (quiet, quiet))
        rows = autoscore.finalize_quiet(time.time())
        self.assertEqual([(r["estimate_id"], r["status"]) for r in rows], [("E1", "final")])

    def test_running_agent_keeps_its_window_open(self):
        self.write_estimates(est("E-ag", self.base + 0.5, worker="agent:fixer"))
        self.lead("E-ag")
        self.agent(finished=False, start=3000)           # still writing its transcript
        row = autoscore.score_session(self.session, now=self.base + 3100)[0]
        self.assertEqual(row["status"], "open")


class Validity(IsolatedTest):
    def row(self, actual: float, p50: float = 1.0, **extra) -> dict:
        return dict({"estimate_id": f"E{actual}-{len(extra)}", "active_min": actual, "p10": p50 / 2, "p50": p50,
                     "p90": p50 * 2, "raw_p10": p50 / 2, "raw_p50": p50, "raw_p90": p50 * 2,
                     "predicted": {}, "actual": {}}, **extra)

    def test_correction_and_track_record_learn_only_from_final_rows(self):
        good = [self.row(2.0) for _ in range(12)]
        bad = [self.row(50.0, status="invalid") for _ in range(6)] + [self.row(40.0, status="open") for _ in range(6)]
        self.assertEqual(estimate.correction(good + bad)["shift"], 2.0)
        self.assertEqual(estimate.track_record(bad), "no scored estimates yet")
        self.assertEqual(estimate.correction(good)["shift"], 2.0)       # rows from before statuses: final

    def test_invalidate_command_and_report(self):
        rows = [self.row(1.0), dict(self.row(30.0), estimate_id="E-wrong")]
        (self.data / "actuals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        out = io.StringIO()
        with mock.patch("sys.argv", ["compare.py", "--invalidate", "E-wrong", "E-none", "--reason", "agent work"]), \
                contextlib.redirect_stdout(out):
            compare.main()
        self.assertIn("E-wrong: marked invalid", out.getvalue())
        self.assertIn("E-none: no scored row", out.getvalue())
        stored = {r["estimate_id"]: r for r in compare.read_jsonl(self.data / "actuals.jsonl")}
        self.assertEqual((stored["E-wrong"]["status"], stored["E-wrong"]["invalid_reason"]), ("invalid", "agent work"))
        self.assertEqual(stored["E-wrong"]["active_min"], 30.0)                         # kept for the record
        out = io.StringIO()
        with mock.patch("sys.argv", ["compare.py", "--report"]), contextlib.redirect_stdout(out):
            compare.main()
        self.assertIn("Comparisons: 1 | inside P10-P90: 1/1", out.getvalue())
        self.assertIn("Not counted: 1 invalid", out.getvalue())

    def test_rescoring_keeps_an_invalid_mark(self):
        (self.data / "actuals.jsonl").write_text(json.dumps(dict(self.row(30.0), estimate_id="E1", status="invalid",
                                                                 invalid_reason="x")) + "\n", encoding="utf-8")
        stored = compare.store_row(dict(self.row(1.0), estimate_id="E1", status="final"))
        self.assertEqual(stored["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
