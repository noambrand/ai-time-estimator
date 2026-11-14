# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude Code add-on that estimates how long a Claude Code worker will take on a task, measured in AI wall-clock time. The LLM only counts operations (reads, edits, fix loops, command runs) in 2–3 weighted scenarios. Python scripts turn those counts into P10/P50/P90 minutes using rates measured from the user's own transcripts in `~/.claude/projects/**/*.jsonl`. Pure Python standard library (3.9+): there are no dependencies and no build step. `README.md` is the user-facing description.

## Source vs installed copy

This folder is the source. `python install.py` copies:
- `agents/time-estimator.md` → `~/.claude/agents/` (subagent, runs on sonnet)
- `commands/estimate-here.md` → `~/.claude/commands/` (`/estimate-here`: the same procedure, run in the current session)
- `tools/*.py` → `~/.claude/ai-time-estimator/`, then runs `calibrate.py` once
- registers `autoscore.py` as an async **Stop hook** in `~/.claude/settings.json` (backup `settings.json.bak`; matched by the script name, so re-running updates instead of duplicating)
- adds a marked `<!-- ai-time-estimator:start/end -->` "Time estimates" rule to `~/.claude/CLAUDE.md` (replaced in place on re-run)
- `--no-hook` / `--no-rule` skip those two edits; `--uninstall` removes everything it added (`--purge` also deletes the data folder). `tests/test_install.py` checks that an install followed by an uninstall gives back the user's files unchanged.

CI (`.github/workflows/tests.yml`) runs the suite on Linux (Python 3.9 and 3.13), macOS and Windows, so keep paths in tests OS-neutral (forward slashes; a backslash is no separator on POSIX).

The user never runs the scripts. They ask Claude how long something will take; the rule sends that to the agent, which answers "About N min, likely A–B min". After each Claude turn the Stop hook re-scores the estimates of that session. Its log is `~/.claude/ai-time-estimator/autoscore.log`.

The agent and command prompts call the **installed** scripts by path (`~/.claude/ai-time-estimator/...`). An edit here has no effect until `install.py` runs again. Run it with the same Python that Claude Code uses (Windows or WSL), because `Path.home()` decides where the files go.

## Commands

```bash
python -m unittest discover -s tests -v        # full suite (stdlib unittest, no pytest needed)
python -m unittest discover -s tests -p test_estimate.py -k fix_loop   # one file / matching tests
python tools/calibrate.py [--days 60]          # parse transcripts -> ~/.claude/ai-time-estimator/rates.json
python tools/estimate.py counts.json           # or: python tools/estimate.py - < counts.json
python tools/compare.py [--id E...] [--session x.jsonl] [--until 2026-11-13T14:05:00]
python tools/compare.py --report               # summary of all past comparisons
python tools/backtest.py [--holdout 0.2] [--worst 10]   # replay past requests through the time model (~40 s)
python tools/history.py [--project P] [--grep w] [--everywhere]   # past requests with real minutes and counts
```

The scripts import each other by bare module name (`import cc_log`, `from estimate import ...`), so run them as files in `tools/`, never as a package. `calibrate.py`, `estimate.py` and `compare.py` read and write the **real** data directory `~/.claude/ai-time-estimator/`, even when run from this source folder. Each `estimate.py` run appends a record to `estimates.jsonl`, and each `compare.py` run rewrites `actuals.jsonl`. A test run therefore leaves an entry in the user's history that `--report` will count. `backtest.py` and `history.py` only read; the backtest measures its rates in memory from the current source code, so run it before and after any change to the time model and compare the "inside P10-P90" and "median P50 error" lines (out-of-sample: tested on the newest 20% of requests, measured on the older ones). History is limited by Claude Code's transcript cleanup (`cleanupPeriodDays`, 30 days by default).

The suite in `tests/` never touches `~/.claude`: `fixtures.IsolatedTest` patches `cc_log.PROJECTS_DIR` and `cc_log.DATA_DIR` to a temp folder, `fixtures.Transcript` builds synthetic `.jsonl` transcripts, and `fixtures.make_rates()` gives rates simple enough that `test_estimate.py` checks totals computed by hand (see the comments there). Tests marked `@unittest.expectedFailure` pin down known bugs. When you fix one, remove the decorator.

## Architecture

Data flow: transcripts → `calibrate.py` (+ `backtest.replay()` for the speed spread) → `rates.json` → `estimate.py` (+ scenario JSON from the agent, anchored on `history.py`) → `estimates.jsonl` → Stop hook `autoscore.py` / `compare.py` (re-read transcripts after the task) → `actuals.jsonl` → `estimate.correction()` (self-correcting ranges after 10 scores) and `compare.py --report` (per-count bias the agent applies in Step 0).

Two layers of measured correction, kept apart on purpose: the **speed spread** (from replaying hundreds of past requests with their real counts: the time model's own error) lives in `rates.json`; the **self-correction** (from scored estimates: mostly the agent's counting error) is computed from `actuals.jsonl` at estimate time. `estimates.jsonl` and `actuals.jsonl` keep both the shown numbers (`p10/p50/p90`) and the model's own (`raw_p10/raw_p50/raw_p90`); the correction learns from the raw ones.

