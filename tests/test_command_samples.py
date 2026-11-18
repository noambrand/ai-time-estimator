# test_command_samples.py - shell command samples hold run time only: a quick read-only command that took
# 30+ s waited (like a slow Read), and a call whose permission prompt a person answered, or that was denied,
# is no run-time sample at all.
from __future__ import annotations

import unittest

from fixtures import MODEL, IsolatedTest, Transcript, make_rates
import calibrate
import cc_log
import compare
import estimate

ASKED = {"permissionDecision": {"decision": "accept", "source": "user_temporary", "grantScope": "once"}}
RULE = {"permissionDecision": {"decision": "accept", "source": "config", "reasonType": "rule"}}
DENIED = {"permissionDecision": {"decision": "reject", "source": "config", "reasonType": "classifier"},
          "toolDenialKind": "automode-blocked"}


class QuickCommands(unittest.TestCase):
    def test_read_only_commands_are_quick(self):
        for command in ("ls -la", "git status", "git -C repo log --oneline | head -5", "grep -n foo a.py | wc -l",
                        "sed -n '1,5p' notes.txt", "cd sub && cat a.txt", 'grep -E "a|b" f.txt',
                        "FOO=1 git diff --stat", "find . -name '*.py' | sort"):
            self.assertTrue(cc_log.is_quick_command(command), command)

    def test_commands_that_do_work_are_not_quick(self):
        for command in ("python run.py", "npm test", "git commit -m x", "git push", "grep x f && python run.py",
                        "make all", "cat > f.py <<'EOF'\nimport os\nEOF\npython f.py", "", "cd sub"):
            self.assertFalse(cc_log.is_quick_command(command), command)

    def test_loops_and_ifs_around_read_only_steps_are_quick(self):
        for command in ('for d in a b; do ls "$d"; done', "if [ -f x ]; then cat x; fi",
                        "date +%H; for k in x y; do git -C $k log -1; done"):
            self.assertTrue(cc_log.is_quick_command(command), command)
        for command in ("for f in *.py; do python $f; done", "X=$(python run.py)", "echo `make`"):
            self.assertFalse(cc_log.is_quick_command(command), command)

    def test_coordination_tools_are_instant(self):
        for name in ("SendMessage", "ListAgents", "TaskStop", "Skill", "Read", "TaskUpdate"):
            self.assertTrue(cc_log.is_instant({"name": name, "input": {}}), name)
        for name in ("Agent", "WebFetch", "Workflow", "mcp__x__y"):
            self.assertFalse(cc_log.is_instant({"name": name, "input": {}}), name)


