# [UPDATED v1.0] autoscore.py - Claude Code Stop hook: scores time estimates automatically, so nobody
# has to run compare.py. Reads the hook JSON on stdin, looks only at this session's transcript (and its
# subagents), and re-scores every estimate made in it during the last day. An estimate's window ends at the
# user's next message after the work started (a new request), or where the next estimate of the same session
# begins, whichever comes first. Also recalibrates when rates.json is older than 14 days.
# Silent and never fails the hook: all output goes to autoscore.log.
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import traceback
from pathlib import Path

import cc_log

WINDOW_SECONDS = 24 * 3600     # estimates older than this are final and no longer re-scored
RECALIBRATE_DAYS = 14


def session_text(transcript: Path) -> str:
    """Text of the session transcript plus its subagent transcripts (where the time-estimator agent runs)."""
    parts = [transcript.read_text(encoding="utf-8", errors="replace")]
    for sub in sorted((transcript.parent / transcript.stem / "subagents").glob("*.jsonl")):
        parts.append(sub.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def estimates_in_session(transcript: Path, now: float) -> list[dict]:
    import compare
    recent = [e for e in compare.read_jsonl(cc_log.DATA_DIR / "estimates.jsonl") if now - e["ts"] <= WINDOW_SECONDS]
    if not recent:
        return []
    text = session_text(transcript)
    return sorted((e for e in recent if e["id"] in text), key=lambda e: e["ts"])


def task_end(events: list[dict], start: float) -> float:
    """The task ends where the user's next message begins a new request. Without one it is still running."""
    nxt = next((e["ts"] for e in events if e["kind"] == "human" and e["ts"] > start), None)
    return nxt - 0.001 if nxt is not None else float("inf")


def score_session(transcript: Path, now: float | None = None) -> list[dict]:
    """Score every recent estimate of this session. Returns the stored rows."""
    import compare
    now = time.time() if now is None else now
    found = estimates_in_session(transcript, now)
    rows = []
    for i, est in enumerate(found):
        until = found[i + 1]["ts"] if i + 1 < len(found) else now
        main_events, side = compare.session_streams(transcript, est["ts"])
        if not any(e["kind"] == "turn" for e in main_events):
            continue
        if i + 1 < len(found) and not any(e["kind"] == "human" and e["ts"] < until for e in main_events):
            continue  # superseded: the estimator re-ran estimate.py before anyone replied
        until = min(until, task_end(main_events, compare.work_start(main_events, est["ts"], until)))
        with contextlib.redirect_stdout(io.StringIO()):
            rows.append(compare.score_and_log(est, transcript, True, until, main_events, side))
    return rows


def recalibrate_if_stale(now: float) -> bool:
    rates = cc_log.DATA_DIR / "rates.json"
    if rates.exists() and now - rates.stat().st_mtime < RECALIBRATE_DAYS * 86400:
        return False
    import calibrate
    with contextlib.redirect_stdout(io.StringIO()), mock_argv(["calibrate.py"]):
        calibrate.main()
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
        rows = score_session(transcript)
        recalibrated = recalibrate_if_stale(time.time())
        if rows or recalibrated:
            with open(log, "a", encoding="utf-8") as out:
                for r in rows:
                    out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} scored {r['estimate_id']}: "
                              f"actual {r['active_min']:.1f} min vs P50 {r['p50']:.1f}\n")
                if recalibrated:
                    out.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} recalibrated rates.json\n")
    except Exception:  # a hook must never break the session; keep the error for diagnosis
        try:
            with open(log, "a", encoding="utf-8") as out:
                out.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc())
        except OSError:
            pass


if __name__ == "__main__":
    main()