- **`cc_log.py`** is the only module that knows the Claude Code transcript format: streamed assistant chunks merged by `requestId`, the highest usage kept, tool time = gap from `tool_use` to `tool_result`, sidechain/subagent streams split out. When Claude Code changes its log format, only this file should change. `calibrate.py` prints `FORMAT CHECK FAILED` when it stops recognizing turns or usage. It also holds the shared thresholds (stall/outlier limits, `BIG_WRITE_LINES`), the tool categories, `command_keys()` (a shell command is keyed by the first 3, then 2 words of its main step, so measurements match across runs), `norm_path()` (`C:\x`, `/c/x` and `/mnt/c/x` are the same project) and `RetryTracker` (a fix loop = a Bash command that failed, then at least one Edit/Write, then the same command again; a retry without an edit is not a loop). "Failed" also covers a command whose exit status a pipe, `;` or `||` hid, when its output matches `FAILURE_MARKERS` (`looks_failed()`). Task-list tools (`BOOKKEEPING_TOOLS`) get their own `bookkeeping` count, separate from `other_calls`. User-type lines the user did not write (background-task notices, messages from other Claude sessions, compaction summaries, skill text, local `/command` and `!` output; see `SYSTEM_PREFIXES` / `is_system_line()`) become `system` events, never `human`: no human wait before them, never the "go" or the end of a task. About 40% of user-type lines are such lines. Notices carry the `tool_use_id` of the background tool they end. `human` events keep the start of their text; turns carry the reasoning `effort`.
- **`calibrate.py`** keeps per-model turn latencies bucketed by context size (`BUCKET_TOKENS`), also per reasoning effort (`by_effort`; max is ~40% slower than xhigh), per-tool and per-command duration samples (per project and `"*"`), output tokens/sec, tokens per written line, calls per turn, and the size of fix loops. Turns that write `BIG_WRITE_LINES`+ lines are left out of the turn buckets, because `estimate.py` times them by output speed (no double count). A background tool (Agent, Workflow, background Bash) lasts from launch to its completion notice, not to its "launched" result; `INSTANT_TOOLS` (Read/Edit/task-list...) are capped at 30 s, since longer gaps were waits, not work. It then replays past requests (`backtest.replay()`) and stores `speed_factors`: real ÷ predicted time per predicted-size band.
- **`estimate.py`** runs a Monte Carlo (`SIMULATIONS` draws, seeded RNG so the output is deterministic). Each draw picks a scenario by weight, turns tool calls into model turns through `calls_per_turn`, samples turn times (for the worker's effort) as the context grows, and adds tool, command, fix-loop and long-write time; then multiplies the run by one drawn speed factor, because real requests are fast or slow as a whole (independent per-call draws alone held only ~40% of real times inside P10–P90). Unless the input says otherwise, the worker is the asking session: `cc_log.latest_turn_settings()` gives its model, effort and current context (`"fresh_worker": true` for a new session). Unmeasured commands/tools fall back to `fallback_seconds` and are flagged `UNMEASURED`. It also computes a deliberately simple baseline (likely calls × measured seconds per call). The design rule: if the baseline is as accurate as the simulation (`backtest.py` shows both), the simulation is not worth keeping. With 10+ scored estimates, `correction()` shifts and widens/narrows the shown range from the newest 20; it prints the model's own numbers too, plus a `Track record:` line. It still accepts the v1.0 flat (scenario-less) input format through `scenarios_of()`.
- **`backtest.py`** splits main transcripts into requests (`compare.requests()`: a typed message up to the next one), measures each with `compare.measure()` (which also returns the commands run, tool names, first context, model and effort), and replays its real calls through `simulate()`. Result bands are grouped by predicted P50 (grouping by the real time would fake a size bias). `history.py` lists the same requests for the agent.
- **`compare.py`** finds the session of an estimate by searching the transcripts for the estimate id. That is why the agent must print the id. If the id is not found, it guesses the busiest session of the same project and warns. It measures actual counts and time with human waits excluded (typed messages, answers to `AskUserQuestion`, and quick tools that waited 30+ s on an approval), picks the closest scenario, and names the count with the biggest miss. It imports `scenarios_of`, `likely_counts` and `expected_scenario` from `estimate.py`, so predicted and actual counts use the same field definitions (`FIELDS`).

- **`autoscore.py`** (Stop hook) reads the hook JSON (`transcript_path`, a Windows path on Windows), finds estimate ids from the last 24 h in that transcript or its `subagents/`, and calls `compare.score_and_log()` silently. An estimate's window runs from `compare.work_start()` to the user's next message after it (`task_end()`) or the next estimate, whichever comes first. An estimate re-run before anyone replied (agent Step 4) is skipped as superseded. Estimator commands and task-list calls never count as work (`compare.is_work()`). It recalibrates when `rates.json` is 14+ days old, and it never raises: errors go to `autoscore.log`.

## Coupling to keep in sync

- The input JSON schema documented in `agents/time-estimator.md` Step 3 (scenario fields, `runs`, `tools`, `commands[].verify` and `fallback_seconds`, the `task_type` values) must match what `estimate.py` reads (`COUNT_FIELDS`, `simulate()`).
- The bias thresholds in the agent prompt (Step 0: 5+ comparisons, bias >1.3 or <0.7) read the output format of `compare.py --report`.
- The agent's answer line copies `estimate.py`'s `P10 | P50 | P90` line (already corrected) and its `Track record:` line; Step 1 calls `history.py`. Keep those output formats and the prompt in step.
- The agent's rules are part of the design: it never states minutes that did not come from `estimate.py`, never uses human effort or complexity as a basis for time, and puts uncertainty only into the scenarios (no safety multipliers). `compare.py` reinforces this ("Fix the counting rule, not a multiplier"). The speed spread and the self-correction are not exceptions: both are measured from this machine's results and printed as such.
- Each file starts with a `# [UPDATED vX.Y] <name> - <purpose>` header line. Bump the version when you change a file.
