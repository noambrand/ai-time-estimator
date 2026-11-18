# [UPDATED v1.6] compare.py - measure what the worker actually did after an estimate, score the estimate,
# and show which count and which scenario were wrong. --report summarizes all past comparisons.
# Usage: python compare.py [--id E2026...] [--session path.jsonl] [--until 2026-11-13T14:05:00]
#        python compare.py --report
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import cc_log
from estimate import command_names, expected_scenario, likely_counts, scenarios_of

FIELDS = ("reads", "searches", "edits", "other_calls", "bookkeeping", "commands", "fix_loops", "write_lines")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def find_session(est: dict) -> tuple[Path, bool] | None:
    """The session whose transcript contains this estimate's id (exact).
    Falls back to the busiest session of the same project, flagged as a guess."""
    main_hits, sub_hits = [], []
    for path in cc_log.find_transcripts(est["ts"]):
        if est["id"] in path.read_text(encoding="utf-8", errors="replace"):
            (sub_hits if "subagents" in path.parts else main_hits).append(path)
    if main_hits:
        return max(main_hits, key=lambda p: p.stat().st_mtime), True
    for path in sub_hits:  # <project>/<session>/subagents/agent-x.jsonl -> <project>/<session>.jsonl
        session_dir = path.parent.parent
        parent = session_dir.parent / (session_dir.name + ".jsonl")
        if parent.exists():
            return parent, True
    best = None
    for path in cc_log.find_transcripts(est["ts"]):
        if "subagents" in path.parts:
            continue
        stream = cc_log.load_streams(path, est["ts"]).get("main")
        if stream and cc_log.norm_path(stream["cwd"]) == est["project"]:
            if best is None or len(stream["events"]) > best[1]:
                best = (path, len(stream["events"]))
    return (best[0], False) if best else None


def session_streams(path: Path, since: float) -> tuple[list[dict], list[list[dict]]]:
    """Main-session events, plus the event lists of every subagent of that session."""
    streams = cc_log.load_streams(path, since)
    main = streams.pop("main", {"events": []})["events"]
    side = [s["events"] for s in streams.values()]
    for sub in sorted((path.parent / path.stem / "subagents").glob("*.jsonl")):
        side += [s["events"] for s in cc_log.load_streams(sub, since).values()]
    return main, side


def work_start(events: list[dict], est_ts: float, until: float) -> float:
    """When the work began. Normally the first human message after the estimate (the "go").
    But if the worker already made real tool calls before any human message (in-session estimate
    followed straight by the work), the work began at the estimate itself."""
    first_human = next((e["ts"] for e in events if e["kind"] == "human" and est_ts <= e["ts"] <= until), None)
    if first_human is None:
        return est_ts
    worked = any(ev["kind"] == "turn" and est_ts <= ev["end"] < first_human and any(map(is_work, ev["tools"]))
                 for ev in events)
    return est_ts if worked else first_human


def is_work(tool: dict) -> bool:
    """A tool call that does the task: not task-list bookkeeping, not the estimator's own scripts."""
    if cc_log.tool_category(tool["name"]) == "bookkeeping":
        return False
    return not (tool["name"] == "Bash" and ESTIMATOR_COMMAND.search(str(tool["input"].get("command", ""))))


ESTIMATOR_COMMAND = re.compile(r"ai-time-estimator|\bestimate\.py\b|\bcalibrate\.py\b")


def requests(events: list[dict]) -> list[dict]:
    """One window per typed request: from a human message to just before the next one."""
    humans = [e for e in events if e["kind"] == "human"]
    return [{"start": h["ts"], "until": humans[i + 1]["ts"] - 0.001 if i + 1 < len(humans) else float("inf"),
             "text": h.get("text", "")} for i, h in enumerate(humans)]


