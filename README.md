# cerebro

Live CLI dashboard for Claude Code activity. Shows every running `claude`
session at a glance, aggregates token usage across all your past sessions,
and surfaces what each session is doing right now.

## What you get

Two tabs in a refreshing TUI: **Overview** (token usage + a session table)
and **Agents** (per-session detail with current activity). Click a tab in
the header or press `1` / `2`.

```
cerebro — claude code activity
   Overview     Agents

┌─────────────────────────────────── Tokens ────────────────────────────────────┐
│  Period          Raw      Cache        Out   Billable   Messages   Sessions   │
│  Today           322     42.46M     98.07K     98.39K         77          2   │
│  Week         14.64K    664.94M      3.05M      3.06M       2605          5   │
│  Lifetime     30.77K      1.27B      4.39M      4.43M       6662         63   │
└───────────────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────── Live ──────────────────────────────────────┐
│  Period            Billable    Messages    Sessions   Detail                  │
│  Today               98.39K          77           2                           │
│  Most active          1.48M           —           —   Apr 24, streak 2d       │
│  Favorite model       3.41M           —           —   claude-opus-4-7         │
└───────────────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────── /usage ────────────────────────────────────┐
│  Period              Tokens    Messages    Sessions   Detail                  │
│  Today              367.32K         573           2                           │
│  Total                3.06M        9244          63   since Mar 26, 2026      │
│  Most active        551.60K           —           —   Apr 16, streak 1d       │
│  Favorite model       1.42M           —           —   claude-opus-4-7         │
└───────────────────────────────────────────────────────────────────────────────┘

   PID    AGE     SESSION                   REPO                 TTY
*  21554  4d20h   ml-auto-annotate-tachyon… auditboard-backend   ttys020
   5063   4d21h   -                         auditboard-frontend  ttys011
   ...

  click a tab or [1]/[2]  ·  ↑↓ / wheel scroll  ·  [s] select  ·  [r] refresh  ·  [q] quit
```

Rows are sorted **most-recently-used first** using the `updatedAt`
field of `~/.claude/sessions/<pid>.json`. The session in the cwd you
just typed in lands at the top; sessions whose metadata has no
`updatedAt` yet fall to the bottom in PID order. `*` marks the session
whose process tree contains the cerebro you launched (works when
invoked via `! cerebro` from inside a Claude session). Branch is no
longer in this table — it's surfaced in the Agents-tab detail pane,
where it's most useful.

## Install

```bash
git clone https://github.com/myronauditboard/cerebro.git ~/Development/cerebro
cd ~/Development/cerebro
./install.sh
exec zsh                  # pick up the new PATH line
cerebro
```

`install.sh` appends one `export PATH=…` line to `~/.zshrc` (idempotent).
Requires `python3`, `lsof`, `git`, `ps` — all preinstalled on macOS.

To remove: `./uninstall.sh`.

## Usage

```bash
cerebro                         # live dashboard (Overview tab), refreshes every 2s
cerebro --tab agents            # live dashboard, Agents tab
cerebro -n 5                    # 5s refresh interval
cerebro --once                  # render one frame and exit
cerebro --no-branch             # skip per-session git branch lookup (faster)
cerebro sessions                # one-shot session table
cerebro tokens                  # one-shot token summary (all 3 panels)
cerebro agents                  # one-shot per-session detail
cerebro --once --json | jq      # machine-readable
cerebro help                    # usage
```

## Live navigation

Always available:

| Action | Keys | Mouse |
|---|---|---|
| Switch tab | `1` / `2` / `tab` | click the tab header |
| Scroll half page | `PgUp` `PgDn` / `ctrl-U` `ctrl-D` | — |
| Top / bottom | `Home` `End` / `g` `G` | — |
| Toggle select mode | `s` | — |
| Force refresh | `r` | — |
| Quit | `q` / `ctrl-C` | — |

On the Overview tab `↑`/`↓` (or `j`/`k`) scroll the body line by line.
On the Agents tab those keys are repurposed for the per-agent layout:

| Action | Keys | Mouse |
|---|---|---|
| Cycle agent sub-tab | `h` `l` / `←` `→` | click an agent chip |
| Move nav (agent ↔ sub-agents) | `↑` `↓` / `j` `k` | — |
| Scroll detail pane | `PgUp` `PgDn` / `ctrl-U` `ctrl-D` | wheel up/down |

Title + tab header + agent sub-tab row stay pinned at the top; the
footer stays pinned at the bottom. A `(↑N ↓M hidden)` indicator appears
in the footer when content is scrolled offscreen. Each tab keeps its own
scroll position.

