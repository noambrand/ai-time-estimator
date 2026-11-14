# test_cc_log.py - transcript parsing, command keys, path normalization, fix-loop detection.
from __future__ import annotations

import unittest

from fixtures import CWD, MODEL, IsolatedTest, Transcript
import cc_log


class NormPath(unittest.TestCase):
    def test_windows_git_bash_and_wsl_forms_match(self):
        forms = [r"C:\Users\X\Proj", "C:/Users/X/Proj/", "/c/Users/X/Proj", "/mnt/c/Users/X/Proj"]
        self.assertEqual({cc_log.norm_path(p) for p in forms}, {"c:/users/x/proj"})

    def test_drive_root_and_empty(self):
        self.assertEqual(cc_log.norm_path("/c"), "c:")
        self.assertEqual(cc_log.norm_path(""), "")
        self.assertEqual(cc_log.norm_path(None), "")


class CommandKeys(unittest.TestCase):
    def test_three_then_two_words(self):
        self.assertEqual(cc_log.command_keys("python -m pytest -q tests"), ["python -m pytest", "python -m"])

    def test_skips_cd_export_and_env_assignments(self):
        cmd = "cd /x && export A=1; FOO=1 BAR=2 npm run build"
        self.assertEqual(cc_log.command_keys(cmd), ["npm run build", "npm run"])

    def test_drops_redirections_and_quotes(self):
        self.assertEqual(cc_log.command_keys('"python" run.py 2>&1'), ["python run.py"])

    def test_pipeline_uses_first_real_step(self):
        self.assertEqual(cc_log.command_keys("git log --oneline | head -5"), ["git log --oneline", "git log"])

    def test_single_word_and_empty(self):
        self.assertEqual(cc_log.command_keys("ls"), ["ls"])
        self.assertEqual(cc_log.command_keys(""), [])

    def test_case_insensitive(self):
        self.assertEqual(cc_log.command_keys("Python -I -c 'x'")[0], "python -i -c")


class WrittenLines(unittest.TestCase):
    def test_write_edit_multiedit_notebook(self):
        self.assertEqual(cc_log.written_lines({"input": {"content": "a\nb\nc"}}), 3)
        self.assertEqual(cc_log.written_lines({"input": {"new_string": "x"}}), 1)
        self.assertEqual(cc_log.written_lines({"input": {"edits": [{"new_string": "a\nb"}, {"new_string": "c"}]}}), 3)
        self.assertEqual(cc_log.written_lines({"input": {"new_source": "a\nb"}}), 2)

    def test_empty_and_malformed(self):
        self.assertEqual(cc_log.written_lines({"input": {"new_string": ""}}), 0)
        self.assertEqual(cc_log.written_lines({"input": "not a dict"}), 0)
        self.assertEqual(cc_log.written_lines({}), 0)


class ToolCategory(unittest.TestCase):
    def test_categories(self):
        got = {n: cc_log.tool_category(n) for n in ("Read", "Glob", "MultiEdit", "Bash", "WebFetch", "TaskUpdate")}
        self.assertEqual(got, {"Read": "reads", "Glob": "searches", "MultiEdit": "edits",
                               "Bash": "commands", "WebFetch": "other_calls", "TaskUpdate": "bookkeeping"})


class PipedFailures(unittest.TestCase):
    FAILED_OUTPUTS = [
        "Ran 3 tests\n\nFAILED (failures=1)",                                   # unittest
        "===== 1 failed, 4 passed in 0.2s =====",                              # pytest
        "Traceback (most recent call last):\n  File \"x.py\"\nKeyError: 'a'",  # python script
        "--- FAIL: TestX (0.00s)\nFAIL\tpkg 0.1s",                             # go test
        "Tests:       2 failed, 10 passed, 12 total",                          # jest
        "src/a.ts(3,1): error TS2304: Cannot find name 'x'.",                  # tsc
        "error[E0308]: mismatched types",                                       # rustc
        "npm ERR! code ELIFECYCLE",
        "TypeError: undefined is not a function\n    at main (a.js:1:1)",      # node
    ]
    PASSED_OUTPUTS = ["Ran 3 tests\n\nOK (expected failures=2)", "===== 0 failed, 5 passed =====",
                      "5 passing", "built in 2.1s", ""]

    def test_failures_seen_through_a_pipe(self):
        for text in self.FAILED_OUTPUTS:
            self.assertTrue(cc_log.looks_failed("npm test 2>&1 | tail -20", False, text), text)

    def test_passing_output_through_a_pipe(self):
        for text in self.PASSED_OUTPUTS:
            self.assertFalse(cc_log.looks_failed("npm test | tail", False, text), text)

    def test_unmasked_command_trusts_the_exit_status(self):
        self.assertFalse(cc_log.looks_failed("python run.py", False, "Traceback (most recent call last):"))
        self.assertFalse(cc_log.looks_failed("a && b", False, "FAILED (failures=1)"))
        self.assertTrue(cc_log.looks_failed("python run.py", True, ""))

    def test_other_masking_forms(self):
        for cmd in ("pytest; echo done", "pytest || true"):
            self.assertTrue(cc_log.looks_failed(cmd, False, "1 failed"), cmd)

    def test_tracker_sees_piped_failure(self):
        tr = cc_log.RetryTracker()
        cmd = {"name": "Bash", "input": {"command": "python -m unittest 2>&1 | tail -3"}}
        tr.on_call(dict(cmd, id="a"))
        tr.on_result("a", False, "FAILED (failures=1)")
        tr.on_call({"id": "e", "name": "Edit", "input": {}})
        self.assertEqual(tr.on_call(dict(cmd, id="b")), 1)

    def test_result_text_forms(self):
        self.assertEqual(cc_log.result_text({"content": "abc"}), "abc")
        self.assertEqual(cc_log.result_text({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}),
                         "a\nb")
        self.assertEqual(cc_log.result_text({}), "")
        self.assertEqual(len(cc_log.result_text({"content": "x" * 10_000})), cc_log.RESULT_TEXT_CHARS)


