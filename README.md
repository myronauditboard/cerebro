# cerebro

Live CLI dashboard for Claude Code activity. Shows every running `claude`
session at a glance and aggregates token usage across all your past
sessions.

## What you get

```
cerebro — claude code activity

┌────────────────────── Tokens ──────────────────────┐
│  Today      62.7M in   ·  383.7K out   ·   350 msgs across  3 sessions │
│  Week      407.9M in   ·    2.3M out   ·  2040 msgs across 12 sessions │
│  Lifetime    1.0B in   ·    3.6M out   ·  5998 msgs across 57 sessions │
└────────────────────────────────────────────────────┘

   PID    AGE     REPO                 BRANCH                       TTY      SESSION
*  21554  4d19h   auditboard-backend   auto-annotate-database-v0    ttys020  4cb31d70…
   5063   4d20h   auditboard-frontend  auto-annotate-database-v0    ttys011  -
   24953  11d16h  auditboard-backend   develop                      ttys013  -
   ...

  ctrl-C to exit
```

`*` marks the session whose process tree contains the cerebro you launched
(works when invoked via `! cerebro` from inside a Claude session).

## Install

```bash
git clone <this-repo> ~/Development/cerebro    # or just `cp -r` if local
cd ~/Development/cerebro
./install.sh
exec zsh    # pick up the new PATH line
cerebro
```

`install.sh` appends one `export PATH=…` line to `~/.zshrc` (idempotent).
Requires `python3`, `lsof`, `git`, `ps` — all preinstalled on macOS.

To remove: `./uninstall.sh`.

## Usage

```bash
cerebro                         # live dashboard (default), refreshes every 2s
cerebro -n 5                    # 5s refresh
cerebro --once                  # render one frame and exit
cerebro --no-branch             # skip per-session git branch lookup
cerebro sessions                # one-shot session table only
cerebro tokens                  # one-shot token summary only
cerebro --once --json | jq      # machine-readable
cerebro help                    # usage
```

## How it works

- **Sessions**: filters `ps -o pid,etime,tty,command -u $USER` for processes
  whose command starts with `claude` (excludes the unblocked MCP child and
  the VS Code extension binary). For each PID, `lsof` gives the cwd and
  `git -C <cwd> branch --show-current` gives the branch.
- **Tokens**: walks `~/.claude/projects/**/*.jsonl`, sums `input_tokens +
  cache_creation_input_tokens + cache_read_input_tokens` (combined as "in")
  and `output_tokens` ("out") on assistant messages, bucketed by local
  date. Re-scans are incremental — files whose mtime hasn't changed are
  skipped, and changed files are read only from the previously-recorded
  byte offset onward.

## Out of scope (for now)

- Cost / dollar conversion (different models, different rates, needs care)
- Cache-token breakdown in the summary panel
- Interactive selection / attach (would be a future `cerebro attach <pid>`)
- Cross-machine view, daemon mode

## Layout

```
cerebro/
├── bin/cerebro            bash wrapper, exec's `python3 -m cerebro`
├── cerebro/
│   ├── __main__.py        argparse dispatch
│   ├── sessions.py        ps/lsof/git enumeration
│   ├── tokens.py          incremental jsonl aggregator
│   └── render.py          ANSI-driven live render loop
├── install.sh
└── uninstall.sh
```
