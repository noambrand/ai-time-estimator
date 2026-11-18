# [UPDATED v1.6] calibrate.py - measure unit rates (seconds per model turn, per tool, per command)
# from your own Claude Code transcripts. Read-only. Writes ~/.claude/ai-time-estimator/rates.json
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from collections import defaultdict

import cc_log

BUCKET_TOKENS = 25_000   # context-size bucket width for turn latency
MAX_SAMPLES = 400        # samples kept per bucket / command (keeps rates.json small)
MIN_MODEL_TURNS = 20
MIN_SESSION_CALLS = 10   # a main session needs this many tool calls to give a seconds-per-call rate


def median_or_none(values: list[float], min_n: int = 1) -> float | None:
    return round(statistics.median(values), 3) if len(values) >= min_n else None


def keep_sample(values: list[float], rng: random.Random) -> list[float]:
    picked = rng.sample(values, MAX_SAMPLES) if len(values) > MAX_SAMPLES else values
    return [round(v, 2) for v in picked]


class Accumulator:
    def __init__(self) -> None:
        self.models: dict[str, dict] = defaultdict(lambda: {
            "turns": [], "start_ctx": [], "growth": [], "tok_per_line": [], "tool_turns": 0, "tool_calls": 0,
            "session_rates": []})
        self.tools: dict[str, list[float]] = defaultdict(list)
        self.commands: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.loops: list[int] = []
        self.turns_seen = 0
        self.turns_with_usage = 0

    def add_stream(self, cwd: str, events: list[dict], is_main: bool) -> None:
        project = cc_log.norm_path(cwd)
        tracker = cc_log.RetryTracker()
        pending: dict[str, tuple[str, float, dict]] = {}
        prev_ctx = 0
        first = True
        model = ""
        # Per model in this stream: model seconds, tool calls, and the longest tool of each turn
        # (calls issued together run in parallel, so a turn's tool time is its slowest call, not the sum).
        session: dict[str, dict] = defaultdict(lambda: {"turn_s": 0.0, "calls": 0, "tool_max": defaultdict(float)})
        turn_no = 0
        # A tool's duration is known once its result arrives - unless it ran in the background (Agent,
        # Workflow, background Bash...): then its result only says "launched", and the real end is the later
        # completion notice that names its tool_use_id, or - for a named agent - its first message back
        # ("teammate_id" = the name it was launched with). So results wait here until the stream ends.
        finished: dict[str, tuple[tuple, float, dict]] = {}
        named: dict[str, str] = {}               # agent name -> tool_use_id of its latest launch

        def record(item: tuple, dur: float, res: dict) -> None:
            tool_s = session[item[3]]["tool_max"]
            tool_s[item[4]] = max(tool_s[item[4]], self.add_tool_time(project, item, dur, res))

        for ev in events:
            if ev["kind"] == "turn":
                if not ev["model"] or ev["model"].startswith("<"):
                    continue
                model = ev["model"]
                m = self.models[model]
                turn_no += 1
                self.turns_seen += 1
                self.turns_with_usage += 1 if ev["ctx"] else 0
                lines = sum(cc_log.written_lines(t) for t in ev["tools"] if t["name"] in cc_log.EDIT_TOOLS)
                dur = ev["end"] - ev["start"]
                if 0 < dur <= cc_log.STALL_SECONDS:
                    m["turns"].append((ev["ctx"], dur, ev["out"], lines >= cc_log.BIG_WRITE_LINES, ev.get("effort", "")))
                    session[model]["turn_s"] += dur
                if is_main and first and ev["ctx"]:
                    m["start_ctx"].append(ev["ctx"])
                first = False
                if prev_ctx and ev["ctx"] > prev_ctx:
                    m["growth"].append(ev["ctx"] - prev_ctx)
                prev_ctx = ev["ctx"] or prev_ctx
                if ev["tools"]:
                    m["tool_turns"] += 1
                    m["tool_calls"] += len(ev["tools"])
                    session[model]["calls"] += len(ev["tools"])
                if lines >= cc_log.BIG_WRITE_LINES and ev["out"]:
                    m["tok_per_line"].append(ev["out"] / lines)
                for tool in ev["tools"]:
                    pending[tool["id"]] = (tool["name"], tool["ts"], tool["input"], model, turn_no)
                    if tool["input"].get("name") and tool["name"] in ("Agent", "Task"):
                        named[str(tool["input"]["name"])] = tool["id"]
                    loop = tracker.on_call(tool)
                    if loop is not None and loop <= 30:
                        self.loops.append(loop)
            elif ev["kind"] == "result":
                for res in ev["results"]:
                    tracker.on_result(res["id"], res["error"], res.get("text", ""))
                    item = pending.pop(res["id"], None)
                    if item:
                        finished[res["id"]] = (item, ev["ts"] - item[1], res)
            elif ev["kind"] == "system":
                tool_id = ev.get("tool_use_id") or named.pop(ev.get("teammate") or "", None)
                if tool_id in finished:
                    item, _, res = finished.pop(tool_id)
                    record(item, ev["ts"] - item[1], res)    # background work: launch -> notice or first reply
        for item, dur, res in finished.values():
            record(item, dur, res)
        if is_main:  # subagent time is already inside the main session's Agent/Task calls
            for name, s in session.items():
                if s["calls"] >= MIN_SESSION_CALLS:
                    self.models[name]["session_rates"].append((s["turn_s"] + sum(s["tool_max"].values())) / s["calls"])

    def add_tool_time(self, project: str, item: tuple, dur: float, res: dict | None = None) -> float:
        """Store one tool duration; returns the seconds accepted (0 when filtered out). A call whose
        permission prompt a person answered, or that was denied and never ran, is no run time."""
        name, _, inp = item[:3]
        quick = name in cc_log.INSTANT_TOOLS or (name == "Bash" and cc_log.is_quick_command(inp.get("command", "")))
        limit = cc_log.QUICK_TOOL_MAX_SECONDS if quick else cc_log.LONG_TOOL_MAX_SECONDS
        if dur < 0 or dur > limit or name in cc_log.HUMAN_TOOLS or (res and (res.get("prompted") or res.get("denied"))):
            return 0.0
        if name == "Bash":
            for key in cc_log.command_keys(inp.get("command", "")):
                self.commands[project][key].append(dur)
                self.commands["*"][key].append(dur)
        else:
            self.tools[name].append(dur)  # every tool keeps its own times: Task/Agent, WebFetch, MCP...
        return dur

    def finish(self) -> dict:
        rng = random.Random(7)
        models = {}
        for model, m in self.models.items():
            if len(m["turns"]) < MIN_MODEL_TURNS:
                continue
            buckets: dict[int, list[float]] = defaultdict(list)
            by_effort: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
            for ctx, dur, _, big_write, effort in m["turns"]:
                if not big_write:  # big writes are added separately by output speed (no double count)
                    bucket = ctx // BUCKET_TOKENS * BUCKET_TOKENS
                    buckets[bucket].append(dur)
                    if effort:
                        by_effort[effort][bucket].append(dur)
            fast = [out / dur for _, dur, out, *_ in m["turns"] if out >= 1500]
            models[model] = {
                "turns": len(m["turns"]),
                "median_turn_seconds": median_or_none([t[1] for t in m["turns"]]),
                "turn_seconds_by_ctx": {str(k): keep_sample(v, rng) for k, v in sorted(buckets.items())},
                # Turn time grows with reasoning effort (max is ~40% slower than xhigh): same buckets per effort.
                "by_effort": {e: {"turns": sum(map(len, bk.values())),
                                  "median_turn_seconds": median_or_none([d for v in bk.values() for d in v]),
                                  "turn_seconds_by_ctx": {str(k): keep_sample(v, rng) for k, v in sorted(bk.items())}}
                              for e, bk in sorted(by_effort.items()) if sum(map(len, bk.values())) >= MIN_MODEL_TURNS},
                "median_output_tokens": median_or_none([t[2] for t in m["turns"] if not t[3]]),
                "tokens_per_second": median_or_none(fast, 5),
                "tokens_per_written_line": median_or_none(m["tok_per_line"], 5),
                "start_context_tokens": median_or_none(m["start_ctx"]),
                "context_growth_per_turn": median_or_none(m["growth"]),
                "calls_per_turn": round(m["tool_calls"] / m["tool_turns"], 3) if m["tool_turns"] else 1.0,
                # Simple baseline rate: median over main sessions of (model + tool seconds) / tool calls.
                "seconds_per_call": round(statistics.median(m["session_rates"]), 2) if m["session_rates"] else None,
            }
        return {
            "bucket_tokens": BUCKET_TOKENS,
            "models": models,
            "tools": {n: {"n": len(v), "median": median_or_none(v), "samples": keep_sample(v, rng)}
                      for n, v in sorted(self.tools.items())},
            "commands": {p: {k: keep_sample(v, rng) for k, v in cmds.items() if len(v) >= 2}
                         for p, cmds in self.commands.items()},
            "fix_loop_calls": {"n": len(self.loops), "median": median_or_none(self.loops, 5)},
        }