def measure(events: list[dict], side: list[list[dict]], est_ts: float, until: float) -> dict:
    """What happened in one window. Active time = wall time minus human waits minus the time the session sat
    idle waiting for background work (it ended its turn with nothing running, until a notice woke it)."""
    start = work_start(events, est_ts, until)
    counts: Counter = Counter()
    runs: Counter = Counter()      # shell commands run, as typed (estimate.py looks each one up)
    names: Counter = Counter()     # other tools by name (WebFetch, Agent, TaskUpdate...)
    background: Counter = Counter()  # calls that started background work: command as typed, or tool name
    background_cmds: set = set()
    models: Counter = Counter()
    efforts: Counter = Counter()
    tracker = cc_log.RetryTracker()
    asked: dict[str, tuple[str, float]] = {}   # tool id -> (name, call time) for calls that may wait on the user
    bg_ids = cc_log.background_ids(events)
    running: set = set()           # foreground calls without a result yet
    idle = False                   # the last turn ended with no tool call and nothing runs in the foreground
    model_s = human_s = bg_wait_s = 0.0
    turns = first_ctx = 0
    prev = last = start
    for ev in events:
        t = ev["end"] if ev["kind"] == "turn" else ev["ts"]
        if t < start or t > until:
            continue
        if ev["kind"] == "human":
            human_s += max(0.0, ev["ts"] - prev)
            idle = False
        elif ev["kind"] == "turn":
            turns += 1
            models[ev.get("model", "")] += 1
            efforts[ev.get("effort", "")] += 1
            first_ctx = first_ctx or ev.get("ctx", 0)
            model_s += max(0.0, ev["end"] - max(ev["start"], start))
            for tool in ev["tools"]:
                category = cc_log.tool_category(tool["name"])
                counts[category] += 1
                command = str(tool["input"].get("command", ""))
                if tool["id"] in bg_ids:
                    background[command if category == "commands" else tool["name"]] += 1
                    if category == "commands":
                        background_cmds.add(command)
                else:
                    running.add(tool["id"])
                    if category == "commands":
                        runs[command] += 1
                    elif category in ("other_calls", "bookkeeping"):
                        names[tool["name"]] += 1
                counts["write_lines"] += cc_log.written_lines(tool) if tool["name"] in cc_log.EDIT_TOOLS else 0
                if tracker.on_call(tool) is not None:
                    counts["fix_loops"] += 1
                if tool["name"] in cc_log.HUMAN_TOOLS or (tool["id"] not in bg_ids and cc_log.is_instant(tool)):
                    asked[tool["id"]] = (tool["name"], tool["ts"])
            idle = not ev["tools"] and not running
        elif ev["kind"] == "result":
            for res in ev["results"]:
                running.discard(res["id"])
                tracker.on_result(res["id"], res["error"], res.get("text", ""))
                name, called = asked.pop(res["id"], ("", 0.0))
                # The user answering a question, or an instant call (Read/Edit/ls/grep/SendMessage...) taking 30+ s
                # or marked as answered at a permission prompt, is a human wait, not AI time.
                if name in cc_log.HUMAN_TOOLS or (name and (res.get("prompted")
                                                            or ev["ts"] - called > cc_log.QUICK_TOOL_MAX_SECONDS)):
                    human_s += max(0.0, ev["ts"] - prev)
            idle = False
        elif idle and ev.get("notice"):
            # A notice (a background agent or command finished, a teammate wrote) after the session had
            # stopped: the gap was a wait for background results, not work and not a human wait. Other system
            # lines (a compaction summary...) keep the time before them as active time.
            bg_wait_s += max(0.0, ev["ts"] - prev)
        prev = last = t
    # Subagent time is already inside the main session's Task/Agent call; their own calls are reported apart.
    sub = Counter()
    for stream in side:
        for ev in stream:
            if ev["kind"] == "turn" and start <= ev["end"] <= until:
                sub["turns"] += 1
                sub["calls"] += len(ev["tools"])
    wall = last - start
    return {"start": start, "end": last, "wall_min": wall / 60, "human_min": human_s / 60,
            "background_wait_min": bg_wait_s / 60, "active_min": (wall - human_s - bg_wait_s) / 60,
            "model_min": model_s / 60, "turns": turns,
            "counts": {f: counts[f] for f in FIELDS}, "subagent_turns": sub["turns"], "subagent_calls": sub["calls"],
            "runs": dict(runs), "tools": dict(names), "background": dict(background),
            "background_commands": sorted(background_cmds), "first_ctx": first_ctx,
            "model": models.most_common(1)[0][0] if models else "",
            "effort": efforts.most_common(1)[0][0] if efforts else ""}


