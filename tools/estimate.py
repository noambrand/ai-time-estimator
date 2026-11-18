# [UPDATED v1.5] estimate.py - turn the time-estimator's scenarios (operation counts) into
# P10/P50/P90 wall-clock minutes, using the rates measured by calibrate.py.
# Usage: python estimate.py counts.json      or      python estimate.py - < counts.json
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

import cc_log

COUNT_FIELDS = ("reads", "searches", "edits", "fix_loops", "write_lines", "read_lines")
SIMULATIONS = 4000
MIN_BUCKET_SAMPLES = 15
MIN_TOOL_SAMPLES = 3
READ_TOKENS_PER_LINE = 10        # approximation; only shifts which context bucket is used
FALLBACK_LOOP_CALLS = 2          # used until the logs hold 5+ measured fix loops
FALLBACK_TOKENS_PER_LINE = 12    # used until the logs hold 5+ large writes
FALLBACK_QUICK_SECONDS = 0.5     # Read/Grep/Edit with no measurement
FALLBACK_OTHER_SECONDS = 10      # any other tool with no measurement and no fallback_seconds
FALLBACK_COMMAND_SECONDS = 30
FALLBACK_LAUNCH_SECONDS = 1.0    # starting background work, until the logs hold 3+ measured launches
MIN_SCORED = 10                  # scored estimates needed before shown ranges correct themselves
RECENT_SCORED = 20               # the correction learns from the newest scored estimates only
TARGET_INSIDE = 0.8              # share of real times a P10-P90 range should hold


def measured(value, fallback):
    """A measured rate, or the fallback only when nothing was measured. A measured 0 is a real value."""
    return fallback if value is None else value


def as_range(value) -> tuple[float, float, float]:
    if isinstance(value, dict):
        low, likely, high = sorted([value.get("low", 0), value.get("likely", 0), value.get("high", 0)])
        return float(low), float(likely), float(high)
    return float(value or 0), float(value or 0), float(value or 0)


def draw(rng: random.Random, value) -> int:
    low, likely, high = as_range(value)
    return max(0, round(likely if high <= low else rng.triangular(low, high, likely)))


def scenarios_of(counts: dict) -> list[dict]:
    """Scenarios with weights summing to 1. The v1.0 flat format becomes one scenario."""
    if counts.get("scenarios"):
        items = counts["scenarios"]
    else:
        flat = {f: counts.get(f, 0) for f in COUNT_FIELDS}
        flat["runs"] = {c["cmd"]: c.get("runs", 1) for c in counts.get("commands", [])}
        flat["tools"] = {"other": counts["other_calls"]} if counts.get("other_calls") else {}
        items = [dict(flat, name="single")]
    total = sum(float(s.get("weight", 1)) for s in items)
    return [dict(s, weight=float(s.get("weight", 1)) / total) for s in items]


def expected_scenario(scenarios: list[dict]) -> dict:
    return max(scenarios, key=lambda s: s["weight"])


def command_names(counts: dict) -> set:
    """The shell commands an input declares (a `background` entry named like one is a command, else a tool)."""
    return {c["cmd"] for c in counts.get("commands", [])}


def likely_counts(scenario: dict, commands: set = frozenset()) -> dict:
    """Comparable counts of one scenario (likely values), same fields compare.py measures."""
    lk = lambda v: as_range(v)[1]
    out = {f: lk(scenario.get(f, 0)) for f in ("reads", "searches", "edits", "fix_loops", "write_lines")}
    tools = scenario.get("tools", {})
    background = scenario.get("background", {})
    out["bookkeeping"] = sum(lk(v) for k, v in tools.items() if k in cc_log.BOOKKEEPING_TOOLS)
    out["other_calls"] = (sum(lk(v) for k, v in tools.items() if k not in cc_log.BOOKKEEPING_TOOLS)
                          + sum(lk(v) for k, v in background.items() if k not in commands))
    out["commands"] = (sum(lk(v) for v in scenario.get("runs", {}).values()) + out["fix_loops"]
                       + sum(lk(v) for k, v in background.items() if k in commands))
    return out


