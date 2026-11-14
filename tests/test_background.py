# test_background.py - background work is timed from launch to its completion notice, and instant
# bookkeeping tools never get minute-long samples.
from __future__ import annotations

import unittest

from fixtures import IsolatedTest, Transcript
import calibrate
import cc_log


class Timing(IsolatedTest):
    def rates(self, tr: Transcript) -> dict:
        acc = calibrate.Accumulator()
        for name, stream in cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl")).items():
            acc.add_stream(stream["cwd"], stream["events"], name == "main")
        return acc.finish()

    def test_background_agent_lasts_until_its_notice(self):
        tr = Transcript(1_000_000.0)
        tr.human("go", typed=True)
        agent = tr.tool("Agent", prompt="research", run_in_background=True)
        tr.turn(agent, dur=3)
        tr.result(agent, gap=1, text="Agent launched in background")
        tr.turn(dur=2)
        tr.notification(agent, gap=300)
        tr.turn(dur=4)
        self.assertEqual(self.rates(tr)["tools"]["Agent"]["samples"], [round(1 + 2 + 300, 2)])

    def test_foreground_agent_lasts_until_its_result(self):
        tr = Transcript(1_000_000.0)
        agent = tr.tool("Agent", prompt="research")
        tr.turn(agent, dur=3)
        tr.result(agent, gap=95, text="final report")
        tr.notification(gap=5)                           # someone else's notice, no matching id
        self.assertEqual(self.rates(tr)["tools"]["Agent"]["samples"], [95.0])

    def test_background_command_lasts_until_its_notice(self):
        tr = Transcript(1_000_000.0)
        run = tr.tool("Bash", command="npm run build", run_in_background=True)
        tr.turn(run, dur=2)
        tr.result(run, gap=0.5, text="Command running in background")
        tr.notification(run, gap=40)
        self.assertEqual(self.rates(tr)["commands"]["*"].get("npm run build"), None)  # 1 sample: under the 2 kept
        acc = calibrate.Accumulator()
        stream = cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl"))["main"]
        acc.add_stream(stream["cwd"], stream["events"], True)
        self.assertEqual(acc.commands["*"]["npm run build"], [40.5])

    def test_named_agent_lasts_until_its_first_message_back(self):
        tr = Transcript(1_000_000.0)
        agent = tr.tool("Agent", prompt="estimate")
        agent["input"]["name"] = "estimator"
        tr.turn(agent, dur=3)
        tr.result(agent, gap=1, text="Spawned successfully.")
        reply = '<teammate-message teammate_id="estimator" color="blue">{"type":"idle_notification"}</teammate-message>'
        tr._add({"type": "user", "origin": {"kind": "peer"}, "promptSource": "system",
                 "message": {"role": "user", "content": "Another Claude session sent a message:\n" + reply}}, 180)
        tr._add({"type": "user", "origin": {"kind": "peer"}, "promptSource": "system",
                 "message": {"role": "user", "content": "Another Claude session sent a message:\n" + reply}}, 60)
        self.assertEqual(self.rates(tr)["tools"]["Agent"]["samples"], [181.0])   # the first reply ends it

    def test_bookkeeping_waits_are_not_tool_time(self):
        tr = Transcript(1_000_000.0)
        for gap in (0.4, 0.6, 200.0):                    # the 200 s one waited on something else
            todo = tr.tool("TaskUpdate", taskId="1", status="in_progress")
            tr.turn(todo, dur=2)
            tr.result(todo, gap=gap)
        self.assertEqual(sorted(self.rates(tr)["tools"]["TaskUpdate"]["samples"]), [0.4, 0.6])


if __name__ == "__main__":
    unittest.main()
