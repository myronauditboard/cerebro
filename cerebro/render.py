"""Live render loop + table/summary formatting."""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import termios
import time
import tty
from typing import Sequence

from . import term_ctl
from .agents import AgentDetail, Interaction, SubAgentDetail, list_agents
from .sessions import Session, list_sessions
from .stats_cache import StatsCacheSummary
from .tokens import ActivityStats, Bucket, TokenAggregator, TokenSummary


CSI = "\033["
HOME = f"{CSI}H"
CLEAR_TO_END = f"{CSI}J"
HIDE_CURSOR = f"{CSI}?25l"
SHOW_CURSOR = f"{CSI}?25h"
DIM = f"{CSI}2m"
BOLD = f"{CSI}1m"
RESET = f"{CSI}0m"
# Mouse tracking — enable basic button events with SGR-format coordinates.
MOUSE_ON = f"{CSI}?1000h{CSI}?1006h"
MOUSE_OFF = f"{CSI}?1000l{CSI}?1006l"


# ── Theme palettes ─────────────────────────────────────────────────────────
# Every theme-sensitive ANSI string lives in here so adding a new theme later
# means adding one dict entry — no hunting through render code. BOLD/DIM/RESET
# stay theme-neutral (SGR attributes, not color codes).
#
# Right now there's only "dark" — the values are exactly what cerebro emitted
# before this refactor, so the rendered output is byte-identical. Light theme
# and a runtime toggle are intentionally deferred.
_PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "status_working":  f"{CSI}32m",       # green
        "status_waiting":  f"{CSI}33m",       # yellow
        "status_active":   f"{CSI}36m",       # cyan
        "status_idle":     f"{CSI}90m",       # bright black
        "status_stale":    f"{CSI}90m",
        "status_finished": f"{CSI}35m",       # magenta — "process gone"
        "status_unknown":  f"{CSI}90m",
        "tab_active":      f"{CSI}48;5;60m{CSI}1;97m",   # violet bg + white bold fg
        "subtab_active":   f"{CSI}48;5;60m{CSI}1;97m",   # same — agent tabs match top tabs
        "warning":         f"{CSI}33m",       # yellow (select-mode ribbon)
        "queued":          f"{CSI}33m",       # yellow (queued-prompt tag)
    },
}

_THEME: str = "dark"


def set_theme(name: str) -> None:
    """Switch the active palette. No-op on unknown names."""
    global _THEME
    if name in _PALETTES:
        _THEME = name


def get_theme() -> str:
    return _THEME


def _p(key: str) -> str:
    """Look up an ANSI string in the active palette. `""` for missing keys."""
    return _PALETTES[_THEME].get(key, "")

TABS = ["overview", "agents"]
TAB_ROW = 2          # 1-indexed terminal row where the tab header lives
TAB_PREFIX = "  "    # 2-char left margin before the first tab
TAB_SEPARATOR = "  " # spaces between adjacent tabs

# Capitalization for display (the lowercase form is the canonical id).
TAB_DISPLAY = {"overview": "Overview", "agents": "Agents"}


def fmt_count(n: int) -> str:
    """Compact human-readable count with exactly 2 decimals for consistency.

    1_180_000 → "1.18M" · 12_400_000 → "12.40M" · 12_000_000 → "12.00M" · 999_990 → "999.99K"
    """
    for unit, divisor in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(n) >= divisor:
            return f"{n / divisor:.2f}{unit}"
    return str(int(n))


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    if n <= 1:
        return s[:n]
    return s[: n - 1] + "…"


def _panel(title: str, lines: list[str], inner: int) -> list[str]:
    pad = (inner - len(title) - 2) // 2
    top = "┌" + "─" * pad + f" {title} " + "─" * (inner - pad - len(title) - 2) + "┐"
    bot = "└" + "─" * inner + "┘"
    out = [top]
    for ln in lines:
        # Strip ANSI for length calc, but we don't generate ANSI inside body so len() is fine
        if len(ln) > inner:
            ln = ln[:inner]
        out.append("│" + ln.ljust(inner) + "│")
    out.append(bot)
    return out


def render_summary(summary: TokenSummary, width: int) -> list[str]:
    inner = max(72, width - 2)
    label_w = 10
    num_w = 9

    def row(label: str, raw: str, cache: str, out: str, bill: str, msg: str, sess: str) -> str:
        return (
            f"  {label:<{label_w}}"
            f"{raw:>{num_w}}  "
            f"{cache:>{num_w}}  "
            f"{out:>{num_w}}  "
            f"{bill:>{num_w}}  "
            f"{msg:>{num_w}}  "
            f"{sess:>{num_w}}"
        )

    def data_row(b: Bucket) -> str:
        return row(
            b.label.capitalize(),
            fmt_count(b.raw_input_tokens),
            fmt_count(b.cache_tokens),
            fmt_count(b.output_tokens),
            fmt_count(b.billable_tokens),
            str(b.msgs),
            str(b.sessions),
        )

    rows = [
        row("Period", "Raw", "Cache", "Out", "Billable", "Messages", "Sessions"),
        data_row(summary.today),
        data_row(summary.week),
        data_row(summary.lifetime),
    ]
    return _panel("Tokens", rows, inner)


# Column widths for the table-style Stats and /usage panels.
_TBL_LABEL = 16
_TBL_NUM = 10


def _table_row(label: str, tokens: str, msgs: str, sess: str, detail: str = "") -> str:
    base = (
        f"  {label:<{_TBL_LABEL}}"
        f"{tokens:>{_TBL_NUM}}  "
        f"{msgs:>{_TBL_NUM}}  "
        f"{sess:>{_TBL_NUM}}"
    )
    return f"{base}   {detail}" if detail else base


