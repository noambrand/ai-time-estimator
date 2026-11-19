# [UPDATED v1.1] autoscore.py - Claude Code Stop hook: scores time estimates automatically, so nobody
# has to run compare.py. Reads the hook JSON on stdin, looks only at this session's transcript (and its
# subagents), and scores the estimates made in it during the last day. An estimate's window ends at the user's
# next message after the work started (a new request), or where the next estimate for the same worker begins.
# Work done by a background agent is measured on that agent's own transcript (compare.resolve_window); an
# estimate no window can measure is stored as unscored with its reason. A window that is still running is stored
# as "open" and updated in place; once it closes the score is final and never redone. One log line when an
# estimate is first scored and one when its status changes - no repeats. Also recalibrates rates older than a
# day (or written in an older format), one session at a time.
# Silent and never fails the hook: all output goes to autoscore.log.
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import cc_log

WINDOW_SECONDS = 24 * 3600     # estimates older than this are no longer looked for in a session
# Calibrating takes about a minute of background CPU and the hook only runs while Claude Code is in use, so
# a daily refresh is cheap and keeps new sessions (a new model or effort, new commands) in the rates.
RECALIBRATE_DAYS = 1
CALIBRATE_LOCK_SECONDS = 900   # another session's calibration younger than this is still running
QUIET_SECONDS = 1800           # a session silent this long will not close an open window by itself
SETTLED = ("final", "unscored", "invalid")