The tab header mirrors Claude Code's `/usage` style — active tab gets a
filled background block, inactive tabs are dim. Mouse support uses SGR
mouse mode (CSI `?1000h` + `?1006h`), which most modern terminals
(iTerm2, recent macOS Terminal.app, Kitty, Alacritty, Ghostty) handle
natively. If yours doesn't, keyboard nav still works.

### Select mode (copy / paste)

Live mouse tracking and the 2 s refresh both interfere with the
terminal's normal click-drag selection. Press `s` to enter **select
mode** — mouse tracking turns off and the refresh pauses, so the screen
freezes and you can drag-select / copy with your terminal's normal
keystrokes (Cmd-C on macOS). The footer turns yellow (`⚠ select mode`)
to make the state obvious. Press `s` again to resume live updates.

## Overview tab — the three panels

### Tokens

Live, derived from `~/.claude/projects/**/*.jsonl`. Re-reads on every
refresh, incrementally — files whose mtime hasn't changed are skipped,
changed files are read from the previously-recorded byte offset onward.

| Column | Meaning |
|---|---|
| `Raw` | `input_tokens` — uncached input. The "fresh prompt" tokens, no cache reads, no cache writes. |
| `Cache` | `cache_creation_input_tokens + cache_read_input_tokens` — both halves of the prompt cache combined. Usually dwarfs `Raw`. |
| `Out` | `output_tokens` — what the model generated. |
| `Billable` | `Raw + Out` — same definition as `/usage`'s "Total tokens". |
| `Messages` | Count of assistant records in that window. |
| `Sessions` | Distinct JSONL files (≈ sessions) that contributed to that window. |

Three rows: `Today` (today, local timezone), `Week` (last 7 days inclusive of today), `Lifetime` (every assistant message on disk).

`Billable` is the column to compare against the `/usage` Stats screen
inside Claude Code. Values format with exactly two decimals (`3.06M`,
`12.40K`) for consistent column widths.

### Live

Aggregate facts derived from the same jsonl scan as Tokens, recomputed
on every refresh. Today row, most-active day, favorite model. All
values are billable tokens for direct comparison with the Tokens panel
above. The trailing `Detail` column carries the row's identifying note
(which day, which model).

### `/usage`

Reads `~/.claude/stats-cache.json` directly — the same source the
`/usage` Stats screen inside Claude Code reads from. Numbers match
`/usage` exactly (within the cache's recompute cadence).

**Live vs. /usage**: same shape, different sources. `Live` is cerebro's
own jsonl-derived numbers, recomputed every refresh. `/usage` is
Claude Code's pre-computed cache, recomputed lazily. Disagreement
between the two means the stats cache is stale relative to the
underlying jsonl files, not that either is wrong — Claude Code
recomputes the cache lazily, while cerebro re-reads jsonl every
refresh. Having both visible side by side makes the drift legible.

## Agents tab

Per-session detail view. For every running `claude` process, cerebro
reads `~/.claude/sessions/<pid>.json` for the auto-named session label,
then tails the matching jsonl in `~/.claude/projects/<encoded-cwd>/` to
extract what each session is doing, what prompts have been sent, and
which sub-agents have been spawned.

The layout has three levels of selection:

1. **Top tabs** (`Overview` / `Agents`) — same as before.
2. **Agent sub-tabs** — one chip per running session, labeled with the
   session name (PID fallback). Active chip gets the violet block.
3. **Per-agent left nav** — the agent itself, then each of its sub-agents.

The right pane shows the detail for whichever nav item is selected.

```
   surface-subagents-cer…   move-fieldwork-but…   PID 23593   PID 24953
   ────────────────────────

   ▸ ⌂ surface-subagents-cer…   │ * PID 59143  [working]  last activity 2s ago
     Explore  cerebro surfacing │   surface-subagents-cerebro
     Explore  fetch jsonl tail  │   cerebro  (main)  ·  tty ttys020  ·  age 5d
                                │   session 63e1a1d3…
                                │   now: tool_use: Edit  ·  cerebro/render.py
                                │
                                │   Recent interactions
                                │   You · 14:35 · queued
                                │     also add an MRU sort to the table
                                │   AI · waiting in queue
                                │
                                │   You · 14:32
                                │     Can you make the text in cerebro copy-able?
                                │   AI · 14:33 · 3 tools
                                │     I'll add a select-mode toggle that pauses…
```

When the terminal is narrower than 70 cols, the split view collapses
back to a flat per-agent block list (each block: header + repo + branch
+ session id + last event + sub-agents inline).

### Detail pane

For the **agent itself** (nav row 1):

- Header line: `PID … [status] last activity Ns ago`
- Session name, repo / branch / tty / age, session id, last event excerpt.
- **Recent interactions** — up to the last 10 prompt/response pairs
  newest-first. Each entry shows `You · HH:MM` + the prompt, then
  `AI · HH:MM · N tools` + the assistant's final reply (`in progress` if
  the AI is still working). Cerebro reads the jsonl backward in 256 KB
  chunks until 10 user-text records are accumulated (or 16 MB cap), so
  long sessions display a real history rather than just the tail.
