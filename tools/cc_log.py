# [UPDATED v1.7] cc_log.py - shared Claude Code transcript parsing for the AI time estimator
# Parsing rules follow dioptx/cctime: merge streamed assistant chunks by requestId,
# keep the usage with the highest output_tokens, tool time = tool_use -> tool_result gap.
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
DATA_DIR = Path.home() / ".claude" / "ai-time-estimator"

STALL_SECONDS = 900          # a model turn longer than this is a stall/sleep, not work
LONG_TOOL_MAX_SECONDS = 3600 # shell commands, subagents, web/MCP tools: longer runs are outliers
QUICK_TOOL_MAX_SECONDS = 30  # Read/Grep/Edit taking longer = waited for a human approval
BIG_WRITE_LINES = 30         # a turn writing this many lines is timed by output speed, not the turn pool

HUMAN_TOOLS = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}
READ_TOOLS = {"Read"}
SEARCH_TOOLS = {"Grep", "Glob", "LS"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
QUICK_TOOLS = READ_TOOLS | SEARCH_TOOLS | EDIT_TOOLS
BOOKKEEPING_TOOLS = {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TodoWrite", "ToolSearch"}  # no project work
# Tools that take well under a second: a longer gap means they waited (approval, a blocked teammate, a
# stall), so their samples are capped at QUICK_TOOL_MAX_SECONDS.
INSTANT_TOOLS = QUICK_TOOLS | BOOKKEEPING_TOOLS
TOOL_ALIASES = {"Task": "Agent", "Agent": "Task"}  # the subagent tool was renamed between versions
RESULT_TEXT_CHARS = 3000     # tail of each tool result kept, to spot failures a pipe hid from the exit code

# User-type lines that Claude Code writes itself, not the user: background-task completion notices,
# messages from other Claude sessions, compaction summaries, skill text, output of local /commands and of
# `!` shell commands. They become "system" events: no human wait before them, and they never start or end a task.
SYSTEM_PREFIXES = ("<task-notification>", "Another Claude session sent a message", "This session is being continued",
                   "Base directory for this skill", "<local-command-stdout>", "<local-command-stderr>",
                   "<local-command-caveat>", "<bash-stdout>", "<bash-stderr>")
SYSTEM_ORIGINS = {"task-notification", "peer"}
NOTICE_TOOL_ID = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")
TEAMMATE_ID = re.compile(r'<teammate-message[^>]*\bteammate_id="([^"]+)"')  # a named agent reporting back
HUMAN_TEXT_CHARS = 300       # start of each typed message kept (history.py shows it)

# A shell command whose exit status is not the status of its main step: `x | tail`, `x; echo`, `x || true`.
MASKED_STATUS = re.compile(r"(?<!\|)\|(?!\|)|;|\|\|")
# Output of a failed test run, build or script. Only consulted when MASKED_STATUS hid the exit code.
FAILURE_MARKERS = re.compile(
    r"Traceback \(most recent call last\)|^FAILED\b|\bFAILED \(|^--- FAIL|^FAIL\b|\b[1-9]\d* (?:failed|failing)\b"
    r"|npm ERR!|\berror(?:\[E\d+\]| TS\d+)?: |^\w*Error: |^Exit code [1-9]", re.MULTILINE)

# Read-only shell utilities that finish in well under a second. A command made only of such steps that took
# longer than QUICK_TOOL_MAX_SECONDS waited (an approval, a queued call, a slow hook), like a slow Read does.
QUICK_COMMAND_WORDS = {"ls", "dir", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "sed", "awk", "cut",
                       "sort", "uniq", "tr", "nl", "column", "file", "stat", "find", "echo", "printf", "pwd", "which",
                       "type", "basename", "dirname", "realpath", "readlink", "date", "diff", "cmp", "test", "true"}
QUICK_GIT_SUBCOMMANDS = {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "ls-tree", "blame",
                         "remote", "tag", "describe", "shortlog", "cat-file", "config", "reflog", "merge-base", "grep"}
QUOTED = re.compile(r'"(?:\\.|[^"\\])*"|\'[^\']*\'')
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\1\b", re.DOTALL)
# A person answered a permission prompt for this call ("source" of the permissionDecision Claude Code writes on
# the tool_result line): the gap holds their reply time. "user_permanent" is a rule the user saved earlier.
PROMPTED_SOURCES = {"user_temporary", "user_reject"}


def parse_ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def norm_path(path: str) -> str:
    """Same folder written as C:\\x, /c/x (Git Bash) or /mnt/c/x (WSL) -> c:/x."""
    p = (path or "").strip().replace("\\", "/").rstrip("/").lower()
    m = re.match(r"^(?:/mnt)?/([a-z])(/.*)?$", p)
    if m:
        p = m.group(1) + ":" + (m.group(2) or "")
    return p


def tool_category(name: str) -> str:
    if name in READ_TOOLS:
        return "reads"
    if name in SEARCH_TOOLS:
        return "searches"
    if name in EDIT_TOOLS:
        return "edits"
    if name == "Bash":
        return "commands"
    if name in BOOKKEEPING_TOOLS:
        return "bookkeeping"
    return "other_calls"


def result_text(block: dict) -> str:
    """Tail of a tool_result's text (content is a string or a list of text blocks)."""
    content = block.get("content")
    if isinstance(content, list):
        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content[-RESULT_TEXT_CHARS:] if isinstance(content, str) else ""


def message_text(content) -> str:
    """Text of a message whose content is a string or a list of blocks."""
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content if isinstance(content, str) else ""


def is_system_line(obj: dict, text: str) -> bool:
    """A user-type line that the user did not write (see SYSTEM_PREFIXES). Newer Claude Code versions mark
    typed prompts with origin.kind "human" and notices with "task-notification"; older lines only have text."""
    origin = obj.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else origin
    if kind == "human":
        return False
    if kind in SYSTEM_ORIGINS or obj.get("promptSource") == "system" or obj.get("turnOrigin") in ("task_notification", "peer"):
        return True
    return bool(obj.get("isCompactSummary")) or text.lstrip().startswith(SYSTEM_PREFIXES)


def looks_failed(command: str, error: bool, text: str) -> bool:
    """Failed run: an error result, or a failure in the output of a command whose exit status was masked."""
    return error or bool(MASKED_STATUS.search(command or "") and FAILURE_MARKERS.search(text or ""))


def command_keys(command: str) -> list[str]:
    """Lookup keys for a shell command: its main step's first 3 words, then first 2 words."""
    steps = [s.strip() for s in re.split(r"&&|\|\|?|;|\n", command or "") if s.strip()]
    main = next((s for s in steps if not re.match(r"(?i)(cd|pushd|set|export|source)\b", s)), "")
    words = [w.strip("\"'").lower() for w in main.split() if not re.match(r"^\d?>|^<|^&>", w)]  # drop redirections
    while words and re.match(r"^[a-z_][a-z0-9_]*=", words[0]):
        words.pop(0)
    keys = [" ".join(words[:n]) for n in (3, 2) if len(words) >= n]
    return list(dict.fromkeys(keys)) or words[:1]


def is_quick_command(command: str) -> bool:
    """True when every step of a shell command is a read-only utility (ls, grep, sed -n, git status/log/diff...)."""
    text = QUOTED.sub('""', HEREDOC.sub("", command or ""))
    steps = [s.strip() for s in re.split(r"&&|\|\|?|;|\n", text) if s.strip()]
    steps = [s for s in steps if not re.match(r"(?i)(cd|pushd|popd|set|export|source)\b", s)]
    return bool(steps) and all(quick_step(s) for s in steps)


def quick_step(step: str) -> bool:
    words = [w for w in step.split() if not re.match(r"^\d?>|^<|^&>", w)]   # drop redirections
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):         # VAR=value prefixes
        words.pop(0)
    if not words:
        return True
    name = words[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name != "git":
        return name in QUICK_COMMAND_WORDS
    rest = words[1:]
    while rest and rest[0].startswith("-"):                                  # git -C path / -c key=value
        if rest.pop(0) in ("-C", "-c") and rest:
            rest.pop(0)
    return bool(rest) and rest[0].lower() in QUICK_GIT_SUBCOMMANDS


def permission_flags(obj: dict) -> tuple[bool, bool]:
    """(prompted, denied) of a tool_result line: a person answered a permission prompt / the call never ran."""
    decision = obj.get("permissionDecision")
    if not isinstance(decision, dict):
        return False, bool(obj.get("toolDenialKind"))
    prompted = decision.get("source") in PROMPTED_SOURCES or "grantScope" in decision
    return prompted, decision.get("decision") == "reject" or bool(obj.get("toolDenialKind"))


def written_lines(tool: dict) -> int:
    inp = tool["input"] if isinstance(tool.get("input"), dict) else {}
    texts = [inp.get("content"), inp.get("new_string"), inp.get("new_source")]
    texts += [e.get("new_string") for e in inp.get("edits", []) if isinstance(e, dict)]
    return sum(t.count("\n") + 1 for t in texts if isinstance(t, str) and t)


def find_transcripts(since_ts: float) -> list[Path]:
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p for p in PROJECTS_DIR.rglob("*.jsonl") if p.stat().st_mtime >= since_ts)


def transcript_cwd(path: Path, max_lines: int = 200) -> str:
    """Normalized working folder of a transcript, from its first lines."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(max_lines), handle):
                if '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd")
                    except ValueError:
                        continue
                    if cwd:
                        return norm_path(cwd)
    except OSError:
        pass
    return ""


def latest_turn_settings(project: str, max_files: int = 30, tail_bytes: int = 2_000_000) -> dict:
    """{"model", "effort", "ctx"} of the newest model turn in this project's most recently written main
    transcript - the session that is asking for an estimate. Empty values when there is none."""
    found = {"model": "", "effort": "", "ctx": 0}
    if not PROJECTS_DIR.exists():
        return found
    files = sorted(PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:max_files]:
        if transcript_cwd(path) != norm_path(project):
            continue
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - tail_bytes))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if '"assistant"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant" or obj.get("isSidechain"):
                continue
            msg = obj.get("message") or {}
            model = msg.get("model", "")
            if model and not model.startswith("<"):
                usage = msg.get("usage") or {}
                ctx = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                return {"model": model, "effort": str(obj.get("effort") or ""), "ctx": ctx}
        return found
    return found


def load_streams(path: Path, since_ts: float = 0.0, until_ts: float = float("inf")) -> dict[str, dict]:
    """One transcript file -> {"main" | "side:<agent>": {"cwd": str, "events": [...]}}."""
    raw: dict[str, list[dict]] = {}
    cwd: dict[str, str] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # partial last line while Claude Code is still writing
            if not isinstance(obj, dict) or obj.get("type") not in ("user", "assistant") or not obj.get("timestamp"):
                continue
            ts = parse_ts(obj["timestamp"])
            if ts < since_ts or ts > until_ts:
                continue
            obj["_ts"] = ts
            name = "side:" + str(obj.get("agentId", "")) if obj.get("isSidechain") else "main"
            raw.setdefault(name, []).append(obj)
            if obj.get("cwd") and name not in cwd:
                cwd[name] = obj["cwd"]
    return {name: {"cwd": cwd.get(name, ""), "events": to_events(lines)} for name, lines in raw.items()}


def to_events(lines: list[dict]) -> list[dict]:
    """Ordered events: turn (one model request), result (tool results), human (typed by the user, with the
    start of its text), system (written by Claude Code: task notices with their tool_use_id, summaries...)."""
    events: list[dict] = []
    prev_ts: float | None = None
    for obj in lines:
        ts = obj["_ts"]
        msg = obj.get("message") or {}
        content = msg.get("content")
        if obj["type"] == "assistant":
            req = obj.get("requestId") or msg.get("id") or obj.get("uuid")
            last = events[-1] if events else None
            if last and last["kind"] == "turn" and last["req"] == req:
                turn = last
            else:
                turn = {"kind": "turn", "req": req, "start": prev_ts if prev_ts is not None else ts,
                        "end": ts, "model": msg.get("model", ""), "effort": str(obj.get("effort") or ""),
                        "ctx": 0, "out": 0, "tools": []}
                events.append(turn)
            turn["end"] = max(turn["end"], ts)
            usage = msg.get("usage") or {}
            turn["out"] = max(turn["out"], usage.get("output_tokens") or 0)
            ctx = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            turn["ctx"] = max(turn["ctx"], ctx)
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    turn["tools"].append({"id": block.get("id"), "name": block.get("name", ""),
                                          "input": block.get("input") or {}, "ts": ts})
        else:
            blocks = content if isinstance(content, list) else []
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                prompted, denied = permission_flags(obj)
                events.append({"kind": "result", "ts": ts,
                               "results": [{"id": b.get("tool_use_id"), "error": bool(b.get("is_error")),
                                            "text": result_text(b), "prompted": prompted, "denied": denied}
                                           for b in results]})
            elif not obj.get("isMeta"):
                text = message_text(content)
                if is_system_line(obj, text):
                    notice = NOTICE_TOOL_ID.search(text)
                    teammate = TEAMMATE_ID.search(text)
                    events.append({"kind": "system", "ts": ts, "tool_use_id": notice.group(1) if notice else None,
                                   "teammate": teammate.group(1) if teammate else None})
                else:
                    events.append({"kind": "human", "ts": ts, "text": text[:HUMAN_TEXT_CHARS]})
        prev_ts = ts
    return events


class RetryTracker:
    """Detects fix loops: a shell command fails, the worker edits something, the command runs again.
    Loop size = tool calls made between the failed run and its rerun.
    A rerun with no edit in between is a plain retry (flaky run, tweaked arguments), not a fix loop:
    it costs one more command run, which is already counted under commands.
    "Failed" includes a failure printed by a command whose exit status a pipe hid (see looks_failed)."""

    def __init__(self) -> None:
        self.calls = 0
        self.edits = 0
        self.pending: dict[str, tuple[str, int, str]] = {}
        self.failed_at: dict[str, tuple[int, int]] = {}  # command key -> (call index, edits so far)

    def on_call(self, tool: dict) -> int | None:
        self.calls += 1
        if tool["name"] in EDIT_TOOLS:
            self.edits += 1
        if tool["name"] != "Bash":
            return None
        command = tool["input"].get("command", "")
        key = (command_keys(command) or [""])[0]
        self.pending[tool["id"]] = (key, self.calls, command)
        if key in self.failed_at:
            failed_call, edits_then = self.failed_at.pop(key)
            if self.edits > edits_then:
                return self.calls - failed_call - 1
        return None

    def on_result(self, tool_id: str, error: bool, text: str = "") -> None:
        item = self.pending.pop(tool_id, None)
        if item and looks_failed(item[2], error, text):
            # Edits made after the call but before its result (parallel calls) do not count as the fix.
            self.failed_at[item[0]] = (item[1], self.edits)
