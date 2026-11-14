# [UPDATED v1.0] history.py - past requests with their real active time and counts, so the time-estimator
# can anchor its scenarios on similar work that really happened on this machine (reference class).
# Read-only. Human waits are excluded, like everywhere else in the estimator.
# Usage: python history.py [--project PATH] [--grep WORD ...] [--everywhere] [--limit 15] [--days 60]
from __future__ import annotations

import argparse
import os
import re
import statistics
import time

import cc_log
from backtest import past_requests

TAG = re.compile(r"<[^>]+>")


def request_text(text: str) -> str:
    """A typed request as one readable line (slash-command tags removed)."""
    return " ".join(TAG.sub(" ", text).split())


def past_rows(project: str | None, days: int = 60, words: list[str] | None = None,
              min_seconds: float = 10) -> list[dict]:
    """Past requests, newest first. project=None means every project."""
    words = [w.lower() for w in (words or [])]
    rows = []
    for path, cwd, req, act in past_requests(time.time() - days * 86400, project=project):
        text = request_text(req["text"])
        if act["active_min"] * 60 < min_seconds or not all(w in text.lower() for w in words):
            continue
        c = act["counts"]
        rows.append({"start": req["start"], "minutes": act["active_min"], "edits": c["edits"], "reads": c["reads"],
                     "searches": c["searches"], "commands": c["commands"], "fix_loops": c["fix_loops"],
                     "other": c["other_calls"], "write_lines": c["write_lines"], "turns": act["turns"],
                     "project": cc_log.norm_path(cwd), "text": text})
    return sorted(rows, key=lambda r: r["start"], reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Past requests with their real time and counts.")
    parser.add_argument("--project", default=os.getcwd(), help="project folder (default: the current folder)")
    parser.add_argument("--grep", nargs="*", default=[], help="only requests whose text contains all these words")
    parser.add_argument("--everywhere", action="store_true", help="search all projects, not only this one")
    parser.add_argument("--limit", type=int, default=15, help="rows to show (default 15)")
    parser.add_argument("--days", type=int, default=60, help="history window (default 60)")
    args = parser.parse_args()

    project = None if args.everywhere else cc_log.norm_path(args.project)
    rows = past_rows(project, args.days, args.grep)
    scope = "all projects" if project is None else project
    if not rows:
        print(f"No past requests in {scope} (last {args.days} days)" + (f" matching {args.grep}" if args.grep else "")
              + ". Try --everywhere or fewer --grep words.")
        return
    minutes = [r["minutes"] for r in rows]
    print(f"{len(rows)} past requests in {scope}" + (f" matching {args.grep}" if args.grep else "") +
          f": median {statistics.median(minutes):.1f} min, P90 {sorted(minutes)[int(0.9 * (len(minutes) - 1))]:.1f} min"
          " (real active time, human waits excluded). Newest first:")
    print(f"  {'when':<11} {'min':>5} {'edits':>5} {'lines':>5} {'reads':>5} {'srch':>4} {'cmds':>4} {'loops':>5} "
          f"{'other':>5}  request")
    for r in rows[:args.limit]:
        where = f"[{r['project'].rsplit('/', 1)[-1]}] " if project is None else ""
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(r['start'])):<11} {r['minutes']:5.1f} {r['edits']:5} "
              f"{r['write_lines']:5} {r['reads']:5} {r['searches']:4} {r['commands']:4} {r['fix_loops']:5} "
              f"{r['other']:5}  {where}{r['text'][:80]}")


if __name__ == "__main__":
    main()
