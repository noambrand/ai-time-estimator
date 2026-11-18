# test_classify.py - user-type transcript lines the user did not write (background-task notices,
# compaction summaries, local command output) must not count as human messages: no human wait before
# them, never the "go", never the end of a task.
from __future__ import annotations

import json
import time
import unittest

from fixtures import IsolatedTest, Transcript
import autoscore
import cc_log
import compare


def events_of(tr: Transcript, path) -> list[dict]:
    return cc_log.load_streams(tr.write(path))["main"]["events"]


class Classify(IsolatedTest):
    def kinds(self, tr: Transcript) -> list[str]:
        return [e["kind"] for e in events_of(tr, self.projects / "p" / "s.jsonl")]

    def test_typed_and_old_style_prompts_are_human(self):
        tr = Transcript(1_000_000.0)
        tr.human("fix the bug", typed=True)
        tr.human("older version, no origin field", gap=5)
        tr.human("<command-name>/model</command-name>", gap=5)       # a slash command the user typed
        tr.human("[Request interrupted by user]", gap=5)
        events = events_of(tr, self.projects / "p" / "s.jsonl")
        self.assertEqual([e["kind"] for e in events], ["human"] * 4)
        self.assertEqual(events[0]["text"], "fix the bug")

    def test_notices_summaries_and_local_output_are_system(self):
        tr = Transcript(1_000_000.0)
        agent = tr.tool("Agent", prompt="x", run_in_background=True)
        tr.notification(agent)
        tr.compact(gap=1)
        for text in ("<local-command-stdout>Set model to Opus</local-command-stdout>",
                     "<local-command-caveat>Caveat: ...</local-command-caveat>", "<bash-stdout>ok</bash-stdout>"):
            tr.human(text, gap=1)
        events = events_of(tr, self.projects / "p" / "s.jsonl")
        self.assertEqual([e["kind"] for e in events], ["system"] * 5)
        self.assertEqual(events[0]["tool_use_id"], agent["id"])
        self.assertIsNone(events[1]["tool_use_id"])

    def test_notice_marked_only_by_its_text_is_system(self):
        tr = Transcript(1_000_000.0)
        tr.human("<task-notification> <tool-use-id>toolu_9</tool-use-id> </task-notification>")
        events = events_of(tr, self.projects / "p" / "s.jsonl")
        self.assertEqual((events[0]["kind"], events[0]["tool_use_id"]), ("system", "toolu_9"))

    def test_origin_human_wins_over_text(self):
        tr = Transcript(1_000_000.0)
        tr.human("<bash-stdout>pasted by the user</bash-stdout>", typed=True)
        self.assertEqual(self.kinds(tr), ["human"])


class Effects(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.base = time.time() - 3600
        self.session = self.projects / "C--Work-demo" / "s1.jsonl"

    def background_session(self) -> Transcript:
        """go -> launch a background agent -> 120 s until its notice -> 2 reads."""
        tr = Transcript(self.base)
        tr.human("go", gap=1, typed=True)
        agent = tr.tool("Agent", prompt="research", run_in_background=True)
        tr.turn(agent, dur=3)
        tr.result(agent, gap=1, text="Agent launched in background")
        tr.turn(dur=2)                                   # "waiting for the agent"
        tr.notification(agent, gap=120)
        for _ in range(2):
            read = tr.tool("Read", file_path="a.py")
            tr.turn(read, dur=5)
            tr.result(read, gap=1)
        return tr

    def test_waiting_for_a_background_agent_is_not_active_time(self):
        # The session launched the agent, ended its turn and sat idle until the notice: a wait, not work.
        self.background_session().write(self.session)
        main, side = compare.session_streams(self.session, self.base)
        act = compare.measure(main, side, self.base, float("inf"))
        self.assertEqual(act["human_min"], 0)
        self.assertAlmostEqual(act["background_wait_min"] * 60, 120, places=3)
        self.assertAlmostEqual(act["active_min"] * 60, 3 + 1 + 2 + 2 * 6, places=3)
        self.assertEqual((act["counts"]["other_calls"], act["counts"]["reads"]), (1, 2))
        self.assertEqual((act["background"], act["tools"]), ({"Agent": 1}, {}))

    def test_a_notice_is_not_the_go(self):
        tr = Transcript(self.base)
        tr.turn(dur=4)                                   # the estimate is shown
        tr.notification(gap=30)                          # some earlier background task ends
        tr.human("go", gap=300, typed=True)
        read = tr.tool("Read", file_path="a.py")
        tr.turn(read, dur=5)
        tr.result(read, gap=1)
        tr.write(self.session)
        main, _ = compare.session_streams(self.session, self.base)
        self.assertAlmostEqual(compare.work_start(main, self.base, float("inf")), self.base + 4 + 30 + 300, places=3)

    def test_a_notice_does_not_end_the_task(self):
        est = {"id": "E-bg", "ts": self.base + 0.5, "project": "c:/work/demo", "model": "claude-test-1",
               "task": "t", "task_type": "other", "p10": 1, "p50": 2, "p90": 4, "median_turns": 5,
               "counts": {"scenarios": [{"reads": 2}]}}
        (self.data / "estimates.jsonl").write_text(json.dumps(est) + "\n", encoding="utf-8")
        tr = self.background_session()
        tr.lines[0]["message"]["content"] = "go - estimate E-bg was shown above"
        tr.write(self.session)
        row = autoscore.score_session(self.session, now=self.base + 900)[0]
        self.assertEqual(row["actual"]["reads"], 2)      # work after the notice still belongs to the task
        self.assertEqual(row["human_min"], 0)

    def test_compaction_is_not_a_human_wait(self):
        tr = Transcript(self.base)
        tr.human("go", gap=1, typed=True)
        tr.turn(dur=5)
        tr.compact(gap=40)                               # compaction takes model time, not user time
        tr.turn(dur=5)
        tr.write(self.session)
        main, side = compare.session_streams(self.session, self.base)
        act = compare.measure(main, side, self.base, float("inf"))
        self.assertEqual(act["human_min"], 0)
        self.assertAlmostEqual(act["active_min"] * 60, 50, places=3)


if __name__ == "__main__":
    unittest.main()
