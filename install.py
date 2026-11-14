# [UPDATED v1.3] install.py - copy the time estimator into ~/.claude, register the automatic-scoring
# Stop hook, add the "how to answer a time question" rule to ~/.claude/CLAUDE.md, and calibrate.
# Run it with the same Python environment Claude Code runs in (Windows or WSL). Safe to run again.
# Usage: python install.py [--no-hook] [--no-rule]      python install.py --uninstall [--purge]
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

src = Path(__file__).resolve().parent
claude = Path.home() / ".claude"
data = claude / "ai-time-estimator"

AGENT, COMMAND = Path("agents") / "time-estimator.md", Path("commands") / "estimate-here.md"
HOOK_SCRIPT = "autoscore.py"
RULE_START, RULE_END = "<!-- ai-time-estimator:start -->", "<!-- ai-time-estimator:end -->"
RULE = f"""{RULE_START}
## Time estimates
- When the user asks how long a task will take, delegate to the `time-estimator` agent (or run `/estimate-here` when the work should continue in this session). Never give minutes from your own judgment.
- Answer with its one-line result first ("About N min, likely A-B min"), then at most a few short lines on what drives the range.
- Scoring is automatic (Stop hook `autoscore.py`). Never ask the user to run estimator scripts.
{RULE_END}
"""
RULE_BLOCK = re.compile(r"\n*" + re.escape(RULE_START) + r".*?" + re.escape(RULE_END) + r"\n?", re.DOTALL)


def copy_files() -> None:
    for folder in (claude / "agents", claude / "commands", data):
        folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / AGENT, claude / AGENT)
    shutil.copy2(src / COMMAND, claude / COMMAND)
    for script in (src / "tools").glob("*.py"):
        shutil.copy2(script, data / script.name)
    print(f"Installed agent, /estimate-here and scripts -> {data}")


def hook_command() -> str:
    """Runs in bash and in PowerShell alike: a bare executable path (PowerShell would treat a quoted one
    as a string), quoted only when it contains spaces."""
    exe = Path(sys.executable).as_posix()
    if " " in exe:
        exe = f'"{exe}"'
    return f'{exe} "{(data / HOOK_SCRIPT).as_posix()}"'


def load_settings() -> tuple[Path, dict]:
    path = claude / "settings.json"
    return path, json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_settings(path: Path, settings: dict) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def register_hook() -> None:
    settings_path, settings = load_settings()
    command = hook_command()
    stop = settings.setdefault("hooks", {}).setdefault("Stop", [])
    existing = [h for group in stop for h in group.get("hooks", []) if HOOK_SCRIPT in h.get("command", "")]
    if existing and existing[0]["command"] == command:
        print("Stop hook already registered")
        return
    if existing:
        existing[0]["command"] = command  # Python or install path moved
    else:
        stop.append({"hooks": [{"type": "command", "command": command, "timeout": 120, "async": True}]})
    save_settings(settings_path, settings)
    print(f"Registered Stop hook -> {settings_path} (backup: settings.json.bak)")


def remove_hook() -> None:
    settings_path, settings = load_settings()
    stop = settings.get("hooks", {}).get("Stop", [])
    kept = []
    for group in stop:
        hooks = [h for h in group.get("hooks", []) if HOOK_SCRIPT not in h.get("command", "")]
        if hooks:
            kept.append(dict(group, hooks=hooks))
    if kept == stop:
        return
    if kept:
        settings["hooks"]["Stop"] = kept
    else:
        del settings["hooks"]["Stop"]
        if not settings["hooks"]:
            del settings["hooks"]
    save_settings(settings_path, settings)
    print(f"Removed the Stop hook from {settings_path} (backup: settings.json.bak)")


def add_rule() -> None:
    md = claude / "CLAUDE.md"
    text = md.read_text(encoding="utf-8") if md.exists() else ""
    if RULE_BLOCK.search(text):
        new = RULE_BLOCK.sub(lambda m: ("\n\n" if m.start() else "") + RULE, text, count=1)
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + RULE
    if new != text:
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(new, encoding="utf-8")
        print(f"Added time-estimate rule -> {md}")


def remove_rule() -> None:
    md = claude / "CLAUDE.md"
    if not md.exists():
        return
    text = md.read_text(encoding="utf-8")
    new = RULE_BLOCK.sub("\n", text).rstrip("\n") + "\n" if RULE_BLOCK.search(text) else text
    if new.strip() == "":
        new = ""
    if new != text:
        md.write_text(new, encoding="utf-8")
        print(f"Removed the time-estimate rule from {md}")


def remove_files(purge: bool) -> None:
    for path in (claude / AGENT, claude / COMMAND):
        if path.exists():
            path.unlink()
    if purge:
        shutil.rmtree(data, ignore_errors=True)
        print(f"Removed agent, /estimate-here and {data} (scripts and all estimate data)")
        return
    for script in data.glob("*.py"):
        script.unlink()
    shutil.rmtree(data / "__pycache__", ignore_errors=True)
    print(f"Removed agent, /estimate-here and scripts. Your estimate data stays in {data} (--purge deletes it).")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Install or remove the Claude Code time estimator.")
    parser.add_argument("--no-hook", action="store_true", help="do not register the automatic-scoring Stop hook")
    parser.add_argument("--no-rule", action="store_true", help="do not add the time-estimate rule to ~/.claude/CLAUDE.md")
    parser.add_argument("--uninstall", action="store_true", help="remove everything install.py added")
    parser.add_argument("--purge", action="store_true", help="with --uninstall: also delete rates and estimate history")
    args = parser.parse_args(argv)
    if args.uninstall:
        remove_hook()
        remove_rule()
        remove_files(args.purge)
        return
    copy_files()
    if not args.no_hook:
        register_hook()
    if not args.no_rule:
        add_rule()
    subprocess.run([sys.executable, str(data / "calibrate.py")], check=False)


if __name__ == "__main__":
    main()
