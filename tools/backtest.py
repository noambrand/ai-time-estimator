# [UPDATED v1.1] backtest.py - test the time model on your own history, without waiting for estimates.
# Every past request (a typed message up to the next one) is replayed: its REAL tool calls go into
# estimate.simulate() as one scenario, and the predicted P10/P50/P90 are compared with its real active time.
# Counts are exact here, so this measures the time model alone, not the estimator's counting.
# Rates and the speed spread are measured in memory with the current calibrate.py code on the requests made
# BEFORE the test window (--holdout, default the newest 20% of requests), so the numbers are out-of-sample;
# rates.json is not touched. calibrate.py also calls replay()/speed_factors() to store the speed spread.
# Usage: python backtest.py [--days 60] [--holdout 0.2] [--project PATH] [--sims 200] [--worst 0]
from __future__ import annotations

import argparse
import random
import statistics
import time

import calibrate
import cc_log
import compare
import estimate

BANDS = ((0.0, 5.0, "< 5 min"), (5.0, 20.0, "5-20 min"), (20.0, float("inf"), "> 20 min"))
MIN_FACTORS = 30          # past requests needed before the speed spread is used at all
MIN_BAND_FACTORS = 20     # below this, a size band uses the factors of all bands


def request_counts(act: dict) -> dict:
    """estimate.py input for one past request: its real calls as one scenario. No fix loops: the
    reruns and fix edits of a past request are already among its real calls."""
    c = act["counts"]
    scenario = {"name": "real", "reads": c["reads"], "searches": c["searches"], "edits": c["edits"],
                "write_lines": c["write_lines"], "runs": act["runs"], "tools": act["tools"]}
    counts = {"commands": [{"cmd": k} for k in act["runs"]], "scenarios": [scenario]}
    if act["first_ctx"]:
        counts["start_context_tokens"] = act["first_ctx"]
    if act["effort"]:
        counts["worker_effort"] = act["effort"]
    return counts


def past_requests(since_ts: float, until_ts: float = float("inf"), project: str | None = None):
    """(transcript path, cwd, request window, measured request) for every typed request in main sessions."""
    for path in cc_log.find_transcripts(since_ts):
        if "subagents" in path.parts:
            continue
        stream = cc_log.load_streams(path, since_ts, until_ts).get("main")
        if not stream or (project and cc_log.norm_path(stream["cwd"]) != project):
            continue
        for req in compare.requests(stream["events"]):
            act = compare.measure(stream["events"], [], req["start"], req["until"])
            if act["turns"]:
                yield path, stream["cwd"], req, act


def replay(rates: dict, since_ts: float, until_ts: float = float("inf"), project: str | None = None,
           sims: int = 200, min_seconds: float = 10) -> list[dict]:
    rows = []
    for path, cwd, req, act in past_requests(since_ts, until_ts, project):
        if act["active_min"] * 60 < min_seconds or act["model"] not in rates["models"]:
            continue
        res = estimate.simulate(request_counts(act), rates, rates["models"][act["model"]], cc_log.norm_path(cwd), n=sims)
        rows.append({"actual": act["active_min"], "p10": res["p10"], "p50": res["p50"], "p90": res["p90"],
                     "baseline": res["baseline"], "model": act["model"], "effort": act["effort"],
                     "session": path.stem[:8], "text": " ".join(req["text"].split())[:70]})
    return rows


def speed_factors(rows: list[dict]) -> dict | None:
    """Spread of real / predicted time over past requests (real counts in), per predicted size band.
    estimate.simulate() draws one factor per simulated run, so a whole run is fast or slow together."""
    rng = random.Random(3)
    pooled = [r["actual"] / r["p50"] for r in rows if r["p50"] > 0]
    if len(pooled) < MIN_FACTORS:
        return None

    def keep(values: list[float]) -> list[float]:
        picked = rng.sample(values, calibrate.MAX_SAMPLES) if len(values) > calibrate.MAX_SAMPLES else values
        return [max(0.001, round(v, 3)) for v in picked]

    bands = []
    for low, high, label in BANDS:
        factors = [r["actual"] / r["p50"] for r in rows if r["p50"] > 0 and low <= r["p50"] < high]
        bands.append({"label": label, "max_p50": None if high == float("inf") else high,
                      "factors": keep(factors if len(factors) >= MIN_BAND_FACTORS else pooled)})
    return {"n": len(pooled), "bands": bands}