def render_stats_cache(sc: StatsCacheSummary, width: int) -> list[str]:
    inner = max(60, width - 2)
    if not sc.available:
        return _panel(
            "/usage",
            ["  (~/.claude/stats-cache.json not found)"],
            inner,
        )
    most_day = sc.most_active_day.strftime("%b %-d") if sc.most_active_day else "—"
    first_d = sc.first_session_date.strftime("%b %-d, %Y") if sc.first_session_date else "—"
    rows = [
        _table_row("Period", "Tokens", "Messages", "Sessions", "Detail"),
        _table_row(
            "Today",
            fmt_count(sc.today_tokens),
            str(sc.today_messages),
            str(sc.today_sessions),
        ),
        _table_row(
            "Total",
            fmt_count(sc.total_tokens),
            str(sc.total_messages),
            str(sc.total_sessions),
            f"since {first_d}",
        ),
        _table_row(
            "Most active",
            fmt_count(sc.most_active_day_tokens),
            "—",
            "—",
            f"{most_day}, streak {sc.current_streak}d",
        ),
        _table_row(
            "Favorite model",
            fmt_count(sc.favorite_model_tokens),
            "—",
            "—",
            sc.favorite_model,
        ),
    ]
    return _panel("/usage", rows, inner)


def render_activity(summary: TokenSummary, width: int) -> list[str]:
    inner = max(60, width - 2)
    act = summary.activity
    today = summary.today
    fav_label = act.favorite_model or "—"
    fav_n = fmt_count(act.favorite_model_billable) if act.favorite_model_billable else "0"
    most_day = act.most_active_day.strftime("%b %-d") if act.most_active_day else "—"
    most_n = fmt_count(act.most_active_day_billable) if act.most_active_day_billable else "0"
    streak = act.current_streak
    rows = [
        _table_row("Period", "Billable", "Messages", "Sessions", "Detail"),
        _table_row(
            "Today",
            fmt_count(today.billable_tokens),
            str(today.msgs),
            str(today.sessions),
        ),
        _table_row(
            "Most active",
            most_n,
            "—",
            "—",
            f"{most_day}, streak {streak}d",
        ),
        _table_row(
            "Favorite model",
            fav_n,
            "—",
            "—",
            fav_label,
        ),
    ]
    return _panel("Live", rows, inner)


def render_table(sessions: Sequence[Session], width: int) -> list[str]:
    if not sessions:
        return [DIM + "  (no running claude sessions)" + RESET]

    repo_w = max(4, min(28, max((len(s.repo) for s in sessions), default=4)))
    tty_w = max(3, max((len(s.tty) for s in sessions), default=3))
    sess_w = max(
        7,
        min(24, max((len(s.name or s.resume_id or "") for s in sessions), default=7)),
    )

    header = (
        "   "
        + "PID".ljust(7)
        + "AGE".ljust(8)
        + "SESSION".ljust(sess_w + 2)
        + "REPO".ljust(repo_w + 2)
        + "TTY".ljust(tty_w + 2)
    )
    lines = [BOLD + header + RESET]

    for s in sessions:
        marker = "* " if s.is_current else "  "
        sess_label = s.name or s.resume_id or "-"
        row = (
            marker
            + " "
            + str(s.pid).ljust(7)
            + s.age.ljust(8)
            + _truncate(sess_label, sess_w).ljust(sess_w + 2)
            + _truncate(s.repo, repo_w).ljust(repo_w + 2)
            + s.tty.ljust(tty_w + 2)
        )
        if s.is_current:
            row = BOLD + row + RESET
        lines.append(row)
    return lines


def _status_color(status: str) -> str:
    return _p(f"status_{status}")


def _fmt_age(secs: int) -> str:
    if secs < 0:
        return "—"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _trim(text: str, max_len: int) -> str:
    if max_len <= 1:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _render_sub_agent(sa: SubAgentDetail, indent: str, width: int) -> list[str]:
    """Render one sub-agent as 3 lines: header, prompt, current activity."""
    color = _status_color(sa.status)
    head_indent = indent + "└─ "
    cont_indent = indent + "   "
    desc = sa.description or ""
    # Header: type, description, status, age
    desc_budget = max(20, width - len(head_indent) - len(sa.agent_type) - 30)
    head = (
        f"{head_indent}{BOLD}Agent[{sa.agent_type}]{RESET}"
        + (f"  {DIM}·{RESET}  {_trim(desc, desc_budget)}" if desc else "")
        + f"  {color}[{sa.status}]{RESET}  "
        f"{DIM}{_fmt_age(sa.last_activity_secs)}{RESET}"
    )
    lines = [head]
    if sa.prompt_excerpt:
        budget = max(20, width - len(cont_indent) - len("prompt: "))
        body = _trim(sa.prompt_excerpt, budget)
        line = f"{cont_indent}{DIM}prompt: {body}{RESET}" if not sa.is_active \
            else f"{cont_indent}prompt: {DIM}{body}{RESET}"
        lines.append(line)
    if sa.last_event:
        line = f"{cont_indent}now: {BOLD}{sa.last_event}{RESET}"
        if sa.last_event_detail:
            detail = sa.last_event_detail
            max_detail = max(20, width - len(cont_indent) - len("now: ") - len(sa.last_event) - 5)
            line += f"  {DIM}·{RESET}  {_trim(detail, max_detail)}"
        lines.append(line)
    return lines


def render_agents(agents: Sequence[AgentDetail], width: int) -> list[str]:
    if not agents:
        return [DIM + "  (no running claude sessions)" + RESET]
    out: list[str] = []
    for a in agents:
        marker = "* " if a.is_current else "  "
        color = _status_color(a.status)
        head = (
            f"{marker}{BOLD}PID {a.pid}{RESET}  "
            f"{color}[{a.status}]{RESET}  "
            f"last activity {_fmt_age(a.last_activity_secs)} ago"
        )
        out.append(head)
        sub_indent = "    "
        if a.name and a.name != "—":
            out.append(f"{sub_indent}{BOLD}{a.name}{RESET}")
        repo_branch = a.repo or "—"
        if a.branch:
            repo_branch += f"  ({a.branch})"
        out.append(f"{sub_indent}{DIM}{repo_branch}  ·  tty {a.tty}  ·  age {a.age}{RESET}")
        if a.session_id:
            sid = a.session_id[:8] + "…" if len(a.session_id) > 9 else a.session_id
            out.append(f"{sub_indent}{DIM}session {sid}{RESET}")
        if a.last_event:
            line = f"{sub_indent}now: {BOLD}{a.last_event}{RESET}"
            if a.last_event_detail:
                detail = a.last_event_detail
                # Trim to fit
                max_detail = max(20, width - len(sub_indent) - len("now: ") - len(a.last_event) - 5)
                line += f"  {DIM}·{RESET}  {_trim(detail, max_detail)}"
            out.append(line)
        for sa in a.sub_agents:
            out.extend(_render_sub_agent(sa, sub_indent, width))
        out.append("")
    return out