- **Queued prompts** are surfaced too: if you type a second prompt while
  Claude is still working on the first, it shows at the top of the
  feed tagged `queued` with `AI · waiting in queue` — derived from
  Claude Code's `queue-operation` records.

For a **sub-agent** (any subsequent nav row):

- Header line: `Agent[<type>] [status] last activity Ns ago` plus its
  description.
- **Prompt (from parent agent)** — the full prompt the parent invoked
  it with (read from `agent-<id>.meta.json` next to the sub-agent jsonl).
- **Recent interactions** — sub-agents are one-shot, so this is a single
  pair: parent prompt → assistant final text + tool count.

### Sub-agents

Every Agent-tool invocation produces a dedicated transcript at
`~/.claude/projects/<encoded-cwd>/<sessionId>/subagents/agent-<id>.jsonl`
plus a tiny `agent-<id>.meta.json` with `{agentType, description}`. For
each running parent, cerebro lists those files and includes:

- All currently-active sub-agents (jsonl mtime within 60 s), plus
- The 10 most-recent completed sub-agents (older mtimes).

Each sub-agent gets its own row in the left nav and a full detail view
on selection.

### Status colors

| Status | Color | Meaning |
|---|---|---|
| `working` | green | jsonl write < 10s ago |
| `waiting` | yellow | last record is `tool_use`, idle 10–60s (likely on a tool call) |
| `active` | cyan | activity within last 2 minutes |
| `idle` / `stale` / `unknown` | gray | quieter or no jsonl found |

### Looking up the active jsonl

Claude Code can rotate a session's id mid-process (e.g. after `/clear`
or compaction) and start writing to a new `<newId>.jsonl` in the same
project directory, while `~/.claude/sessions/<pid>.json` keeps pointing
at the old `sessionId`. To pick the right transcript:

1. Use the metadata file's `updatedAt` timestamp — it gets bumped on
   every session pulse — and pick the jsonl in the cwd's project dir
   whose mtime is closest to it. This handles rotation cleanly and also
   disambiguates when multiple `claude` processes share a cwd (each
   PID has its own `updatedAt`).
2. If `updatedAt` isn't available, fall back to a sessionId match on
   `--resume <id>` or `metadata.sessionId`.
3. Then the most-recently-modified jsonl in the project dir.
4. Finally, a cross-project sessionId scan.

## How it works

| Source | What for |
|---|---|
| `ps -o pid,etime,tty,command -u $USER` | enumerate running `claude` PIDs |
| `lsof -a -p <pid> -d cwd -Fn` | per-PID working directory |
| `git -C <cwd> branch --show-current` | per-PID branch |
| `~/.claude/projects/**/*.jsonl` | token aggregation, last-activity excerpt |
| `~/.claude/sessions/<pid>.json` | auto-named session label, kind, sessionId |
| `~/.claude/stats-cache.json` | `/usage` panel (verbatim) |

The detection filter excludes the `unblocked` MCP child processes and
the VS Code extension's `native-binary/claude` so only real CLI sessions
show up. Token aggregation is incremental and keyed on file mtime, so
steady-state refreshes are essentially free.

## Out of scope (for now)

- Cost / dollar conversion (different models, different rates, needs care)
- Cache-token breakdown in the Live / `/usage` panels
- Interactive selection / attach (e.g. `cerebro attach <pid>`)
- Cross-machine view, daemon mode, history graphs

## Layout

```
cerebro/
├── bin/cerebro             bash wrapper, exec's `python3 -m cerebro`
├── cerebro/
│   ├── __main__.py         argparse dispatch
│   ├── sessions.py         ps/lsof/git enumeration of running PIDs
│   ├── agents.py           per-session detail (Agents tab data source)
│   ├── tokens.py           incremental jsonl aggregator (Tokens + Live)
│   ├── stats_cache.py      ~/.claude/stats-cache.json reader (/usage panel)
│   └── render.py           ANSI render loop, tab nav, mouse + scroll input
├── install.sh
└── uninstall.sh
```