def holdout_split(since_ts: float, share: float) -> float:
    """The time after which the newest `share` of typed requests were made."""
    starts = sorted(e["ts"] for path in cc_log.find_transcripts(since_ts) if "subagents" not in path.parts
                    for e in cc_log.load_streams(path, since_ts).get("main", {"events": []})["events"]
                    if e["kind"] == "human")
    return starts[int(len(starts) * (1 - share))] - 0.001 if starts else time.time()


def summary(rows: list[dict]) -> str:
    n = len(rows)
    inside = sum(r["p10"] <= r["actual"] <= r["p90"] for r in rows) / n
    below = sum(r["actual"] < r["p10"] for r in rows) / n
    error = statistics.median(abs(r["p50"] - r["actual"]) / r["actual"] for r in rows)
    ratio = statistics.median(r["actual"] / r["p50"] for r in rows if r["p50"])
    return (f"n={n:4} | inside P10-P90 {inside:4.0%} (below {below:.0%}, above {1 - inside - below:.0%}) | "
            f"median P50 error {error:4.0%} | median actual/P50 {ratio:.2f}")


def report(rows: list[dict], worst: int = 0) -> None:
    if not rows:
        print("  No past requests to replay.")
        return
    print(f"  {'all':<13} {summary(rows)}")
    # Grouped by PREDICTED size: grouping by the actual time would make small requests look
    # over-predicted and big ones under-predicted even for a perfect model (regression to the mean).
    for low, high, label in BANDS:
        band = [r for r in rows if low <= r["p50"] < high]
        if band:
            print(f"  P50 {label:<9} {summary(band)}")
    paired = [r for r in rows if r["baseline"]]
    if paired:
        base_error = statistics.median(abs(r["baseline"] - r["actual"]) / r["actual"] for r in paired)
        closer = sum(abs(r["p50"] - r["actual"]) < abs(r["baseline"] - r["actual"]) for r in paired)
        print(f"  Simple baseline (calls x seconds per call): median error {base_error:.0%} | "
              f"P50 closer than the baseline in {closer}/{len(paired)}")
    for r in sorted(rows, key=lambda r: abs(r["p50"] - r["actual"]) / r["actual"], reverse=True)[:worst]:
        print(f"  miss: actual {r['actual']:6.1f} vs P50 {r['p50']:6.1f} min | {r['session']} {r['effort'] or '-'} | {r['text']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay past requests through the time model.")
    parser.add_argument("--days", type=int, default=60, help="history window (default 60)")
    parser.add_argument("--holdout", type=float, default=0.2,
                        help="test on this newest share of requests, measure on the older ones (default 0.2; 0 = in-sample)")
    parser.add_argument("--project", help="only test requests made in this folder")
    parser.add_argument("--sims", type=int, default=200, help="simulated runs per request (default 200)")
    parser.add_argument("--min-seconds", type=float, default=10, help="skip shorter requests (default 10)")
    parser.add_argument("--worst", type=int, default=0, help="also list the N worst misses")
    args = parser.parse_args()

    started = now = time.time()
    since = now - args.days * 86400
    project = cc_log.norm_path(args.project) if args.project else None
    split = holdout_split(since, args.holdout) if args.holdout else now
    rates, _, files = calibrate.measure_rates(args.days, split)
    spread = speed_factors(replay(rates, since, split, sims=100, min_seconds=args.min_seconds))
    test_since = split if args.holdout else since
    window = (f"the newest {args.holdout:.0%} of requests (since {time.strftime('%Y-%m-%d %H:%M', time.localtime(split))}),"
              f" with rates and spread measured on the older ones" if args.holdout else f"the last {args.days} days, in-sample")
    print(f"Backtest on past requests of {window} ({len(files)} transcripts). "
          f"Real counts in, so this tests the time model, not the counting.")
    print("Per-call model only:")
    report(replay(rates, test_since, now, project, args.sims, args.min_seconds))
    if spread:
        print(f"With the speed spread of {spread['n']} earlier requests:")
        report(replay(dict(rates, speed_factors=spread), test_since, now, project, args.sims, args.min_seconds),
               args.worst)
    print(f"Target: ~80% inside P10-P90. ({time.time() - started:.0f} s)")


if __name__ == "__main__":
    main()