def closest_scenario(est: dict, act: dict) -> str:
    def distance(s: dict) -> float:
        pred = likely_counts(s, command_names(est["counts"]))
        return sum(abs(math.log((act[f] + 1) / (pred[f] + 1))) for f in FIELDS)
    best = min(scenarios_of(est["counts"]), key=distance)
    return best.get("name", "single")


def score(est: dict, actual: dict, exact: bool) -> str:
    scenarios = scenarios_of(est["counts"])
    pred = likely_counts(expected_scenario(scenarios), command_names(est["counts"]))
    act = actual["counts"]
    a = actual["active_min"]
    verdict = "inside P10-P90" if est["p10"] <= a <= est["p90"] else ("ABOVE P90" if a > est["p90"] else "BELOW P10")
    print(f"Estimate {est['id']} [{est.get('task_type', 'other')}]: {est['task']}")
    print(f"Session: {Path(actual['session']).name} ({'matched by estimate id' if exact else 'guessed'})")
    if not exact:
        print("WARNING: estimate id not found in any transcript; session guessed by project. Pass --session to be sure.")
    print(f"Predicted P10 {est['p10']:.1f} | P50 {est['p50']:.1f} | P90 {est['p90']:.1f} min"
          + (f" | simple baseline {est['baseline']:.1f} min" if est.get("baseline") else ""))
    print(f"Actual AI time {a:.1f} min -> {verdict}  (wall {actual['wall_min']:.1f}, human waits {actual['human_min']:.1f}"
          f" and waits for background work {actual.get('background_wait_min', 0):.1f} excluded)")
    print(f"Actual split: model {actual['model_min']:.1f} min in {actual['turns']} turns "
          f"(predicted ~{est['median_turns']:.0f}) | tools+commands {actual['active_min'] - actual['model_min']:.1f} min")
    if actual["subagent_calls"]:
        print(f"Inside subagents: {actual['subagent_turns']} turns, {actual['subagent_calls']} tool calls "
              f"(their time is already inside the Task/Agent calls)")
    print(f"{'count':<12}{'predicted':>10}{'actual':>8}   (predicted = main scenario '{expected_scenario(scenarios).get('name', 'single')}')")
    for f in FIELDS:
        print(f"{f:<12}{pred[f]:>10.0f}{act[f]:>8}")
    closest = closest_scenario(est, act)
    weights = {s.get("name", "single"): s["weight"] for s in scenarios}
    if len(scenarios) > 1:
        print(f"The run looked most like scenario '{closest}' (you gave it {weights[closest]:.0%}).")
    misses = {f: math.log((act[f] + 1) / (pred[f] + 1)) for f in FIELDS}
    worst = max(misses, key=lambda f: abs(misses[f]))
    if abs(misses[worst]) > math.log(1.5):
        word = "under" if misses[worst] > 0 else "over"
        print(f"Biggest miss: {worst} {word}-counted ({pred[worst]:.0f} vs {act[worst]}). Fix the counting rule, not a multiplier.")
    if actual["turns"]:
        print(f"Seconds per model turn this run: {actual['model_min'] * 60 / actual['turns']:.1f} "
              f"(if far from calibration, rerun calibrate.py)")
    return closest


