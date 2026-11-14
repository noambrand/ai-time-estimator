<div align="center">

![AI Time Estimator for Claude Code: a guess of 2-3 days, struck out in red pencil, next to a measured estimate of about 7 minutes, likely 4 to 16](assets/banner.svg)

</div>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://github.com/noambrand/ai-time-estimator/releases/latest"><img src="https://img.shields.io/github/v/release/noambrand/ai-time-estimator?label=version&color=brightgreen" alt="Latest release"></a>
  <a href="https://github.com/noambrand/ai-time-estimator/actions/workflows/tests.yml"><img src="https://github.com/noambrand/ai-time-estimator/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/built%20for-Claude%20Code-8A63D2" alt="Built for Claude Code">
  <img src="https://img.shields.io/badge/python-3.9%2B-3776AB" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/dependencies-none-brightgreen" alt="No dependencies">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Windows, macOS, Linux">
  <a href="https://github.com/noambrand/ai-time-estimator/stargazers"><img src="https://img.shields.io/github/stars/noambrand/ai-time-estimator?style=flat&color=yellow" alt="GitHub stars"></a>
</p>

<h3 align="center">Ask Claude Code how long a task will take, and get an answer in its own working minutes, measured on your machine.</h3>

<p align="center">
  Claude estimates like a human developer: "2–3 days" for work it finishes before lunch. This add-on has Claude count the work instead of guessing, times it with rates measured from your own sessions, and checks itself after every task.
</p>

<div align="center">

![Example session: asked how long CSV export will take, Claude answers about 7 minutes, likely 4 to 16, shows three scenarios, and after the work the estimate is scored automatically: 6.2 minutes, inside the range](assets/demo.svg)

<sub>Example session (illustrative numbers)</sub>

</div>

<p align="center">
  <a href="#quick-start">Quick start</a> &bull;
  <a href="#how-it-works">How it works</a> &bull;
  <a href="#how-accurate-is-it">Accuracy</a> &bull;
  <a href="#privacy">Privacy</a> &bull;
  <a href="#uninstall">Uninstall</a> &bull;
  <a href="https://github.com/noambrand/ai-time-estimator/releases">Releases</a>
</p>

---

## Quick start

**1. In Claude Code, run:**

```text
/plugin install ai-time-estimator --marketplace noambrand/ai-time-estimator
```

**2. Start a new Claude Code session and ask:**

```text
how long will it take to add CSV export to the report page?
```

That's it. The first session measures your rates in the background (about a minute), and every later answer is scored and improves on its own. You never run a script yourself. Needs Python 3.9+ on your PATH; nothing to `pip install`.

<details>
<summary>Older Claude Code, or prefer another way?</summary>

