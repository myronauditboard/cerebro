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

from .agents import AgentDetail, list_agents
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
    inner = max(60, width - 2)

    def line(b: Bucket) -> str:
        return (
            f"  {b.label.capitalize():<9}"
            f"raw {fmt_count(b.raw_input_tokens):>7} · "
            f"cache {fmt_count(b.cache_tokens):>7} · "
            f"out {fmt_count(b.output_tokens):>7} · "
            f"bill {fmt_count(b.billable_tokens):>7} · "
            f"{b.msgs:>4} msg · {b.sessions:>3} sess"
        )

    return _panel(
        "Tokens",
        [line(summary.today), line(summary.week), line(summary.lifetime)],
        inner,
    )


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
    line_today = (
        f"  Today          {fmt_count(sc.today_tokens):>7}"
        f"   ·   Messages         {sc.today_messages:>5}"
        f"   ·   Sessions  {sc.today_sessions}"
    )
    line_total = (
        f"  Total tokens   {fmt_count(sc.total_tokens):>7}"
        f"   ·   Total messages  {sc.total_messages:>5}"
        f"   ·   Sessions  {sc.total_sessions}"
    )
    line_fav = (
        f"  Favorite model {sc.favorite_model}  ({fmt_count(sc.favorite_model_tokens)} tokens)"
    )
    line_active = (
        f"  Most active    {most_day}  ({fmt_count(sc.most_active_day_tokens)} tokens)"
        f"   ·   Streak  {sc.current_streak}d"
        f"   ·   Since  {first_d}"
    )
    return _panel("/usage", [line_today, line_total, line_fav, line_active], inner)


def render_activity(summary: TokenSummary, width: int) -> list[str]:
    inner = max(60, width - 2)
    act = summary.activity
    today = summary.today
    fav_label = act.favorite_model or "—"
    fav_n = fmt_count(act.favorite_model_output) if act.favorite_model_output else "0"
    most_day = act.most_active_day.strftime("%b %-d") if act.most_active_day else "—"
    most_n = fmt_count(act.most_active_day_billable) if act.most_active_day_billable else "0"
    streak = act.current_streak
    line_today = (
        f"  Today            {fmt_count(today.billable_tokens):>7} billable"
        f"  ·  {today.msgs:>4} msg"
        f"  ·  {today.sessions:>2} sess"
    )
    line_fav = f"  Favorite model   {fav_label}  ({fav_n} out tokens)"
    line_active = (
        f"  Most active day  {most_day}  ({most_n} billable)   ·   Streak  {streak}d"
    )
    return _panel("Stats", [line_today, line_fav, line_active], inner)


def render_table(sessions: Sequence[Session], width: int) -> list[str]:
    if not sessions:
        return [DIM + "  (no running claude sessions)" + RESET]

    repo_w = max(4, min(28, max((len(s.repo) for s in sessions), default=4)))
    branch_w = max(6, min(32, max((len(s.branch) for s in sessions), default=6)))
    tty_w = max(3, max((len(s.tty) for s in sessions), default=3))

    header = (
        "   "
        + "PID".ljust(7)
        + "AGE".ljust(8)
        + "REPO".ljust(repo_w + 2)
        + "BRANCH".ljust(branch_w + 2)
        + "TTY".ljust(tty_w + 2)
        + "SESSION"
    )
    lines = [BOLD + header + RESET]

    for s in sessions:
        marker = "* " if s.is_current else "  "
        sess = _truncate(s.resume_id, 9) if s.resume_id else "-"
        row = (
            marker
            + " "
            + str(s.pid).ljust(7)
            + s.age.ljust(8)
            + _truncate(s.repo, repo_w).ljust(repo_w + 2)
            + _truncate(s.branch or "-", branch_w).ljust(branch_w + 2)
            + s.tty.ljust(tty_w + 2)
            + sess
        )
        if s.is_current:
            row = BOLD + row + RESET
        lines.append(row)
    return lines