def render_overview(
    summary: TokenSummary,
    sessions: Sequence[Session],
    stats_cache: StatsCacheSummary,
    width: int,
) -> list[str]:
    blocks: list[str] = []
    blocks.extend(render_summary(summary, width))
    blocks.extend(render_activity(summary, width))
    blocks.extend(render_stats_cache(stats_cache, width))
    blocks.append("")
    blocks.extend(render_table(sessions, width))
    return blocks


def tab_layout(names: Sequence[str] = TABS) -> list[tuple[int, int, str]]:
    """Return click hit-test ranges as (col_start, col_end, tab_id) — 1-indexed inclusive.

    The tab header has a fixed deterministic layout (2-char prefix, " Name " per tab,
    2-char separator), so we can compute click targets without measuring the rendered
    output.
    """
    ranges: list[tuple[int, int, str]] = []
    col = len(TAB_PREFIX) + 1  # 1-indexed
    for i, name in enumerate(names):
        display = TAB_DISPLAY.get(name, name.capitalize())
        width = len(display) + 2  # one space padding on each side
        ranges.append((col, col + width - 1, name))
        col += width
        if i < len(names) - 1:
            col += len(TAB_SEPARATOR)
    return ranges


def render_tabs_header(active_id: str, names: Sequence[str] = TABS) -> str:
    """Render the tab header in /usage's visual style — active tab gets a colored block."""
    parts = [TAB_PREFIX]
    for i, name in enumerate(names):
        display = TAB_DISPLAY.get(name, name.capitalize())
        if name == active_id:
            parts.append(f"{_p('tab_active')} {display} {RESET}")
        else:
            parts.append(f"{DIM} {display} {RESET}")
        if i < len(names) - 1:
            parts.append(TAB_SEPARATOR)
    return "".join(parts)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_len(s: str) -> int:
    """Length of `s` ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", s))


def _trim_visible(s: str, max_visible: int) -> str:
    """Trim `s` so its visible (printed-column) length is ≤ max_visible, while
    preserving ANSI escape sequences inline. Used on footers to keep them from
    wrapping onto a second visual row, which would scroll the title off the top.
    """
    if max_visible <= 0:
        return ""
    out: list[str] = []
    visible = 0
    pos = 0
    while pos < len(s):
        m = _ANSI_RE.match(s, pos)
        if m:
            out.append(m.group(0))
            pos = m.end()
            continue
        if visible >= max_visible:
            return "".join(out)
        out.append(s[pos])
        visible += 1
        pos += 1
    return "".join(out)


def _pad_visible(s: str, width: int) -> str:
    """Right-pad `s` so its visible length is `width` (no truncation)."""
    diff = width - _visible_len(s)
    if diff > 0:
        return s + " " * diff
    return s


# Layout for the per-agent sub-tab row.
SUBTAB_PREFIX = "  "
SUBTAB_SEPARATOR = "  "
SUBTAB_LABEL_MAX = 20      # max chars of session name before truncation
SUBTAB_LABEL_FALLBACK = "PID {pid}"
# Where the sub-tab block starts vertically (1-indexed terminal row). It can
# now span multiple rows when agent tabs wrap; absolute click rows are computed
# in render_frame against this base.
SUBTAB_FIRST_ROW = 4
SPLIT_MIN_WIDTH = 70       # below this, fall back to the flat list renderer
SPLIT_NAV_WIDTH = 32       # left pane width
SPLIT_GAP = " │ "          # column separator between nav and detail


def _agent_label(a: AgentDetail) -> str:
    if a.name and a.name != "—":
        label = a.name
    elif a.pid is not None:
        label = SUBTAB_LABEL_FALLBACK.format(pid=a.pid)
    else:
        sid = a.session_id or "?"
        label = sid[:8] + ("…" if len(sid) > 8 else "")
    if len(label) > SUBTAB_LABEL_MAX:
        label = label[: SUBTAB_LABEL_MAX - 1] + "…"
    return label


def _agent_key(a: AgentDetail) -> str:
    """Stable id for tracking the active sub-tab — works for both live (pid)
    and finished (session_id) agents."""
    return a.session_id or (f"pid:{a.pid}" if a.pid is not None else "?")


def render_agent_subtabs(
    agents: Sequence[AgentDetail], active_id: str | None, width: int
) -> tuple[str, list[tuple[int, int, int, str]], int]:
    """Render the per-agent tab block and return
    `(rendered_text, click_ranges, num_rows)`.

    The block self-wraps across as many rows as needed to fit every agent
    tab; render_frame stretches the sticky-top region to match `num_rows`
    so the split view below stays aligned regardless of how many tabs
    there are.

    Each click_range is `(row_within_block, col_start, col_end, agent_id)`
    with row 0-indexed from the block's first row and cols 1-indexed
    inclusive. render_frame translates row offsets to absolute terminal
    rows before handing the ranges to the click handler.
    """
    if not agents:
        return (DIM + "  (no recent claude sessions)" + RESET, [], 1)

    tab_widths = [len(_agent_label(a)) + 2 for a in agents]  # incl. side padding
    sep_w = len(SUBTAB_SEPARATOR)
    prefix_w = len(SUBTAB_PREFIX)
    keys = [_agent_key(a) for a in agents]

    # Pack agent tabs greedily across rows. A tab never gets split — if it
    # wouldn't fit on the current row, start a new one.
    rows: list[list[int]] = [[]]      # each entry is the list of tab indices for that row
    row_used: list[int] = [prefix_w]
    for i, w in enumerate(tab_widths):
        cur = rows[-1]
        used = row_used[-1]
        addition = (sep_w if cur else 0) + w
        if cur and used + addition > width:
            rows.append([i])
            row_used.append(prefix_w + w)
        else:
            cur.append(i)
            row_used[-1] = used + addition

    lines: list[str] = []
    ranges: list[tuple[int, int, int, str]] = []
    for r, row in enumerate(rows):
        parts: list[str] = [SUBTAB_PREFIX]
        col = prefix_w + 1   # 1-indexed
        for slot, i in enumerate(row):
            label = _agent_label(agents[i])
            tab_w = tab_widths[i]
            key = keys[i]
            if key == active_id:
                parts.append(f"{_p('subtab_active')} {label} {RESET}")
            else:
                parts.append(f"{DIM} {label} {RESET}")
            ranges.append((r, col, col + tab_w - 1, key))
            col += tab_w
            if slot < len(row) - 1:
                parts.append(SUBTAB_SEPARATOR)
                col += sep_w
        lines.append("".join(parts))
    return ("\n".join(lines), ranges, len(rows))


def _short_ts(iso: str) -> str:
    """Compact "HH:MM" from an ISO timestamp; empty string if unparseable."""
    if not iso or len(iso) < 16 or iso[10:11] != "T":
        return ""
    return iso[11:16]


def _format_interactions(
    interactions: Sequence[Interaction],
    width: int,
    user_label: str = "You",
) -> list[str]:
    out: list[str] = []
    if not interactions:
        out.append(f"{DIM}(no recent interactions captured){RESET}")
        return out
    body_w = max(20, width - 2)
    # Newest first.
    ordered = list(reversed(interactions))
    for i, intr in enumerate(ordered):
        if i > 0:
            out.append("")
        ts_u = _short_ts(intr.user_ts)
        u_meta_parts = []
        if ts_u:
            u_meta_parts.append(ts_u)
        if intr.queued:
            u_meta_parts.append(f"{_p('queued')}queued{RESET}{DIM}")
        u_meta = f" {DIM}· {' · '.join(u_meta_parts)}{RESET}" if u_meta_parts else ""
        out.append(f"{BOLD}{user_label}{RESET}{u_meta}")
        for line in _wrap_plain(intr.user_text, body_w):
            out.append("  " + line)
        # Queued items have no assistant side yet — Claude hasn't popped them.
        if intr.queued:
            out.append(f"{BOLD}AI{RESET} {DIM}· waiting in queue{RESET}")
            continue
        ts_a = _short_ts(intr.assistant_ts)
        tools_str = (
            f"{intr.tool_calls} tool{'s' if intr.tool_calls != 1 else ''}"
            if intr.tool_calls else ""
        )
        if intr.in_progress:
            head_extra = f"{DIM}· in progress"
            if tools_str:
                head_extra += f" · {tools_str}"
            head_extra += RESET
        else:
            parts = []
            if ts_a:
                parts.append(ts_a)
            if tools_str:
                parts.append(tools_str)
            head_extra = f"{DIM}· {' · '.join(parts)}{RESET}" if parts else ""
        out.append(f"{BOLD}AI{RESET} {head_extra}")
        body = intr.assistant_text or ("…" if intr.in_progress else "")
        if body:
            for line in _wrap_plain(body, body_w):
                out.append("  " + line)
    return out


def _wrap_plain(text: str, width: int) -> list[str]:
    """Cheap line wrap for prompt body. No ANSI to worry about (text is plain)."""
    out: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        line = paragraph
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            if cut <= 0:
                cut = width
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return out


def _render_agent_nav(
    agent: AgentDetail, nav_index: int, width: int
) -> list[str]:
    """Render the left-pane nav: agent first, then sub-agents."""
    out: list[str] = []
    items: list[tuple[str, str]] = []  # (label, status)
    items.append(("⌂ " + _agent_label(agent), agent.status))
    for sa in agent.sub_agents:
        desc = sa.description or sa.agent_id
        label = f"{sa.agent_type}  {desc}"
        items.append((label, sa.status))
    for i, (label, status) in enumerate(items):
        cursor = "▸ " if i == nav_index else "  "
        color = _status_color(status)
        budget = max(8, width - len(cursor) - 2)
        trimmed = _trim(label, budget)
        line = f"{cursor}{color}●{RESET} {trimmed}"
        if i == nav_index:
            line = f"{BOLD}{cursor}{RESET}{color}●{RESET} {BOLD}{trimmed}{RESET}"
        out.append(line)
    return out


def _render_prompt_section(prompt: str, source: str, width: int) -> list[str]:
    out: list[str] = [f"{BOLD}Prompt{RESET} {DIM}(from {source}){RESET}"]
    if prompt:
        for line in _wrap_plain(prompt, width):
            out.append(f"{DIM}{line}{RESET}")
    else:
        out.append(f"{DIM}(no prompt captured){RESET}")
    return out


def _render_agent_detail(
    agent: AgentDetail, nav_index: int, width: int
) -> list[str]:
    """Render the right-pane detail for whichever nav item is selected."""
    out: list[str] = []
    if nav_index == 0:
        # The parent agent itself
        color = _status_color(agent.status)
        marker = "* " if agent.is_current else ""
        if agent.pid is not None:
            head_prefix = f"{marker}{BOLD}PID {agent.pid}{RESET}  "
        else:
            sid_short = (
                agent.session_id[:8] + "…"
                if agent.session_id and len(agent.session_id) > 9
                else (agent.session_id or "?")
            )
            head_prefix = f"{BOLD}session {sid_short}{RESET}  "
        out.append(
            f"{head_prefix}"
            f"{color}[{agent.status}]{RESET}  "
            f"last activity {_fmt_age(agent.last_activity_secs)} ago"
        )
        if agent.name and agent.name != "—":
            out.append(f"{BOLD}{agent.name}{RESET}")
        repo_branch = agent.repo or "—"
        if agent.branch:
            repo_branch += f"  ({agent.branch})"
        # Live agents have tty + age; finished ones don't (process is gone).
        if agent.is_live and agent.tty:
            out.append(
                f"{DIM}{repo_branch}  ·  tty {agent.tty}  ·  age {agent.age}{RESET}"
            )
        else:
            out.append(f"{DIM}{repo_branch}{RESET}")
        if agent.session_id and agent.pid is not None:
            sid = agent.session_id[:8] + "…" if len(agent.session_id) > 9 else agent.session_id
            out.append(f"{DIM}session {sid}{RESET}")
        if agent.last_event:
            line = f"now: {BOLD}{agent.last_event}{RESET}"
            if agent.last_event_detail:
                budget = max(20, width - _visible_len(line) - 5)
                line += f"  {DIM}·{RESET}  {_trim(agent.last_event_detail, budget)}"
            out.append(line)
        out.append("")
        out.append(f"{BOLD}Recent interactions{RESET}")
        out.extend(_format_interactions(agent.recent_interactions, width, user_label="You"))
        return out

    sa_idx = nav_index - 1
    if sa_idx < 0 or sa_idx >= len(agent.sub_agents):
        out.append(f"{DIM}(invalid selection){RESET}")
        return out
    sa = agent.sub_agents[sa_idx]
    color = _status_color(sa.status)
    out.append(
        f"{BOLD}Agent[{sa.agent_type}]{RESET}  "
        f"{color}[{sa.status}]{RESET}  "
        f"{DIM}last activity {_fmt_age(sa.last_activity_secs)} ago{RESET}"
    )
    if sa.description:
        out.append(f"{DIM}{_trim(sa.description, max(20, width - 2))}{RESET}")
    if sa.last_event:
        line = f"now: {BOLD}{sa.last_event}{RESET}"
        if sa.last_event_detail:
            budget = max(20, width - _visible_len(line) - 5)
            line += f"  {DIM}·{RESET}  {_trim(sa.last_event_detail, budget)}"
        out.append(line)
    out.append("")
    out.extend(_render_prompt_section(sa.prompt, "parent agent", width))
    out.append("")
    out.append(f"{BOLD}Recent interactions{RESET}")
    out.extend(_format_interactions(sa.recent_interactions, width, user_label="Parent"))
    return out


def render_agent_split(
    agent: AgentDetail, nav_index: int, width: int
) -> list[str]:
    """Stitch left nav and right detail into a single list of lines."""
    nav_w = SPLIT_NAV_WIDTH
    gap_w = len(SPLIT_GAP)
    detail_w = max(20, width - nav_w - gap_w)
    nav = _render_agent_nav(agent, nav_index, nav_w)
    detail = _render_agent_detail(agent, nav_index, detail_w)
    height = max(len(nav), len(detail))
    out: list[str] = []
    for i in range(height):
        left = nav[i] if i < len(nav) else ""
        right = detail[i] if i < len(detail) else ""
        out.append(_pad_visible(left, nav_w) + f"{DIM}{SPLIT_GAP}{RESET}" + right)
    return out


def render_frame(
    *,
    tab: str,
    summary: TokenSummary,
    sessions: Sequence[Session],
    stats_cache: StatsCacheSummary,
    agents: Sequence[AgentDetail],
    active_id: str | None,
    nav_index: int,
    width: int,
    mouse_enabled: bool = True,
    kill_pending_pid: int | None = None,
    status_msg: str | None = None,
) -> tuple[str, int, list[tuple[int, int, int, str]]]:
    """Render a complete frame.

    Returns (frame_text, sticky_top_lines, subtab_click_ranges). The sub-tab
    block self-wraps across as many rows as it needs; `sticky_top_lines`
    grows to match so the split-view body below stays correctly pinned.
    Click ranges include the absolute terminal row of each agent tab so
    the live loop's hit-testing keeps working across the wrapped rows.

    Transient footer states (in order of priority): select-mode ribbon,
    kill-confirm ribbon, status-flash message, normal footer.
    """
    out: list[str] = []
    out.append(BOLD + "cerebro" + RESET + DIM + " — claude code activity" + RESET)
    out.append(render_tabs_header(tab))
    out.append("")
    subtab_ranges: list[tuple[int, int, int, str]] = []
    sticky_top = _STICKY_TOP
    if tab == "agents":
        subtab_block, rel_ranges, subtab_rows = render_agent_subtabs(
            agents, active_id, width
        )
        out.append(subtab_block)
        out.append("")
        sticky_top = _STICKY_TOP + subtab_rows + 1
        # Translate agent-tab ranges' row offsets to absolute terminal rows.
        subtab_ranges = [
            (SUBTAB_FIRST_ROW + r, c1, c2, key) for (r, c1, c2, key) in rel_ranges
        ]
        # Body
        active = next((a for a in agents if _agent_key(a) == active_id), None)
        if not agents:
            pass  # subtab_block already says "(no running claude sessions)"
        elif width < SPLIT_MIN_WIDTH or active is None:
            out.extend(render_agents(agents, width))
        else:
            out.extend(render_agent_split(active, nav_index, width))
    else:
        out.extend(render_overview(summary, sessions, stats_cache, width))
    out.append("")
    # Build the footer line and ALWAYS truncate it to terminal width before
    # appending. A wrap would push our pinned title off the top of the screen
    # because cerebro's frame is sized to exactly fill term_rows; one extra
    # visual row at the bottom forces the terminal to scroll.
    if not mouse_enabled:
        # Selection mode: refresh paused, mouse tracking off so the terminal can
        # do native click-drag selection. Make the state obvious in the footer.
        footer_line = (
            f"  {_p('warning')}⚠ select mode{RESET}{DIM}"
            f"  ·  refresh paused, drag to select / copy normally  ·  "
            f"[s] resume  ·  [q] quit{RESET}"
        )
    elif kill_pending_pid is not None:
        footer_line = (
            f"  {_p('warning')}⚠ kill PID {kill_pending_pid}?{RESET}{DIM}"
            f"  press K again to confirm (3 s){RESET}"
        )
    elif status_msg:
        footer_line = f"  {DIM}{status_msg}{RESET}"
    else:
        if tab == "agents":
            footer = (
                "  [1]/[2] · h/l agent · j/k nav · [o] open · [K] kill · "
                "[s] select · [r] refresh · [q] quit"
            )
        else:
            footer = (
                "  [1]/[2] · ↑↓ / wheel scroll · [s] select · "
                "[r] refresh · [q] quit"
            )
        footer_line = DIM + footer + RESET
    out.append(_trim_visible(footer_line, width))
    return ("\n".join(out), sticky_top, subtab_ranges)


def _terminal_size() -> tuple[int, int]:
    try:
        size = shutil.get_terminal_size((100, 30))
        return size.columns, size.lines
    except OSError:
        return 100, 30


_SGR_MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_CSI_KEY_RE = re.compile(rb"\x1b\[([0-9;]*)([A-Za-z~])")


def _read_input(fd: int, timeout: float) -> bytes | None:
    """Block up to `timeout` seconds. Return all available bytes, or None on timeout."""
    try:
        rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return None
    if not rlist:
        return None
    try:
        return os.read(fd, 1024)
    except OSError:
        return None


def _parse_input(buf: bytes):
    """Yield input events from a raw buffer.

    Event tuples:
      ('key',   ch)                                — single character keystroke
      ('csi',   "A" | "B" | "5~" | ... )           — non-mouse CSI sequence (arrows, pgup, etc.)
      ('mouse', button, col, row, press_bool)      — SGR mouse event
    """
    i = 0
    n = len(buf)
    while i < n:
        # SGR mouse event has the form ESC [ < ...
        if buf[i : i + 3] == b"\x1b[<":
            m = _SGR_MOUSE_RE.match(buf, i)
            if m:
                yield (
                    "mouse",
                    int(m.group(1)),
                    int(m.group(2)),
                    int(m.group(3)),
                    m.group(4) == b"M",
                )
                i = m.end()
                continue
        # Any other CSI sequence (arrows, page up/down, home/end, function keys)
        if buf[i : i + 2] == b"\x1b[":
            m = _CSI_KEY_RE.match(buf, i)
            if m:
                yield ("csi", (m.group(1) + m.group(2)).decode("ascii", errors="ignore"))
                i = m.end()
                continue
        # Otherwise: regular keystroke
        ch = buf[i : i + 1].decode("utf-8", errors="ignore")
        if ch:
            yield ("key", ch)
        i += 1


# Top sticky lines: title (0), tab header (1), blank (2)
# Bottom sticky lines: blank (-2), footer (-1)
_STICKY_TOP = 3
_STICKY_BOTTOM = 2


def _apply_scroll(
    frame: str, term_rows: int, offset: int, sticky_top: int = _STICKY_TOP
) -> tuple[str, int, int]:
    """Slice the rendered frame so it fits in `term_rows`, keeping the top
    `sticky_top` lines and bottom 2 lines sticky. Returns
    (rendered_string, clamped_offset, max_offset).
    """
    lines = frame.split("\n")
    if term_rows <= 0 or len(lines) <= term_rows:
        return frame, 0, 0

    top = lines[:sticky_top]
    bottom = lines[-_STICKY_BOTTOM:]
    body = lines[sticky_top:-_STICKY_BOTTOM]

    avail = term_rows - len(top) - len(bottom)
    if avail <= 0:
        return "\n".join(top + bottom), 0, 0

    max_offset = max(0, len(body) - avail)
    clamped = max(0, min(offset, max_offset))
    visible = body[clamped : clamped + avail]

    # Append a "[N hidden ↑ / M hidden ↓]" indicator to the footer when scrolled
    if max_offset > 0:
        above = clamped
        below = len(body) - clamped - avail
        marks = []
        if above > 0:
            marks.append(f"↑{above}")
        if below > 0:
            marks.append(f"↓{below}")
        indicator = "  " + DIM + f"({' '.join(marks)} hidden)" + RESET
        bottom = list(bottom)
        bottom[-1] = bottom[-1] + indicator

    return "\n".join(top + visible + bottom), clamped, max_offset


def live(interval: float, include_branch: bool = True, tab: str = "overview") -> None:
    """Run the live dashboard until the user exits."""
    from . import stats_cache as sc_mod  # noqa: PLC0415

    aggregator = TokenAggregator()
    needs_redraw = {"flag": True}

    def on_resize(_signum, _frame):
        needs_redraw["flag"] = True

    signal.signal(signal.SIGWINCH, on_resize)

    # Switch stdin to cbreak and enable mouse tracking — but only if stdin is a tty.
    fd = None
    old_settings = None
    if sys.stdin.isatty():
        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (termios.error, OSError):
            fd = None
            old_settings = None

    tab_ranges = tab_layout()
    # Per-tab scroll offset so switching back to a tab restores its position.
    scroll_offsets: dict[str, int] = {name: 0 for name in TABS}
    # Agents-tab state: which agent tab is active and which nav item is
    # selected within that agent (0 = the agent itself, 1+ = its sub-agents).
    # Keyed by `_agent_key(agent)` (session_id) since pid is None for finished
    # agents.
    active_agent_id: str | None = None
    nav_indices: dict[str, int] = {}        # agent_key → nav_index
    subtab_ranges: list[tuple[int, int, int, str]] = []
    last_term_rows = {"v": 24}
    # Selection mode: when False, mouse tracking is off and refresh is paused so
    # the terminal's native click-drag selection works for copy/paste.
    mouse_state = {"on": True}
    # Kill confirm: first `K` press sets this; second `K` within 3s sends SIGTERM.
    kill_pending: dict | None = None         # {"pid": int, "expires_at": float}
    # Transient footer flash for non-confirm messages ("opened terminal" etc.).
    status_msg: dict | None = None           # {"text": str, "expires_at": float}
    # Hoisted across iterations so the input-handler closures retain a valid
    # reference while the refresh is paused in select mode.
    agents: list[AgentDetail] = []

    def _switch(new_tab: str) -> None:
        nonlocal tab
        if new_tab in TABS and new_tab != tab:
            tab = new_tab
            needs_redraw["flag"] = True

    def _scroll(delta: int) -> None:
        scroll_offsets[tab] = max(0, scroll_offsets[tab] + delta)
        needs_redraw["flag"] = True

    def _scroll_to(pos: int | None) -> None:
        # None == bottom (resolved against actual content during render)
        scroll_offsets[tab] = pos if pos is not None else 1_000_000
        needs_redraw["flag"] = True

    def _toggle_select_mode() -> None:
        mouse_state["on"] = not mouse_state["on"]
        if fd is not None:
            sys.stdout.write(MOUSE_ON if mouse_state["on"] else MOUSE_OFF)
            sys.stdout.flush()
        needs_redraw["flag"] = True

    sys.stdout.write(HIDE_CURSOR)
    if fd is not None:
        sys.stdout.write(MOUSE_ON)
    sys.stdout.flush()
    try:
        while True:
            cols, term_rows = _terminal_size()
            last_term_rows["v"] = term_rows
            # In selection mode we freeze data + display so the terminal's native
            # selection isn't clobbered. Only re-render on explicit toggle/redraw.
            if mouse_state["on"] or needs_redraw["flag"]:
                summary = aggregator.summarize()
                sessions = list_sessions(include_branch=include_branch)
                stats_cache = sc_mod.load()
                agents = list_agents() if tab == "agents" else []

                # Reconcile active_agent_id with the current agents list.
                if tab == "agents":
                    keys = [_agent_key(a) for a in agents]
                    if active_agent_id not in keys:
                        active_agent_id = keys[0] if keys else None
                    if active_agent_id is not None:
                        active = next(a for a in agents if _agent_key(a) == active_agent_id)
                        max_idx = len(active.sub_agents)  # 0..len inclusive
                        cur = nav_indices.get(active_agent_id, 0)
                        nav_indices[active_agent_id] = max(0, min(cur, max_idx))

                nav_index = nav_indices.get(active_agent_id, 0) if active_agent_id is not None else 0
                # Expire transient footer states before rendering.
                now_ts = time.time()
                if kill_pending and kill_pending["expires_at"] < now_ts:
                    kill_pending = None
                if status_msg and status_msg["expires_at"] < now_ts:
                    status_msg = None
                frame, sticky_top, subtab_ranges = render_frame(
                    tab=tab,
                    summary=summary,
                    sessions=sessions,
                    stats_cache=stats_cache,
                    agents=agents,
                    active_id=active_agent_id,
                    nav_index=nav_index,
                    width=cols,
                    mouse_enabled=mouse_state["on"],
                    kill_pending_pid=kill_pending["pid"] if kill_pending else None,
                    status_msg=status_msg["text"] if status_msg else None,
                )
                view, clamped, _max_offset = _apply_scroll(
                    frame, term_rows, scroll_offsets[tab], sticky_top=sticky_top
                )
                scroll_offsets[tab] = clamped
                sys.stdout.write(HOME + CLEAR_TO_END + view)
                sys.stdout.flush()
            needs_redraw["flag"] = False

            def _cycle_subtab(delta: int) -> None:
                nonlocal active_agent_id
                if not agents:
                    return
                keys = [_agent_key(a) for a in agents]
                if active_agent_id in keys:
                    i = keys.index(active_agent_id)
                else:
                    i = 0
                active_agent_id = keys[(i + delta) % len(keys)]
                # New tab → reset detail scroll
                scroll_offsets[tab] = 0
                needs_redraw["flag"] = True

            def _move_nav(delta: int) -> None:
                if active_agent_id is None:
                    return
                active = next(
                    (a for a in agents if _agent_key(a) == active_agent_id), None
                )
                if active is None:
                    return
                max_idx = len(active.sub_agents)  # 0..len inclusive
                cur = nav_indices.get(active_agent_id, 0)
                nav_indices[active_agent_id] = max(0, min(cur + delta, max_idx))
                scroll_offsets[tab] = 0
                needs_redraw["flag"] = True

            def _active_agent() -> AgentDetail | None:
                if active_agent_id is None:
                    return None
                return next(
                    (a for a in agents if _agent_key(a) == active_agent_id), None
                )

            def _flash(text: str, secs: float = 1.5) -> None:
                nonlocal status_msg
                status_msg = {"text": text, "expires_at": time.time() + secs}
                needs_redraw["flag"] = True

            def _handle_open() -> None:
                a = _active_agent()
                if a is None:
                    _flash("no agent selected")
                    return
                resume_cmd = (
                    f"claude --resume {a.session_id}" if a.session_id else None
                )
                # Live agent with a known TTY → focus that terminal directly.
                if a.is_live and a.tty and term_ctl.focus_tty(a.tty):
                    _flash(f"focused {a.tty}")
                    return
                # Fallback: scan every process on a real TTY for one whose cwd
                # matches the agent's cwd. Catches finished agents whose
                # original window is still open, claude-in-tmux, claude
                # launched by VS Code with no TTY, etc.
                if a.cwd:
                    fallback_tty = term_ctl.find_tty_at(a.cwd)
                    if fallback_tty and term_ctl.focus_tty(fallback_tty):
                        _flash(f"focused {fallback_tty}")
                        return
                # No existing window matched — open a new terminal at the cwd.
                if a.cwd and term_ctl.open_at(a.cwd, command=resume_cmd):
                    _flash(f"opened new terminal at {a.cwd}")
                else:
                    _flash("could not open terminal")

            def _request_kill() -> None:
                nonlocal kill_pending
                a = _active_agent()
                if a is None or a.pid is None:
                    _flash("no live process to kill")
                    kill_pending = None
                    return
                if kill_pending and kill_pending["pid"] == a.pid:
                    # Second press within the window → confirm.
                    killed = False
                    try:
                        os.kill(a.pid, signal.SIGTERM)
                        killed = True
                    except (OSError, ProcessLookupError) as e:
                        _flash(f"kill failed: {e}")
                    if killed:
                        # Also close the owning terminal tab so the now-empty
                        # shell doesn't linger. Best-effort — if we can't
                        # address the tab (unknown terminal), we still report
                        # the SIGTERM as the primary action.
                        closed = bool(a.tty) and term_ctl.close_tty(a.tty)
                        if closed:
                            _flash(f"killed PID {a.pid} and closed {a.tty}")
                        else:
                            _flash(f"sent SIGTERM to PID {a.pid}")
                    kill_pending = None
                    needs_redraw["flag"] = True
                    return
                kill_pending = {"pid": a.pid, "expires_at": time.time() + 3.0}
                needs_redraw["flag"] = True

            # Wait for either input or the refresh interval, whichever comes first
            slept = 0.0
            slice_s = 0.2
            while slept < interval and not needs_redraw["flag"]:
                if fd is None:
                    time.sleep(interval - slept)
                    break
                buf = _read_input(fd, min(slice_s, interval - slept))
                if not buf:
                    slept += slice_s
                    continue
                page = max(1, last_term_rows["v"] // 2)
                on_agents = tab == "agents"
                in_select = not mouse_state["on"]
                for event in _parse_input(buf):
                    kind = event[0]
                    if kind == "key":
                        ch = event[1]
                        if ch in ("q", "Q", "\x03"):
                            raise KeyboardInterrupt
                        if ch in ("s", "S"):
                            _toggle_select_mode()
                            continue
                        # In select mode, swallow everything else to preserve the
                        # terminal's text selection. Only `s` (resume) and `q`
                        # (quit) above are honored.
                        if in_select:
                            continue
                        # Capital K — kill the active agent (with confirm).
                        if ch == "K":
                            _request_kill()
                            continue
                        # Any other key cancels a pending kill confirm.
                        if kill_pending is not None:
                            kill_pending = None
                            needs_redraw["flag"] = True
                        if ch in ("o", "O"):
                            _handle_open()
                            continue
                        if ch == "1":
                            _switch("overview")
                        elif ch == "2":
                            _switch("agents")
                        elif ch in ("r", "R"):
                            needs_redraw["flag"] = True
                        elif ch == "\t":
                            _switch("agents" if tab == "overview" else "overview")
                        elif on_agents and ch == "h":
                            _cycle_subtab(-1)
                        elif on_agents and ch == "l":
                            _cycle_subtab(1)
                        elif on_agents and ch == "j":
                            _move_nav(1)
                        elif on_agents and ch == "k":
                            _move_nav(-1)
                        elif not on_agents and ch == "j":
                            _scroll(1)
                        elif not on_agents and ch == "k":
                            _scroll(-1)
                        elif ch == "g":
                            _scroll_to(0)
                        elif ch == "G":
                            _scroll_to(None)
                        elif ch == "\x04":      # ctrl-D
                            _scroll(page)
                        elif ch == "\x15":      # ctrl-U
                            _scroll(-page)
                    elif kind == "csi":
                        if in_select:
                            continue
                        seq = event[1]
                        if seq == "A":      # up
                            if on_agents:
                                _move_nav(-1)
                            else:
                                _scroll(-1)
                        elif seq == "B":    # down
                            if on_agents:
                                _move_nav(1)
                            else:
                                _scroll(1)
                        elif seq == "D":    # left
                            if on_agents:
                                _cycle_subtab(-1)
                        elif seq == "C":    # right
                            if on_agents:
                                _cycle_subtab(1)
                        elif seq == "5~":
                            _scroll(-page)
                        elif seq == "6~":
                            _scroll(page)
                        elif seq == "H":
                            _scroll_to(0)
                        elif seq == "F":
                            _scroll_to(None)
                    elif kind == "mouse":
                        if in_select:
                            continue
                        _, button, col, row, press = event
                        if not press:
                            continue
                        if button == 0 and row == TAB_ROW:
                            for c_start, c_end, name in tab_ranges:
                                if c_start <= col <= c_end:
                                    _switch(name)
                                    break
                        elif button == 0 and on_agents:
                            # The agent-tab block may span multiple rows;
                            # each range carries its absolute terminal row.
                            for r, c_start, c_end, key in subtab_ranges:
                                if row == r and c_start <= col <= c_end:
                                    if key != active_agent_id:
                                        active_agent_id = key
                                        scroll_offsets[tab] = 0
                                        needs_redraw["flag"] = True
                                    break
                        elif button == 64:    # wheel up
                            _scroll(-3)
                        elif button == 65:    # wheel down
                            _scroll(3)
    except KeyboardInterrupt:
        pass
    finally:
        if fd is not None:
            sys.stdout.write(MOUSE_OFF)
        if fd is not None and old_settings is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()


def once(include_branch: bool = True, tab: str = "overview") -> None:
    from . import stats_cache as sc_mod  # noqa: PLC0415

    aggregator = TokenAggregator()
    cols, _ = _terminal_size()
    summary = aggregator.summarize()
    sessions = list_sessions(include_branch=include_branch)
    stats_cache = sc_mod.load()
    agents = list_agents() if tab == "agents" else []
    active_id = _agent_key(agents[0]) if agents else None
    frame, _sticky, _ranges = render_frame(
        tab=tab,
        summary=summary,
        sessions=sessions,
        stats_cache=stats_cache,
        agents=agents,
        active_id=active_id,
        nav_index=0,
        width=cols,
    )
    print(frame)