class Samples(IsolatedTest):
    def accumulate(self, tr: Transcript) -> calibrate.Accumulator:
        acc = calibrate.Accumulator()
        for name, stream in cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl")).items():
            acc.add_stream(stream["cwd"], stream["events"], name == "main")
        return acc

    def run_command(self, tr: Transcript, command: str, gap: float, **line) -> None:
        tool = tr.tool("Bash", command=command)
        tr.turn(tool, dur=2)
        tr.result(tool, gap=gap, **line)

    def test_slow_quick_command_is_a_wait_not_a_sample(self):
        tr = Transcript(1_000_000.0)
        for gap in (0.4, 1.5, 240.0):                       # the 240 s run waited on something else
            self.run_command(tr, "ls -la", gap)
        self.assertEqual(sorted(round(v, 2) for v in self.accumulate(tr).commands["*"]["ls -la"]), [0.4, 1.5])

    def test_slow_real_command_keeps_its_sample(self):
        tr = Transcript(1_000_000.0)
        self.run_command(tr, "python build.py", 240.0)
        self.assertEqual(self.accumulate(tr).commands["*"]["python build.py"], [240.0])

    def test_permission_prompt_and_denial_drop_the_sample_for_any_tool(self):
        tr = Transcript(1_000_000.0)
        self.run_command(tr, "python build.py", 300.0, **ASKED)    # waited for a person to click "yes"
        self.run_command(tr, "python build.py", 20.0, **DENIED)    # never ran
        self.run_command(tr, "python build.py", 45.0, **RULE)      # allowed by a settings rule: a real run
        read = tr.tool("Read", file_path="a.py")
        tr.turn(read, dur=2)
        tr.result(read, gap=8.0, **ASKED)
        acc = self.accumulate(tr)
        self.assertEqual(acc.commands["*"]["python build.py"], [45.0])
        self.assertNotIn("Read", acc.tools)

    def test_slow_message_to_a_teammate_is_a_wait_not_a_sample(self):
        tr = Transcript(1_000_000.0)
        for gap in (1.0, 3.0, 200.0):
            send = tr.tool("SendMessage", to="worker", message="status?")
            tr.turn(send, dur=2)
            tr.result(send, gap=gap)
        self.assertEqual(sorted(self.accumulate(tr).tools["SendMessage"]), [1.0, 3.0])

    def test_shell_runs_are_pooled_as_quick_or_other(self):
        tr = Transcript(1_000_000.0)
        for i in range(calibrate.MIN_POOL_SAMPLES):
            self.run_command(tr, f"ls dir{i}", 0.5)
            self.run_command(tr, f"python job{i}.py", 12.0)
        pools = self.accumulate(tr).finish()["command_pools"]
        self.assertEqual((set(pools["quick"]), set(pools["other"])), ({0.5}, {12.0}))

    def test_result_events_carry_the_permission_flags(self):
        tr = Transcript(1_000_000.0)
        self.run_command(tr, "python a.py", 1.0, **ASKED)
        self.run_command(tr, "python b.py", 1.0, **DENIED)
        self.run_command(tr, "python c.py", 1.0, **RULE)
        events = cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl"))["main"]["events"]
        flags = [(r["prompted"], r["denied"]) for e in events if e["kind"] == "result" for r in e["results"]]
        self.assertEqual(flags, [(True, False), (False, True), (False, False)])


class UnmeasuredCommands(unittest.TestCase):
    def rates(self) -> dict:
        rates = make_rates()
        rates["command_pools"] = {"quick": [0.5] * 20, "other": [6.0] * 20}
        return rates

    def test_unmeasured_command_draws_from_the_measured_pool(self):
        samples, source = estimate.command_samples(self.rates(), "c:/x", "python new_tool.py", None)
        self.assertEqual((set(samples), "drawn from 20 measured other commands" in source), ({6.0}, True))
        samples, _ = estimate.command_samples(self.rates(), "c:/x", "wc -l new_file.txt", None)
        self.assertEqual(set(samples), {0.5})
        res = estimate.simulate({"commands": [{"cmd": "python new_tool.py"}],
                                 "scenarios": [{"runs": {"python new_tool.py": 1}}]},
                                self.rates(), self.rates()["models"][MODEL], "c:/x")
        self.assertAlmostEqual(res["p50"] * 60, 2 * 10 + 6)            # was 2 turns + a flat 30 s

    def test_callers_fallback_and_measured_keys_still_win(self):
        self.assertEqual(estimate.command_samples(self.rates(), "c:/x", "make all", 7)[0], [7.0])
        self.assertEqual(estimate.command_samples(self.rates(), "c:/x", "python run.py -v", 7)[0], [30.0] * 3)

    def test_without_pools_the_flat_default_stays(self):
        self.assertEqual(estimate.command_samples(make_rates(), "c:/x", "make all", None)[0], [30.0])


class ActiveTime(IsolatedTest):
    def test_quick_command_waiting_30s_is_a_human_wait_but_a_real_run_is_not(self):
        tr = Transcript(1_000_000.0)
        tr.human("go", typed=True)
        quick = tr.tool("Bash", command="git status")
        tr.turn(quick, dur=2)
        tr.result(quick, gap=45)                           # 45 s for git status: an approval wait
        build = tr.tool("Bash", command="python build.py")
        tr.turn(build, dur=2)
        tr.result(build, gap=45)                           # a real 45 s build
        tr.turn(dur=2)
        events = cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl"))["main"]["events"]
        act = compare.measure(events, [], 1_000_000.0, float("inf"))
        self.assertAlmostEqual(act["human_min"] * 60, 45, places=3)
        self.assertAlmostEqual(act["active_min"] * 60, 2 + 2 + 45 + 2, places=3)


if __name__ == "__main__":
    unittest.main()