def pick_model(rates: dict, wanted: str) -> tuple[str, dict]:
    matches = [(m, r) for m, r in rates["models"].items() if wanted.lower() in m.lower()]
    if not matches:
        sys.exit(f"No calibrated model matches '{wanted}'. Available: {', '.join(rates['models']) or 'none'}")
    return max(matches, key=lambda item: item[1]["turns"])


def effort_rates(model_rates: dict, effort: str) -> tuple[dict, str]:
    """Turn-time rates for this reasoning effort, or the model's pooled rates when that effort was not measured."""
    found = model_rates.get("by_effort", {}).get(effort) if effort else None
    if found:
        return found, f"turn times at effort '{effort}' (n={found['turns']})"
    return model_rates, (f"effort '{effort}' not measured: turn times of all efforts" if effort
                         else "turn times of all efforts")


def turn_sampler(model_rates: dict, bucket_tokens: int):
    buckets = {int(k): v for k, v in model_rates["turn_seconds_by_ctx"].items()}
    usable = sorted(k for k, v in buckets.items() if len(v) >= MIN_BUCKET_SAMPLES)
    pooled = [s for v in buckets.values() for s in v]

    def sample(rng: random.Random, ctx: float) -> float:
        if not usable:
            return rng.choice(pooled)
        target = int(ctx) // bucket_tokens * bucket_tokens
        return rng.choice(buckets[min(usable, key=lambda k: abs(k - target))])

    return sample, usable


def command_samples(rates: dict, project: str, command: str, fallback: float) -> tuple[list[float], str]:
    for key in cc_log.command_keys(command):
        for scope in (project, "*"):
            found = rates["commands"].get(scope, {}).get(key)
            if found:
                return found, ("this project" if scope == project else "other projects") + f": '{key}' n={len(found)}"
    return [float(fallback)], "UNMEASURED (fallback_seconds)"


def tool_samples(rates: dict, name: str, fallback: float) -> tuple[list[float], str]:
    for candidate in (name, cc_log.TOOL_ALIASES.get(name, "")):
        found = rates["tools"].get(candidate, {}).get("samples") or []
        if len(found) >= MIN_TOOL_SAMPLES:
            return found, f"measured '{candidate}' n={rates['tools'][candidate]['n']}"
    return [float(fallback)], "UNMEASURED (fallback_seconds)"


def speed_factor(rng: random.Random, spread: dict, minutes: float) -> float:
    """A real/predicted factor of a past request of about this predicted size (see backtest.speed_factors)."""
    for band in spread["bands"]:
        if band["max_p50"] is None or minutes < band["max_p50"]:
            return rng.choice(band["factors"])
    return 1.0


def quick_seconds(rates: dict, names: set[str]) -> float:
    vals = [rates["tools"][n]["median"] for n in names if rates["tools"].get(n, {}).get("median") is not None]
    return statistics.median(vals) if vals else FALLBACK_QUICK_SECONDS