def report() -> None:
    rows = read_jsonl(cc_log.DATA_DIR / "actuals.jsonl")
    if not rows:
        sys.exit("No comparisons yet.")
    inside = sum(1 for r in rows if r["p10"] <= r["active_min"] <= r["p90"])
    ratios = [r["active_min"] / r["p50"] for r in rows if r["p50"]]
    print(f"Comparisons: {len(rows)} | inside P10-P90: {inside}/{len(rows)} (aim ~80%) | "
          f"median actual/P50: {statistics.median(ratios):.2f} (1.00 = no bias)")
    print("Median actual/predicted per count (1.00 = unbiased; >1 = estimator under-counts):")
    for f in FIELDS:
        vals = [(r["actual"].get(f, 0) + 1) / (r["predicted"].get(f, 0) + 1) for r in rows]  # older rows lack new fields
        print(f"  {f:<12}{statistics.median(vals):.2f}")
    by_type = defaultdict(list)
    for r in rows:
        if r["active_min"] > 0:
            by_type[r.get("task_type", "other")].append(abs(r["p50"] - r["active_min"]) / r["active_min"])
    print("Typical P50 error by task type: " + " | ".join(
        f"{t} {statistics.median(v):.0%} (n={len(v)})" for t, v in sorted(by_type.items())))
    rough = [r for r in rows if r.get("closest_scenario") and r.get("main_scenario")
             and r["closest_scenario"] != r["main_scenario"]]
    print(f"Runs that did not follow the main scenario: {len(rough)}/{len(rows)}")
    pairs = [(r["active_min"], r["p50"], r["baseline"]) for r in rows if r.get("baseline") and r["active_min"] > 0]
    if pairs:
        full = statistics.median(abs(p - a) / a for a, p, _ in pairs)
        simple = statistics.median(abs(b - a) / a for a, _, b in pairs)
        closer = sum(1 for a, p, b in pairs if abs(p - a) < abs(b - a))
        print(f"Typical error: full model {full:.0%} | simple baseline {simple:.0%} | full model closer in {closer}/{len(pairs)}")
        print("If after ~10 tasks the baseline is as accurate, the simulation is not worth keeping.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare an estimate with what actually happened.")
    parser.add_argument("--id", help="estimate id (default: latest)")
    parser.add_argument("--session", help="transcript .jsonl (default: the one containing the estimate id)")
    parser.add_argument("--until", help="ignore activity after this local time, e.g. 2026-11-13T14:05:00")
    parser.add_argument("--report", action="store_true", help="summarize all past comparisons")
    args = parser.parse_args()
    if args.report:
        report()
        return

    estimates = read_jsonl(cc_log.DATA_DIR / "estimates.jsonl")
    est = next((e for e in reversed(estimates) if not args.id or e["id"] == args.id), None)
    if est is None:
        sys.exit("Estimate not found.")
    found = (Path(args.session), True) if args.session else find_session(est)
    if not found:
        sys.exit("No session activity found after this estimate. Pass --session.")
    main_events, side = session_streams(found[0], est["ts"])
    if not main_events:
        sys.exit(f"No activity after the estimate in {found[0]}. Pass --session.")
    until = datetime.fromisoformat(args.until).timestamp() if args.until else float("inf")
    score_and_log(est, found[0], found[1], until, main_events, side)


def score_and_log(est: dict, session: Path, exact: bool, until: float,
                  main_events: list[dict] | None = None, side: list[list[dict]] | None = None) -> dict:
    """Measure the session after the estimate, print the score, and store it in actuals.jsonl
    (replacing an earlier score of the same estimate). Returns the stored row."""
    if main_events is None:
        main_events, side = session_streams(session, est["ts"])
    actual = measure(main_events, side or [], est["ts"], until)
    actual["session"] = str(session)
    closest = score(est, actual, exact)

    scenarios = scenarios_of(est["counts"])
    row = {"estimate_id": est["id"], "session": str(session), "exact_session": exact, "task": est["task"],
           "task_type": est.get("task_type", "other"), "p10": est["p10"], "p50": est["p50"], "p90": est["p90"],
           # the model's own numbers (before the self-correction), which estimate.correction() learns from
           **{f"raw_{q}": est.get(f"raw_{q}", est[q]) for q in ("p10", "p50", "p90")},
           "active_min": round(actual["active_min"], 2), "human_min": round(actual["human_min"], 2),
           "background_wait_min": round(actual["background_wait_min"], 2),
           "turns": actual["turns"], "subagent_calls": actual["subagent_calls"], "baseline": est.get("baseline"),
           "main_scenario": expected_scenario(scenarios).get("name", "single"), "closest_scenario": closest,
           "predicted": likely_counts(expected_scenario(scenarios), command_names(est["counts"])),
           "actual": actual["counts"]}
    log_path = cc_log.DATA_DIR / "actuals.jsonl"
    rows = [r for r in read_jsonl(log_path) if r["estimate_id"] != est["id"]] + [row]  # re-scoring replaces
    log_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return row


if __name__ == "__main__":
    main()