def measure_rates(days: int, until: float = float("inf")) -> tuple[dict, Accumulator, list]:
    """Rates from the transcripts of the last `days` days, in memory (backtest.py uses this too).
    `until` leaves out activity after that time, so backtest.py can test on it."""
    since = time.time() - days * 86400
    files = cc_log.find_transcripts(since)
    acc = Accumulator()
    for path in files:
        for name, stream in cc_log.load_streams(path, since, until).items():
            acc.add_stream(stream["cwd"], stream["events"], name == "main")
    rates = acc.finish()
    rates.update({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "days": days, "files": len(files)})
    return rates, acc, files


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure Claude Code unit rates from your transcripts.")
    parser.add_argument("--days", type=int, default=60, help="use sessions from the last N days (default 60)")
    args = parser.parse_args()

    rates, acc, files = measure_rates(args.days)
    # Real requests are fast or slow as a whole (session, load, slow commands), which independent per-call
    # draws cannot show. Replay past requests through the model and keep the spread of real/predicted time.
    import backtest  # imported here: backtest imports this module
    rates["speed_factors"] = backtest.speed_factors(backtest.replay(rates, time.time() - args.days * 86400, sims=100))
    cc_log.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = cc_log.DATA_DIR / "rates.json"
    out.write_text(json.dumps(rates, indent=1), encoding="utf-8")

    print(f"Transcripts read: {len(files)} (last {args.days} days) -> {out}")
    if files and acc.turns_seen == 0:
        print("FORMAT CHECK FAILED: transcripts exist but no model turns were recognized. "
              "Claude Code probably changed its log format; only cc_log.py needs updating.")
    elif acc.turns_seen and acc.turns_with_usage == 0:
        print("FORMAT CHECK FAILED: turns found but no token usage fields; context-size rates are unusable. "
              "Update cc_log.py.")
    elif not rates["models"]:
        print(f"No model has {MIN_MODEL_TURNS}+ measured turns yet. Use Claude Code a bit more, then rerun.")
    for model, r in rates["models"].items():
        tps = r["tokens_per_second"]
        print(f"  {model}: {r['turns']} turns | median {r['median_turn_seconds']} s/turn | "
              f"{r['calls_per_turn']} tool calls/turn | {r['seconds_per_call']} s per tool call (all-in, median session) | "
              f"{tps if tps else 'n/a'} output tok/s")
        if r["by_effort"]:
            print("    median s/turn by effort: " + ", ".join(
                f"{e} {v['median_turn_seconds']} (n={v['turns']})" for e, v in r["by_effort"].items()))
    measured = sum(len(v) for p, v in rates["commands"].items() if p != "*")
    print(f"  shell command keys measured: {measured} | fix loops seen: {rates['fix_loop_calls']['n']}")
    spread = rates["speed_factors"]
    if spread:
        print(f"  speed spread from {spread['n']} past requests (real / predicted time): " + " | ".join(
            f"{b['label']} median {statistics.median(b['factors']):.2f}" for b in spread["bands"]))
    else:
        print("  speed spread: not enough past requests yet (ranges come from the per-call model only)")


if __name__ == "__main__":
    main()