- **Claude Code before 2.1.275:** run `/plugin marketplace add noambrand/ai-time-estimator`, then `/plugin install ai-time-estimator@ai-time-estimator`.
- **Let Claude do it:** paste *"Install https://github.com/noambrand/ai-time-estimator for me: clone it and run its install.py"* into Claude Code.
- **Terminal, one line:** `git clone https://github.com/noambrand/ai-time-estimator.git && python3 ai-time-estimator/install.py` (on Windows `python`). No git? Download the [ZIP](https://github.com/noambrand/ai-time-estimator/archive/refs/heads/main.zip) and run `python install.py` in its folder.

Pick one way. The plugin and `install.py` do the same job, so having both would score every task twice.

</details>

## Why

| Claude on its own | With AI Time Estimator |
|---|---|
| "This will take 2–3 days" for work it does in 20 minutes | "About 7 min, likely 4–16 min", in its own working time |
| A week-long plan that is finished by lunch | Plans in real AI minutes, measured on your machine |
| A guess from human experience | Claude counts the work; scripts time it with your measured rates |
| Never learns how wrong it was | Scores every estimate and corrects its ranges after 10 tasks |

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

You can also type `/estimate-here <task>` to estimate inside the current session; Claude then waits for your go.

## What installing changes

**As a plugin**, nothing in your own settings: Claude Code loads the agent, the `/estimate-here` command and the two hooks (scoring after each turn, a sync at session start) from the plugin. Your rates and history live in `~/.claude/ai-time-estimator/`.

**With `install.py`**, it:

- copies the `time-estimator` agent to `~/.claude/agents/`, the `/estimate-here` command to `~/.claude/commands/`, and the scripts to `~/.claude/ai-time-estimator/`;
- adds one async **Stop hook** to `~/.claude/settings.json` (your previous file is saved as `settings.json.bak`);
- adds a short, marked "Time estimates" section to `~/.claude/CLAUDE.md`, so Claude uses the estimator whenever you ask how long something will take;
- measures your rates from your existing Claude Code history (about a minute). The rates refresh themselves every 14 days.

Options: `--no-hook` (no automatic scoring), `--no-rule` (leave `~/.claude/CLAUDE.md` alone). Running it again updates in place.

## How accurate is it?

Check it on your own history:

```bash
python ~/.claude/ai-time-estimator/backtest.py
```

The backtest replays your past requests with their real tool calls. It measures rates on the older 80% of your requests and tests on the newest 20%, then reports how often the real time landed inside P10–P90.

On the author's machine (72 held-out requests):

| | inside P10–P90 | median actual ÷ P50 | median P50 error |
|---|---|---|---|
| per-call model only | 39% | 0.88 | 67% |
| with the measured speed spread | **71%** (target 80%) | 1.06 | 67% |

A single request varies a lot, even when its tool calls are known exactly, so trust the range more than the middle number. The backtest checks the time model only; how well the agent counts the work is what the automatic scoring and the self-correction track.

## Privacy

Everything stays on your machine. It makes no network calls and sends nothing anywhere.

What it runs, and when:

- **At session start** (plugin only), a hook runs `install.py --plugin-sync` with Python. It copies the scripts and the agent text to `~/.claude/ai-time-estimator/` and, if your rates were never measured, measures them from your transcripts (about a minute). It never edits your settings or `CLAUDE.md`.
- **After each Claude turn**, a hook runs `tools/autoscore.py`. It reads that session's transcript, scores the estimates made in it, and appends the scores to `actuals.jsonl`. When your rates are 14 days old, it measures them again.
- **When you ask how long something will take**, the `time-estimator` agent reads your project without changing it, and runs `history.py`, `estimate.py`, `compare.py --report` and, when rates are missing, `calibrate.py` from `~/.claude/ai-time-estimator/`. What it reads stays data: a file or a past request that names a command never makes it run that command.

It reads Claude Code's transcripts in `~/.claude/projects/`, and writes only to `~/.claude/ai-time-estimator/` (its scripts, `rates.json`, `estimates.jsonl`, `actuals.jsonl`, `autoscore.log`). Details in [PRIVACY.md](PRIVACY.md).

## Limitations

- It relies on Claude Code's local transcript format, which is internal and can change. If `calibrate.py` stops recognizing model turns it prints `FORMAT CHECK FAILED`; `tools/cc_log.py` is the only file to adapt.
- History reaches back as far as Claude Code keeps transcripts (`cleanupPeriodDays`, 30 days by default). A fresh install with little history gives wider, rougher ranges at first.
- A task ends when you send your next message. If Claude asks you something halfway, the score stops at that point.
- It measures AI time only: approvals, answers and manual checks are not part of the estimate.

## Uninstall

Plugin: run `/plugin uninstall ai-time-estimator`. Your estimate history stays in `~/.claude/ai-time-estimator/`; delete that folder too if you want it gone.

`install.py`, from the cloned folder:

```bash
python install.py --uninstall           # removes the agent, command, scripts, hook and CLAUDE.md section; keeps your history
python install.py --uninstall --purge   # also deletes ~/.claude/ai-time-estimator
```

Or ask Claude Code: *"uninstall the AI time estimator by running its install.py --uninstall"*.

## Development

```bash
python -m unittest discover -s tests -v       # the tests never touch ~/.claude
python tools/backtest.py --worst 10           # accuracy on your history, with the worst misses
python tools/history.py --everywhere --grep test
```

Changes take effect after `python install.py`. [`.claude/CLAUDE.md`](.claude/CLAUDE.md) explains the architecture, for contributors and for Claude Code. Issues and pull requests are welcome.

## Credits

Transcript parsing follows the approach of [dioptx/cctime](https://github.com/dioptx/cctime) (MIT): streamed chunks merged by `requestId`, tool time measured from `tool_use` to `tool_result`.

## License

[MIT](LICENSE) © 2026 Noam Brand

<p align="center"><sub>If it saved you from a wrong plan, a ⭐ helps other Claude Code users find it.</sub></p>