def _status_color(status: str) -> str:
    return {
        "working": f"{CSI}32m",   # green
        "waiting": f"{CSI}33m",   # yellow
        "active": f"{CSI}36m",    # cyan
        "idle": f"{CSI}90m",      # bright black
        "stale": f"{CSI}90m",
        "unknown": f"{CSI}90m",
    }.get(status, "")


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
                if len(detail) > max_detail:
                    detail = detail[: max_detail - 1] + "…"
                line += f"  {DIM}·{RESET}  {detail}"
            out.append(line)
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
            # Active: violet background + white bold text
            parts.append(f"{CSI}48;5;60m{CSI}1;97m {display} {RESET}")
        else:
            parts.append(f"{DIM} {display} {RESET}")
        if i < len(names) - 1:
            parts.append(TAB_SEPARATOR)
    return "".join(parts)


def render_frame(
    *,
    tab: str,
    summary: TokenSummary,
    sessions: Sequence[Session],
    stats_cache: StatsCacheSummary,
    agents: Sequence[AgentDetail],
    width: int,
) -> str:
    out: list[str] = []
    out.append(BOLD + "cerebro" + RESET + DIM + " — claude code activity" + RESET)
    out.append(render_tabs_header(tab))
    out.append("")
    if tab == "agents":
        out.extend(render_agents(agents, width))
    else:
        out.extend(render_overview(summary, sessions, stats_cache, width))
    out.append("")
    out.append(
        DIM
        + "  click a tab or [1]/[2]  ·  ↑↓ / wheel scroll  ·  [r] refresh  ·  [q] quit"
        + RESET
    )
    return "\n".join(out)


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


def _apply_scroll(frame: str, term_rows: int, offset: int) -> tuple[str, int, int]:
    """Slice the rendered frame so it fits in `term_rows`, keeping the top 3 lines and
    bottom 2 lines sticky. Returns (rendered_string, clamped_offset, max_offset).
    """
    lines = frame.split("\n")
    if term_rows <= 0 or len(lines) <= term_rows:
        return frame, 0, 0

    top = lines[:_STICKY_TOP]
    bottom = lines[-_STICKY_BOTTOM:]
    body = lines[_STICKY_TOP:-_STICKY_BOTTOM]

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
    last_term_rows = {"v": 24}

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

    sys.stdout.write(HIDE_CURSOR)
    if fd is not None:
        sys.stdout.write(MOUSE_ON)
    sys.stdout.flush()
    try:
        while True:
            cols, term_rows = _terminal_size()
            last_term_rows["v"] = term_rows
            summary = aggregator.summarize()
            sessions = list_sessions(include_branch=include_branch)
            stats_cache = sc_mod.load()
            agents = list_agents() if tab == "agents" else []
            frame = render_frame(
                tab=tab,
                summary=summary,
                sessions=sessions,
                stats_cache=stats_cache,
                agents=agents,
                width=cols,
            )
            view, clamped, _max_offset = _apply_scroll(frame, term_rows, scroll_offsets[tab])
            scroll_offsets[tab] = clamped
            sys.stdout.write(HOME + CLEAR_TO_END + view)
            sys.stdout.flush()
            needs_redraw["flag"] = False
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
                for event in _parse_input(buf):
                    kind = event[0]
                    if kind == "key":
                        ch = event[1]
                        if ch in ("q", "Q", "\x03"):
                            raise KeyboardInterrupt
                        if ch == "1":
                            _switch("overview")
                        elif ch == "2":
                            _switch("agents")
                        elif ch in ("r", "R"):
                            needs_redraw["flag"] = True
                        elif ch == "\t":
                            _switch("agents" if tab == "overview" else "overview")
                        elif ch == "j":
                            _scroll(1)
                        elif ch == "k":
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
                        seq = event[1]
                        if seq == "A":
                            _scroll(-1)
                        elif seq == "B":
                            _scroll(1)
                        elif seq == "5~":
                            _scroll(-page)
                        elif seq == "6~":
                            _scroll(page)
                        elif seq == "H":
                            _scroll_to(0)
                        elif seq == "F":
                            _scroll_to(None)
                    elif kind == "mouse":
                        _, button, col, row, press = event
                        if not press:
                            continue
                        if button == 0 and row == TAB_ROW:
                            for c_start, c_end, name in tab_ranges:
                                if c_start <= col <= c_end:
                                    _switch(name)
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
    print(
        render_frame(
            tab=tab,
            summary=summary,
            sessions=sessions,
            stats_cache=stats_cache,
            agents=agents,
            width=cols,
        )
    )