def simulate(counts: dict, rates: dict, mr: dict, project: str, n: int = SIMULATIONS) -> dict:
    rng = random.Random(1)
    turn_rates, effort_note = effort_rates(mr, counts.get("worker_effort", ""))
    sample_turn, usable = turn_sampler(turn_rates, rates["bucket_tokens"])
    scenarios = scenarios_of(counts)
    notes = [effort_note]

    declared_cmds = {c["cmd"]: c for c in counts.get("commands", [])}
    bg_names = {k for s in scenarios for k in s.get("background", {})}
    cmd_names = set(declared_cmds) | {k for s in scenarios for k in s.get("runs", {})}
    cmd_samples = {}
    for cmd in sorted(cmd_names):
        fallback = declared_cmds.get(cmd, {}).get("fallback_seconds", FALLBACK_COMMAND_SECONDS)
        cmd_samples[cmd], source = command_samples(rates, project, cmd, fallback)
        notes.append(f"command '{cmd}': {source}")
    verify = next((cmd for cmd, c in declared_cmds.items() if c.get("verify")), None)

    declared_tools = {t["tool"]: t.get("fallback_seconds", FALLBACK_OTHER_SECONDS) for t in counts.get("tools", [])}
    tool_names = {k for s in scenarios for k in s.get("tools", {})}
    other_samples = {}
    for name in sorted(tool_names | (bg_names - set(declared_cmds))):
        other_samples[name], source = tool_samples(rates, name, declared_tools.get(name, FALLBACK_OTHER_SECONDS))
        notes.append(("tool" if name in tool_names else "background tool") + f" '{name}': {source}")
    # Background work (a background command, an agent launched in the background...): the session only spends
    # the launch on it; its run time goes to a separate wall-clock figure, not into the AI time.
    bg_samples = {k: cmd_samples[k] if k in declared_cmds else other_samples[k] for k in bg_names}
    launch_s = measured(rates.get("background_launch_seconds"), FALLBACK_LAUNCH_SECONDS)

    t_read = quick_seconds(rates, cc_log.READ_TOOLS)
    t_search = quick_seconds(rates, cc_log.SEARCH_TOOLS)
    t_edit = quick_seconds(rates, cc_log.EDIT_TOOLS)
    loop_calls = measured(rates["fix_loop_calls"]["median"], FALLBACK_LOOP_CALLS)
    tpl = measured(mr["tokens_per_written_line"], FALLBACK_TOKENS_PER_LINE)
    tps = mr["tokens_per_second"]
    start_ctx = measured(counts.get("start_context_tokens"), measured(mr["start_context_tokens"], 20000))
    growth = measured(mr["context_growth_per_turn"], 1500)
    typical_out = measured(mr["median_output_tokens"], 0)
    weights = [s["weight"] for s in scenarios]
    spread = rates.get("speed_factors") if counts.get("speed_spread", True) else None
    if spread:
        notes.append(f"range includes the speed spread of {spread['n']} past requests (real / predicted time)")

    sims = []
    for _ in range(n):
        si = rng.choices(range(len(scenarios)), weights)[0]
        s = scenarios[si]
        d = {f: draw(rng, s.get(f, 0)) for f in COUNT_FIELDS}
        runs = {k: draw(rng, v) for k, v in s.get("runs", {}).items()}
        tools = {k: draw(rng, v) for k, v in s.get("tools", {}).items()}
        bg = {k: draw(rng, v) for k, v in s.get("background", {}).items()}
        calls = (d["reads"] + d["searches"] + d["edits"] + sum(runs.values()) + sum(tools.values())
                 + sum(bg.values()) + d["fix_loops"] * (loop_calls + 1))
        turns = math.ceil(calls / mr["calls_per_turn"]) + 1
        write_tokens = d["write_lines"] * tpl
        step = max(growth, (d["read_lines"] * READ_TOKENS_PER_LINE + write_tokens) / turns)
        model_s = sum(sample_turn(rng, start_ctx + i * step) for i in range(turns))
        tool_s = (d["reads"] * t_read + d["searches"] * t_search + d["edits"] * t_edit
                  + d["fix_loops"] * loop_calls * t_edit
                  + sum(rng.choice(other_samples[k]) for k, n in tools.items() for _ in range(n))
                  + sum(bg.values()) * launch_s)
        cmd_s = sum(rng.choice(cmd_samples[k]) for k, n in runs.items() for _ in range(n))
        if verify:
            cmd_s += sum(rng.choice(cmd_samples[verify]) for _ in range(d["fix_loops"]))
        write_s = max(0.0, write_tokens - d["edits"] * typical_out) / tps if tps else 0.0
        raw = model_s + tool_s + cmd_s + write_s
        # One measured speed factor per run: real requests are fast or slow as a whole.
        factor = speed_factor(rng, spread, raw / 60) if spread else 1.0
        total = raw * factor
        # Background jobs run side by side: the last result comes when the longest one ends.
        bg_s = max((rng.choice(bg_samples[k]) for k, n in bg.items() for _ in range(n)), default=0.0)
        sims.append({"total": total, "wall": max(total, bg_s), "model": model_s, "tools": tool_s,
                     "commands": cmd_s, "writes": write_s, "turns": turns, "scenario": si})

    def percentiles(key: str) -> dict:
        values = sorted(r[key] for r in sims)
        return {q: values[int(q / 100 * (len(values) - 1))] / 60 for q in (10, 50, 90)}

    pct = percentiles("total")
    wall = percentiles("wall") if bg_names else None
    med = {k: statistics.median(r[k] for r in sims) for k in ("model", "tools", "commands", "writes", "turns")}
    by_scenario = []
    for i, s in enumerate(scenarios):
        own = [r["total"] for r in sims if r["scenario"] == i]
        by_scenario.append((s.get("name", f"#{i + 1}"), s["weight"], statistics.median(own) / 60 if own else 0.0))

    if not tps:
        notes.append("long-write time skipped: no measured tokens/second yet")
    if mr["tokens_per_written_line"] is None:
        notes.append(f"tokens per written line not measured yet: used {FALLBACK_TOKENS_PER_LINE}")
    if rates["fix_loop_calls"]["median"] is None:
        notes.append(f"fix-loop size not measured yet: used {FALLBACK_LOOP_CALLS} calls per loop")
    if not usable:
        notes.append("too few turns per context size: turn time sampled without context adjustment")

    # Simple baseline: likely tool calls of the main scenario x measured all-in seconds per call.
    # compare.py --report shows whether the full model beats it; if not, the simulation is not worth keeping.
    exp = likely_counts(expected_scenario(scenarios), set(declared_cmds))
    likely_calls = (exp["reads"] + exp["searches"] + exp["edits"] + exp["other_calls"] + exp["bookkeeping"] + exp["commands"]
                    + exp["fix_loops"] * loop_calls)
    spc = mr.get("seconds_per_call")
    return {"p10": pct[10], "p50": pct[50], "p90": pct[90], "wall": wall, "median_parts": med, "notes": notes,
            "by_scenario": by_scenario, "baseline": likely_calls * spc / 60 if spc else None,
            "likely_calls": likely_calls, "seconds_per_call": spc}