class Events(IsolatedTest):
    def test_streamed_chunks_merge_into_one_turn(self):
        tr = Transcript(1_000_000.0)
        tr.human("hi")
        read = tr.tool("Read", file_path="a.py")
        tr.turn(read, dur=6.0, ctx=30_000, out=400, chunks=3)
        tr.result(read, gap=0.5)
        events = cc_log.load_streams(tr.write(self.projects / "p" / "s.jsonl"))["main"]["events"]
        self.assertEqual([e["kind"] for e in events], ["human", "turn", "result"])
        turn = events[1]
        self.assertEqual(turn["out"], 400)          # highest output_tokens of the chunks
        self.assertEqual(turn["ctx"], 30_000)
        self.assertAlmostEqual(turn["end"] - turn["start"], 6.0, places=3)
        self.assertEqual([t["name"] for t in turn["tools"]], ["Read"])
        self.assertEqual(turn["model"], MODEL)

    def test_sidechain_split_since_filter_meta_and_partial_line(self):
        main = Transcript(1_000_000.0)
        main.human("old")                     # before since_ts -> dropped
        main.human("new", gap=100)
        main.human("<meta>", gap=1, meta=True)  # isMeta -> no human event
        main.turn(dur=2)
        side = Transcript(1_000_050.0, sidechain=True, agent_id="abc")
        side.turn(dur=3)
        main.lines += side.lines
        path = main.write(self.projects / "p" / "s.jsonl", partial_tail=True)
        streams = cc_log.load_streams(path, since_ts=1_000_050.0)
        self.assertEqual(set(streams), {"main", "side:abc"})
        self.assertEqual([e["kind"] for e in streams["main"]["events"]], ["human", "turn"])
        self.assertEqual(streams["main"]["cwd"], CWD)
        self.assertEqual([e["kind"] for e in streams["side:abc"]["events"]], ["turn"])

    def test_find_transcripts_filters_by_mtime(self):
        Transcript(1.0).write(self.projects / "p" / "a.jsonl")
        self.assertEqual(len(cc_log.find_transcripts(0)), 1)
        self.assertEqual(cc_log.find_transcripts(4_000_000_000), [])


class Retry(unittest.TestCase):
    def test_loop_size_is_calls_between_failure_and_rerun(self):
        tr = cc_log.RetryTracker()
        bash = lambda i: {"id": i, "name": "Bash", "input": {"command": "python run.py"}}
        other = lambda i: {"id": i, "name": "Edit", "input": {}}
        self.assertIsNone(tr.on_call(bash("b1")))
        tr.on_result("b1", error=True)
        tr.on_call(other("e1"))
        tr.on_call(other("e2"))
        self.assertEqual(tr.on_call(bash("b2")), 2)
        tr.on_result("b2", error=False)
        self.assertIsNone(tr.on_call(bash("b3")))  # success clears it: no loop

    def test_rerun_with_different_args_same_key_counts(self):
        tr = cc_log.RetryTracker()
        tr.on_call({"id": "a", "name": "Bash", "input": {"command": "pytest tests -x"}})
        tr.on_result("a", True)
        tr.on_call({"id": "e", "name": "Write", "input": {}})
        self.assertEqual(tr.on_call({"id": "b", "name": "Bash", "input": {"command": "pytest tests -x -k foo"}}), 1)
        # The key is the first 3 words, so a different 3rd word is a different command, not a rerun.
        tr.on_result("b", True)
        tr.on_call({"id": "e2", "name": "Edit", "input": {}})
        self.assertIsNone(tr.on_call({"id": "c", "name": "Bash", "input": {"command": "pytest tests -k foo"}}))

    def test_retry_without_an_edit_is_not_a_fix_loop(self):
        tr = cc_log.RetryTracker()
        bash = lambda i: {"id": i, "name": "Bash", "input": {"command": "npm run build"}}
        tr.on_call(bash("a"))
        tr.on_result("a", True)
        self.assertIsNone(tr.on_call(bash("b")))                          # immediate retry
        tr.on_result("b", True)
        tr.on_call({"id": "r", "name": "Read", "input": {}})
        tr.on_call({"id": "g", "name": "Grep", "input": {}})
        self.assertIsNone(tr.on_call(bash("c")))                          # looked around, changed nothing
        tr.on_result("c", True)
        tr.on_call({"id": "e", "name": "Edit", "input": {}})
        self.assertEqual(tr.on_call(bash("d")), 1)                        # edited, then reran: a fix loop

    def test_edit_issued_in_parallel_with_the_failing_run_is_not_the_fix(self):
        tr = cc_log.RetryTracker()
        bash = lambda i: {"id": i, "name": "Bash", "input": {"command": "make test"}}
        tr.on_call(bash("a"))
        tr.on_call({"id": "e", "name": "Edit", "input": {}})              # same turn, before the result
        tr.on_result("a", True)
        self.assertIsNone(tr.on_call(bash("b")))


if __name__ == "__main__":
    unittest.main()
