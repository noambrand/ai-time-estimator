# Privacy

AI Time Estimator runs entirely on your own computer. It collects nothing, sends nothing, and makes no network calls.

## What it reads

- Claude Code's local session transcripts in `~/.claude/projects/` (to measure how long model turns, tools and commands take on your machine, and to score past estimates).
- With `install.py` only: your `~/.claude/settings.json` and `~/.claude/CLAUDE.md`, to add or remove its own Stop hook and its marked "Time estimates" section.

## What it writes

- `~/.claude/ai-time-estimator/`: the scripts, your measured rates (`rates.json`), your estimates (`estimates.jsonl`), their scores (`actuals.jsonl`), the run times of long jobs you record (`jobs.jsonl`) and a log (`autoscore.log`). These files contain timings, tool-call counts, task summaries you asked about, and the first line of past requests (shown by `history.py`). They never leave your machine.
- With `install.py` only: the agent and command files in `~/.claude/agents/` and `~/.claude/commands/`, the hook entry in `~/.claude/settings.json` (a backup is kept as `settings.json.bak`), and the marked section in `~/.claude/CLAUDE.md`.

## Removing it

- Plugin: `/plugin uninstall ai-time-estimator`, then delete `~/.claude/ai-time-estimator/` if you want your history gone too.
- `install.py`: `python install.py --uninstall --purge` removes everything it added, including the data folder.

Questions: [open an issue](https://github.com/noambrand/ai-time-estimator/issues).
