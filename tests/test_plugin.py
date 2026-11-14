# test_plugin.py - the Claude Code plugin files: valid JSON, consistent names, and every path they
# point to exists. (`claude plugin validate .` is the full check; this keeps CI honest without Claude Code.)
from __future__ import annotations

import json
import re
import unittest

from fixtures import TOOLS_DIR

ROOT = TOOLS_DIR.parent


def load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


class PluginFiles(unittest.TestCase):
    def test_manifest_and_marketplace_agree(self):
        plugin = load(".claude-plugin/plugin.json")
        market = load(".claude-plugin/marketplace.json")
        self.assertRegex(plugin["name"], r"^[a-z0-9][a-z0-9-]*$")
        entry = market["plugins"][0]
        self.assertEqual((entry["name"], entry["source"]), (plugin["name"], "./"))
        self.assertEqual(market["owner"]["name"], plugin["author"]["name"])
        self.assertNotIn("version", entry)          # plugin.json owns the version

    def test_hooks_point_at_files_that_exist(self):
        hooks = load("hooks/hooks.json")["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "Stop"})
        for event in hooks.values():
            for group in event:
                for hook in group["hooks"]:
                    self.assertTrue(hook["async"], "the plugin's hooks must never block Claude Code")
                    for rel in set(re.findall(r'\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)', hook["command"])):
                        self.assertTrue((ROOT / rel).is_file(), rel)

    def test_hook_commands_follow_the_directory_rules(self):
        # Anthropic's directory blocks a hook command with any variable but ${CLAUDE_PLUGIN_ROOT}, a
        # command substitution, a wildcard or an inline program, and wants every path written from
        # ${CLAUDE_PLUGIN_ROOT} (so no /dev/null either).
        for event in load("hooks/hooks.json")["hooks"].values():
            for group in event:
                for hook in group["hooks"]:
                    bare = hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", "ROOT")
                    self.assertNotRegex(bare, r"\$|`|[*?]|\s-c\s|(?<!\w)/", hook["command"])

    def test_agent_and_command_live_where_the_plugin_loads_them(self):
        self.assertTrue((ROOT / "agents" / "time-estimator.md").is_file())
        command = (ROOT / "commands" / "estimate-here.md").read_text(encoding="utf-8")
        self.assertIn("~/.claude/ai-time-estimator/time-estimator.md", command)   # synced there in both modes

    def test_icon_is_a_square_png_of_at_least_512_px(self):
        head = (ROOT / ".claude-plugin" / "icon.png").read_bytes()[:24]
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        width, height = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        self.assertEqual(width, height)
        self.assertGreaterEqual(width, 512)


if __name__ == "__main__":
    unittest.main()
