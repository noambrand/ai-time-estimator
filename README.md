# AI Time Estimator for Claude Code

Ask Claude Code *"how long will this take?"* and get an answer in **AI time**, measured from your own sessions:

> **About 7 min, likely 4–16 min** (8 of 10 past estimates landed inside their range)

Claude Code works at its own pace: model turns, tool calls, test runs, fix loops. That pace has little to do with how long the same task would take a person. This add-on learns it from your local Claude Code transcripts, and scores itself after every task.

## How it works

1. A `time-estimator` subagent inspects the repo (read-only), looks up similar past requests, and writes 2–3 scenarios of the work (smooth / expected / rough) as counts: files read, edits, lines written, command runs, fix loops.
2. `estimate.py` turns those counts into P10 / P50 / P90 minutes with a Monte Carlo simulation, using rates measured on your machine:
   - seconds per model turn, by model, reasoning effort and context size;
   - the run time of each tool and shell command, including background agents up to their completion notice;
   - output speed for long writes;
   - a **speed spread**: real ÷ predicted time of your past requests, because a real request is fast or slow as a whole.
3. A Stop hook (`autoscore.py`) compares every estimate with what really happened. The time you spend reading, answering or approving is left out.
4. After 10 scored estimates, the ranges correct themselves from your results, and each answer carries its track record.

The model never makes up minutes. It only counts operations, and the scripts do the arithmetic.

## Requirements

- Claude Code (CLI, desktop app or IDE extension) on Windows, macOS or Linux / WSL
- Python 3.9 or newer, standard library only: nothing to `pip install`
- Some Claude Code history on the machine. The more sessions, the better the rates.

## Install

```bash
git clone https://github.com/noambrand/ai-time-estimator.git
cd ai-time-estimator
python install.py
```

Use the same Python that Claude Code's shell finds (on Windows: the one Git Bash runs). `install.py`:

- copies the `time-estimator` agent to `~/.claude/agents/`, the `/estimate-here` command to `~/.claude/commands/`, and the scripts to `~/.claude/ai-time-estimator/`;
- adds one async **Stop hook** to `~/.claude/settings.json` (your previous file is saved as `settings.json.bak`);
- adds a short, marked "Time estimates" section to `~/.claude/CLAUDE.md`, so Claude uses the estimator whenever you ask how long something will take;
- measures your rates (about a minute).

Options: `--no-hook` (no automatic scoring), `--no-rule` (leave `~/.claude/CLAUDE.md` alone). Run it again at any time: it updates in place. Start a new Claude Code session afterwards.

## Use

- Ask in plain words: *"how long will it take to add CSV export?"* Claude hands the question to the time-estimator and answers in one line, followed by what drives the range.
- Or type `/estimate-here <task>` to estimate inside the current session; Claude then waits for your go.

Scoring is automatic, and the rates are refreshed every 14 days. You never need to run a script yourself.

## Privacy

Everything stays on your machine. The estimator reads Claude Code's transcripts in `~/.claude/projects/` and writes only to `~/.claude/ai-time-estimator/` (`rates.json`, `estimates.jsonl`, `actuals.jsonl`, `autoscore.log`). It makes no network calls.

## How accurate is it?

Check it on your own history:

```bash
python tools/backtest.py
```

The backtest replays your past requests with their real tool calls. It measures rates on the older 80% of your requests and tests on the newest 20%, then reports how often the real time landed inside P10–P90.

On the author's machine (72 held-out requests):

| | inside P10–P90 | median actual ÷ P50 | median P50 error |
|---|---|---|---|
| per-call model only | 39% | 0.88 | 67% |
| with the measured speed spread | **71%** (target 80%) | 1.06 | 67% |

A single request varies a lot, even when its tool calls are known exactly, so trust the range more than the middle number. The backtest checks the time model only; how well the agent counts the work is what the automatic scoring and the self-correction track.

## Limitations

- It relies on Claude Code's local transcript format, which is internal and can change. If `calibrate.py` stops recognizing model turns it prints `FORMAT CHECK FAILED`; `tools/cc_log.py` is the only file to adapt.
- History reaches back as far as Claude Code keeps transcripts (`cleanupPeriodDays`, 30 days by default).
- A task ends when you send your next message. If Claude asks you something halfway, the score stops at that point.
- It measures AI time only: approvals, answers and manual checks are not part of the estimate.

## Uninstall

```bash
python install.py --uninstall           # removes the agent, command, scripts, hook and CLAUDE.md section; keeps your history
python install.py --uninstall --purge   # also deletes ~/.claude/ai-time-estimator
```

## Development

```bash
python -m unittest discover -s tests -v       # the tests never touch ~/.claude
python tools/backtest.py --worst 10           # accuracy on your history, with the worst misses
python tools/history.py --everywhere --grep test
```

Changes take effect after `python install.py`. [`CLAUDE.md`](CLAUDE.md) explains the architecture, for contributors and for Claude Code.

## Credits

Transcript parsing follows the approach of [dioptx/cctime](https://github.com/dioptx/cctime) (MIT): streamed chunks merged by `requestId`, tool time measured from `tool_use` to `tool_result`.

## License

[MIT](LICENSE) © 2026 Noam Brand