def scored_estimates() -> list[dict]:
    """Rows of actuals.jsonl (written by compare.py / the Stop hook), oldest first."""
    path = cc_log.DATA_DIR / "actuals.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def raw_of(row: dict, q: str) -> float:
    """The model's own number for a scored estimate (rows from before corrections only have the shown one)."""
    return float(row.get(f"raw_{q}", row[q]))


def log_spans(p10: float, p50: float, p90: float) -> tuple[float, float]:
    """Log distance from P50 down to P10 and up to P90 (at least 5%, so even a narrow range can widen)."""
    lo = math.log(p50 / p10) if p10 > 0 and p50 > 0 else 0.0
    hi = math.log(p90 / p50) if p50 > 0 and p90 > 0 else 0.0
    return max(lo, 0.05), max(hi, 0.05)


def correction(rows: list[dict]) -> dict | None:
    """Learned from the newest scored estimates: shift = median real/predicted time; width = the smallest
    log-space widening (or narrowing) of the model's range that would have held ~80% of those real times.
    Measured from this machine's own results, not a safety factor."""
    rows = [r for r in rows if r.get("active_min", 0) > 0 and raw_of(r, "p50") > 0][-RECENT_SCORED:]
    if len(rows) < MIN_SCORED:
        return None
    shift = statistics.median(r["active_min"] / raw_of(r, "p50") for r in rows)
    spans = [(math.log(r["active_min"] / (shift * raw_of(r, "p50"))),
              *log_spans(raw_of(r, "p10"), raw_of(r, "p50"), raw_of(r, "p90"))) for r in rows]
    width = 4.0
    for step in range(10, 81):                       # 0.5 .. 4.0
        if sum(-step / 20 * lo <= z <= step / 20 * hi for z, lo, hi in spans) >= TARGET_INSIDE * len(rows):
            width = step / 20
            break
    return {"n": len(rows), "shift": round(shift, 3), "width": width}


def corrected(p10: float, p50: float, p90: float, corr: dict) -> tuple[float, float, float]:
    lo, hi = log_spans(p10, p50, p90)
    mid = p50 * corr["shift"]
    return mid * math.exp(-corr["width"] * lo), mid, mid * math.exp(corr["width"] * hi)


def track_record(rows: list[dict]) -> str:
    rows = [r for r in rows if "active_min" in r]
    if not rows:
        return "no scored estimates yet"
    inside = sum(r["p10"] <= r["active_min"] <= r["p90"] for r in rows)
    return f"{inside} of {len(rows)} past estimates landed inside their range"


