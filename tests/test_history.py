# test_history.py - past requests with their real time and counts, for the estimator to anchor on.
from __future__ import annotations

import contextlib
import io
import time
import unittest
from unittest import mock

from fixtures import IsolatedTest, Transcript
import history


class History(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        base = time.time() - 3600
        demo = Transcript(base)
        demo.human("<command-name>/fix</command-name> fix the login test", typed=True)
        for _ in range(2):
            edit = demo.tool("Edit", new_string="a\nb")
            demo.turn(edit, dur=10)
            demo.result(edit, gap=1)
        run = demo.tool("Bash", command="npm test")
        demo.turn(run, dur=5)
        demo.result(run, gap=20)
        demo.human("now update the README", gap=100, typed=True)
        write = demo.tool("Write", content="x")
        demo.turn(write, dur=15)
        demo.result(write, gap=1)
        demo.human("thanks", gap=50, typed=True)
        demo.turn(dur=3)                                  # 3 s: below the 10 s minimum
        demo.write(self.projects / "C--Work-demo" / "s1.jsonl")
        other = Transcript(base, cwd=r"D:\other")
        other.human("add a login test", typed=True)
        read = other.tool("Read", file_path="x")
        other.turn(read, dur=20)
        other.result(read, gap=1)
        other.write(self.projects / "D--other" / "s2.jsonl")

    def test_rows_of_one_project_newest_first(self):
        rows = history.past_rows("c:/work/demo")
        self.assertEqual([r["text"] for r in rows], ["now update the README", "/fix fix the login test"])
        fix = rows[1]
        self.assertEqual((fix["edits"], fix["commands"], fix["write_lines"]), (2, 1, 4))
        self.assertAlmostEqual(fix["minutes"] * 60, 2 * 11 + 5 + 20, places=3)

    def test_grep_and_everywhere(self):
        self.assertEqual([r["text"] for r in history.past_rows(None, words=["LOGIN", "test"])],
                         ["/fix fix the login test", "add a login test"])
        self.assertEqual(history.past_rows("c:/work/demo", words=["readme"])[0]["text"], "now update the README")

    def test_main_prints_a_table(self):
        out = io.StringIO()
        with mock.patch("sys.argv", ["history.py", "--project", "/c/Work/demo"]), contextlib.redirect_stdout(out):
            history.main()
        text = out.getvalue()
        self.assertIn("2 past requests in c:/work/demo", text)
        self.assertIn("fix the login test", text)
        out = io.StringIO()
        with mock.patch("sys.argv", ["history.py", "--project", "/c/nowhere"]), contextlib.redirect_stdout(out):
            history.main()
        self.assertIn("Try --everywhere", out.getvalue())


if __name__ == "__main__":
    unittest.main()
