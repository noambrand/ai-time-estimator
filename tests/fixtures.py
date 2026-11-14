# fixtures.py - shared helpers for the test suite: synthetic transcripts, synthetic rates,
# and a base TestCase that points cc_log at a temp folder so ~/.claude is never touched.
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import cc_log  # noqa: E402

MODEL = "claude-test-1"
CWD = r"C:\Work\demo"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class Transcript:
    """Builds transcript lines in the Claude Code .jsonl shape that cc_log parses."""

    def __init__(self, start_ts: float, cwd: str = CWD, sidechain: bool = False, agent_id: str = ""):
        self.t = start_ts
        self.cwd = cwd
        self.lines: list[dict] = []
        self.extra = {"isSidechain": True, "agentId": agent_id} if sidechain else {"isSidechain": False}
        self._n = 0

    def _add(self, obj: dict, gap: float) -> float:
        self.t += gap
        obj.update(self.extra, timestamp=iso(self.t), cwd=self.cwd)
        self.lines.append(obj)
        return self.t

    def human(self, text: str = "go", gap: float = 0.0, meta: bool = False, typed: bool = False) -> float:
        """A user message. typed=True adds the origin fields newer Claude Code versions write."""
        obj = {"type": "user", "message": {"role": "user", "content": text}}
        if meta:
            obj["isMeta"] = True
        if typed:
            obj.update(origin={"kind": "human"}, promptSource="typed")
        return self._add(obj, gap)

    def notification(self, tool: dict | None = None, gap: float = 0.0) -> float:
        """Claude Code's notice that a background task (agent, workflow, background command) finished."""
        tid = f"<tool-use-id>{tool['id']}</tool-use-id> " if tool else ""
        text = f"<task-notification> <task-id>t1</task-id> {tid}<status>completed</status> </task-notification>"
        return self._add({"type": "user", "origin": {"kind": "task-notification"}, "promptSource": "system",
                          "message": {"role": "user", "content": text}}, gap)

    def compact(self, gap: float = 0.0) -> float:
        """The summary line Claude Code writes when it compacts a long conversation."""
        return self._add({"type": "user", "isCompactSummary": True, "message": {
            "role": "user", "content": "This session is being continued from a previous conversation..."}}, gap)

    def tool(self, name: str, **inp) -> dict:
        self._n += 1
        return {"type": "tool_use", "id": f"tu{self._n}", "name": name, "input": inp}

    def turn(self, *tools: dict, dur: float = 5.0, ctx: int = 10_000, out: int = 200,
             model: str = MODEL, chunks: int = 1, req: str | None = None, effort: str | None = None) -> float:
        """One model request; `chunks` > 1 writes it as several streamed lines with the same requestId."""
        self._n += 1
        req = req or f"req{self._n}"
        for i in range(chunks):
            last = i == chunks - 1
            usage = {"input_tokens": 10, "cache_read_input_tokens": ctx - 10, "output_tokens": out if last else 1}
            content = list(tools) if last else [{"type": "text", "text": "..."}]
            obj = {"type": "assistant", "requestId": req,
                   "message": {"model": model, "usage": usage, "content": content}}
            if effort:
                obj["effort"] = effort
            self._add(obj, dur / chunks)
        return self.t

    def result(self, tool: dict, gap: float = 1.0, error: bool = False, text: str = "ok") -> float:
        block = {"type": "tool_result", "tool_use_id": tool["id"], "is_error": error, "content": text}
        return self._add({"type": "user", "message": {"role": "user", "content": [block]}}, gap)

    def write(self, path: Path, partial_tail: bool = False) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(obj) + "\n" for obj in self.lines)
        if partial_tail:
            text += '{"type": "assistant", "timest'
        path.write_text(text, encoding="utf-8")
        return path


def make_rates(**model_overrides) -> dict:
    """Small hand-made rates.json whose numbers make simulate() results computable by hand.
    Every turn below 25k tokens takes 10 s, from 25k on 20 s. Read 1 s, Grep 0.5 s, Edit 2 s."""
    model = {"turns": 100, "median_turn_seconds": 10.0,
             "turn_seconds_by_ctx": {"0": [10.0] * 20, "25000": [20.0] * 20},
             "median_output_tokens": 500, "tokens_per_second": 100.0, "tokens_per_written_line": 10.0,
             "start_context_tokens": 20_000, "context_growth_per_turn": 1000, "calls_per_turn": 1.0,
             "seconds_per_call": 15.0}
    model.update(model_overrides)
    tools = {"Read": [1.0] * 5, "Grep": [0.5] * 5, "Edit": [2.0] * 5, "Agent": [60.0] * 5}
    return {"bucket_tokens": 25_000, "models": {MODEL: model},
            "tools": {n: {"n": len(v), "median": v[0], "samples": v} for n, v in tools.items()},
            "commands": {"*": {"python run.py": [30.0] * 3},
                         "c:/work/demo": {"npm test": [12.0] * 3}},
            "fix_loop_calls": {"n": 10, "median": 2}, "generated": "2026-11-14T00:00:00"}


class IsolatedTest(unittest.TestCase):
    """Redirects cc_log.PROJECTS_DIR and cc_log.DATA_DIR into a fresh temp folder per test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ate-test-"))
        self.projects = self.tmp / "projects"
        self.data = self.tmp / "data"
        self.projects.mkdir()
        self.data.mkdir()
        for name, value in (("PROJECTS_DIR", self.projects), ("DATA_DIR", self.data)):
            patcher = mock.patch.object(cc_log, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_rates(self, rates: dict | None = None) -> dict:
        rates = rates or make_rates()
        (self.data / "rates.json").write_text(json.dumps(rates), encoding="utf-8")
        return rates