def main() -> None:
    parser = argparse.ArgumentParser(description="P10/P50/P90 AI execution time from operation-count scenarios.")
    parser.add_argument("counts", help="counts JSON file, or - for stdin")
    args = parser.parse_args()

    counts = json.loads(sys.stdin.read() if args.counts == "-" else Path(args.counts).read_text(encoding="utf-8"))
    rates_path = cc_log.DATA_DIR / "rates.json"
    if not rates_path.exists():
        sys.exit(f"Missing {rates_path}. Run calibrate.py first.")
    rates = json.loads(rates_path.read_text(encoding="utf-8"))
    project = cc_log.norm_path(counts.get("project", ""))
    # Unless told otherwise, the worker is the session that asked: same model, same reasoning effort, and it
    # carries on with its current context (turns get slower as context grows). "fresh_worker": a new session.
    session = cc_log.latest_turn_settings(project) if project else {"model": "", "effort": "", "ctx": 0}
    if not counts.get("worker_model"):
        counts["worker_model"] = session["model"] or "opus"
    if not counts.get("worker_effort") and session["effort"]:
        counts["worker_effort"] = session["effort"]
    if not counts.get("start_context_tokens") and session["ctx"] and not counts.get("fresh_worker"):
        counts["start_context_tokens"] = session["ctx"]
    model, mr = pick_model(rates, counts["worker_model"])
    res = simulate(counts, rates, mr, project)
    scored = scored_estimates()
    corr = correction(scored)
    p10, p50, p90 = corrected(res["p10"], res["p50"], res["p90"], corr) if corr else (res["p10"], res["p50"], res["p90"])

    est_id = "E" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:3]  # unique even within one second
    record = {"id": est_id, "ts": time.time(), "project": project, "model": model,
              "effort": counts.get("worker_effort", ""), "task": counts.get("task", ""),
              "task_type": counts.get("task_type", "other"), "counts": counts,
              "p10": round(p10, 2), "p50": round(p50, 2), "p90": round(p90, 2),
              "raw_p10": round(res["p10"], 2), "raw_p50": round(res["p50"], 2), "raw_p90": round(res["p90"], 2),
              "correction": corr,
              "median_turns": res["median_parts"]["turns"], "rates_generated": rates.get("generated"),
              "wall": {f"p{q}": round(v, 2) for q, v in res["wall"].items()} if res["wall"] else None,
              "baseline": round(res["baseline"], 2) if res["baseline"] else None}
    with open(cc_log.DATA_DIR / "estimates.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")

    p = res["median_parts"]
    print(f"Estimate {est_id} | worker {model} at effort {record['effort'] or 'unknown'} | {SIMULATIONS} simulated runs"
          f" | rates from {rates.get('generated')}")
    print(f"P10 {p10:.1f} min | P50 {p50:.1f} min | P90 {p90:.1f} min")
    if res["wall"]:
        w = res["wall"]
        print(f"Wall clock until the last background result: P10 {w[10]:.1f} | P50 {w[50]:.1f} | P90 {w[90]:.1f} min"
              " (background work runs on its own; the AI time above counts only its launch)")
    if corr:
        print(f"Corrected from the last {corr['n']} scored estimates (real/predicted x{corr['shift']:.2f}, range "
              f"x{corr['width']:.2f}). Model alone: P10 {res['p10']:.1f} | P50 {res['p50']:.1f} | P90 {res['p90']:.1f} min")
    print(f"Track record: {track_record(scored)}.")
    print("By scenario (median): " + " | ".join(f"{n} {m:.1f} min ({w:.0%})" for n, w, m in res["by_scenario"]))
    print(f"Median breakdown: {p['turns']:.0f} model turns {p['model'] / 60:.1f} min | tools {p['tools'] / 60:.1f} min"
          f" | shell commands {p['commands'] / 60:.1f} min | long writes {p['writes'] / 60:.1f} min")
    if res["baseline"]:
        print(f"Simple baseline: {res['likely_calls']:.0f} likely calls x {res['seconds_per_call']} s = {res['baseline']:.1f} min")
    for note in res["notes"]:
        print("  - " + note)
    print("Time excludes human waits (approvals, answers, manual checks).")


if __name__ == "__main__":
    main()
