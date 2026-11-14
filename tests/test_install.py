# test_install.py - install.py options and a clean uninstall, all inside a temp "~/.claude".
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from fixtures import IsolatedTest
from test_autoscore import load_install


class Installer(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        self.install = load_install()
        self.install.claude = self.tmp / ".claude"
        self.install.data = self.install.claude / "ai-time-estimator"
        self.install.claude.mkdir()
        self.settings = self.install.claude / "settings.json"
        self.md = self.install.claude / "CLAUDE.md"
        self.other = {"type": "command", "command": "node other.js", "timeout": 15}
        self.settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [self.other]}]}}),
                                 encoding="utf-8")
        self.md.write_text("## Mine\n- keep me\n", encoding="utf-8")

    def run_main(self, *argv: str) -> None:
        with mock.patch.object(self.install.subprocess, "run") as calibrate, contextlib.redirect_stdout(io.StringIO()):
            self.install.main(list(argv))
        self.calibrate_calls = calibrate.call_count

    def test_full_install_then_uninstall_restores_the_users_files(self):
        self.run_main()
        self.assertEqual(self.calibrate_calls, 1)
        self.assertTrue((self.install.claude / "agents" / "time-estimator.md").exists())
        self.assertTrue((self.install.data / "estimate.py").exists())
        (self.install.data / "rates.json").write_text("{}", encoding="utf-8")       # the user's data
        self.run_main("--uninstall")
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings, {"model": "opus", "hooks": {"Stop": [{"hooks": [self.other]}]}})
        self.assertEqual(self.md.read_text(encoding="utf-8"), "## Mine\n- keep me\n")
        self.assertFalse((self.install.claude / "agents" / "time-estimator.md").exists())
        self.assertFalse((self.install.claude / "commands" / "estimate-here.md").exists())
        self.assertEqual([p.name for p in self.install.data.iterdir()], ["rates.json"])
        self.assertEqual(self.calibrate_calls, 0)

    def test_uninstall_purge_deletes_the_data_and_empty_hook_sections(self):
        self.settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        self.run_main()
        self.run_main("--uninstall", "--purge")
        self.assertFalse(self.install.data.exists())
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), {"model": "opus"})

    def test_opt_outs_leave_settings_and_claude_md_alone(self):
        before = (self.settings.read_text(encoding="utf-8"), self.md.read_text(encoding="utf-8"))
        self.run_main("--no-hook", "--no-rule")
        self.assertEqual((self.settings.read_text(encoding="utf-8"), self.md.read_text(encoding="utf-8")), before)
        self.assertTrue((self.install.data / "autoscore.py").exists())

    def test_rule_in_a_new_claude_md_has_no_leading_blank_lines(self):
        self.md.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.install.add_rule()
        self.assertTrue(self.md.read_text(encoding="utf-8").startswith(self.install.RULE_START))

    def test_hook_command_runs_in_bash_and_powershell(self):
        # forward-slash paths read the same on every OS (a backslash is no separator on Linux/macOS)
        with mock.patch.object(self.install.sys, "executable", "/opt/python/bin/python3"):
            self.assertTrue(self.install.hook_command().startswith('/opt/python/bin/python3 "'))
        with mock.patch.object(self.install.sys, "executable", "/opt/my python/bin/python3"):
            self.assertTrue(self.install.hook_command().startswith('"/opt/my python/bin/python3" "'))
        self.assertTrue(self.install.hook_command().endswith('autoscore.py"'))


if __name__ == "__main__":
    unittest.main()