def session_text(transcript: Path) -> str:
    """Text of the session transcript plus its subagent transcripts (where the time-estimator agent runs)."""
    parts = [transcript.read_text(encoding="utf-8", errors="replace")]
    for sub in sorted((transcript.parent / transcript.stem / "subagents").glob("*.jsonl")):
        parts.append(sub.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def estimates_in_session(transcript: Path, now: float, max_age: float | None = WINDOW_SECONDS) -> list[dict]:
    import compare
    recent = [e for e in compare.read_jsonl(cc_log.DATA_DIR / "estimates.jsonl")
              if max_age is None or now - e["ts"] <= max_age]
    if not recent:
        return []
    text = session_text(transcript)
    return sorted((e for e in recent if e["id"] in text), key=lambda e: e["ts"])


def task_end(events: list[dict], start: float) -> float:
    import compare
    return compare.task_end(events, start)


def statuses() -> dict:
    import compare
    return {r["estimate_id"]: r.get("status", "final") for r in compare.read_jsonl(cc_log.DATA_DIR / "actuals.jsonl")}


def score_window(est: dict, transcript: Path, now: float, next_ts: float = float("inf"),
                 force_closed: bool = False) -> dict | None:
    """Score one estimate on the window compare.resolve_window picks. Returns the stored row, or None."""
    import compare
    win = compare.resolve_window(est, transcript, now, next_ts=next_ts, force_closed=force_closed)
    if win["kind"] == "none":
        return None
    if win["kind"] == "unscored":
        return compare.store_unscored(est, transcript, win["reason"]) if win["final"] else None
    with contextlib.redirect_stdout(io.StringIO()):
        return compare.score_and_log(est, transcript, True, win["until"], win["events"], win["side"],
                                     status="final" if win["closed"] else "open", window=win["kind"])


def score_session(transcript: Path, now: float | None = None, only: set | None = None,
                  rescore: bool = False) -> list[dict]:
    """Score the recent estimates of this session that have no settled score yet. Returns the stored rows.
    only / rescore (compare.py --rescore): these estimate ids of any age, settled or not (invalid stays invalid)."""
    import compare
    now = time.time() if now is None else now
    found = estimates_in_session(transcript, now, None if rescore else WINDOW_SECONDS)
    done = statuses()
    rows = []
    for i, est in enumerate(found):
        if (only is not None and est["id"] not in only) or (not rescore and done.get(est["id"]) in SETTLED):
            continue                                   # scored once: never redone by the hook
        worker = compare.worker_of(est)
        later = [e for e in found[i + 1:] if compare.worker_of(e) == worker]
        next_ts = later[0]["ts"] if later else float("inf")
        if later:
            main_events, _ = compare.session_streams(transcript, est["ts"])
            if not any(e["kind"] == "human" and e["ts"] < next_ts for e in main_events):
                continue  # superseded: the estimator re-ran estimate.py for the same worker before anyone replied
        row = score_window(est, transcript, now, next_ts)
        if row:
            rows.append(row)
    return rows


def finalize_quiet(now: float) -> list[dict]:
    """Open windows whose session went quiet (no next message ever came): final as they ended."""
    import compare
    estimates = {e["id"]: e for e in compare.read_jsonl(cc_log.DATA_DIR / "estimates.jsonl")}
    rows = []
    for row in compare.read_jsonl(cc_log.DATA_DIR / "actuals.jsonl"):
        session = Path(row.get("session") or "")
        est = estimates.get(row["estimate_id"])
        if row.get("status") != "open" or not est or not session.is_file():
            continue
        if now - session.stat().st_mtime >= QUIET_SECONDS:
            new = score_window(est, session, now, force_closed=True)
            if new:
                rows.append(new)
    return rows


def log_line(row: dict, before: str | None) -> str | None:
    """One line when an estimate is first scored or its status changes; None for an unchanged re-score."""
    status = row.get("status", "final")
    if status == before:
        return None
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if status == "unscored":
        return f"{stamp} unscored {row['estimate_id']}: {row.get('reason', '')}"
    note = "" if status == "final" else f" ({status})"
    return f"{stamp} scored {row['estimate_id']}: actual {row['active_min']:.1f} min vs P50 {row['p50']:.1f}{note}"


def rates_format(rates: Path) -> int:
    """The "format" number at the head of rates.json (0 for files written before it existed)."""
    try:
        with rates.open(encoding="utf-8") as handle:
            head = handle.read(200)
    except OSError:
        return 0
    found = re.search(r'"format":\s*(\d+)', head)
    return int(found.group(1)) if found else 0


def recalibrate_if_stale(now: float) -> bool:
    """Measure the rates again when rates.json is a day old, missing, or written in an older format. Many
    sessions stop at once: a lock file keeps it to one calibration at a time."""
    import calibrate
    rates = cc_log.DATA_DIR / "rates.json"
    if (rates.exists() and now - rates.stat().st_mtime < RECALIBRATE_DAYS * 86400
            and rates_format(rates) >= calibrate.RATES_FORMAT):
        return False
    lock = cc_log.DATA_DIR / "calibrating.lock"
    if lock.exists() and now - lock.stat().st_mtime < CALIBRATE_LOCK_SECONDS:
        return False
    cc_log.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    try:
        with contextlib.redirect_stdout(io.StringIO()), mock_argv(["calibrate.py"]):
            calibrate.main()
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()
    return True


@contextlib.contextmanager
def mock_argv(argv: list[str]):
    saved = sys.argv
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = saved


def main() -> None:
    log = cc_log.DATA_DIR / "autoscore.log"
    try:
        hook = json.loads(sys.stdin.read() or "{}")
        transcript = Path(hook.get("transcript_path") or "")
        if not transcript.is_file():
            return
        before = statuses()
        rows = score_session(transcript) + finalize_quiet(time.time())
        lines = [line for line in (log_line(r, before.get(r["estimate_id"])) for r in rows) if line]
        if recalibrate_if_stale(time.time()):
            lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} recalibrated rates.json")
        if lines:
            with open(log, "a", encoding="utf-8") as out:
                out.write("".join(line + "\n" for line in lines))
    except Exception:  # a hook must never break the session; keep the error for diagnosis
        try:
            with open(log, "a", encoding="utf-8") as out:
                out.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc())
        except OSError:
            pass


if __name__ == "__main__":
    main()
