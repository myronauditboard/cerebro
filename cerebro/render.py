"""Live render loop + table/summary formatting."""

from __future__ import annotations

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


def fmt_count(n: int) -> str:
    """Compact human-readable count with up to 2 decimals (trailing zeros trimmed).

    1_180_000 → "1.18M" · 12_400_000 → "12.4M" · 12_000_000 → "12M" · 999_990 → "999.99K"
    """
    for unit, divisor in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(n) >= divisor:
            s = f"{n / divisor:.2f}".rstrip("0").rstrip(".")
            return f"{s}{unit}"
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
            "/usage parity",
            ["  (~/.claude/stats-cache.json not found)"],
            inner,
        )
    most_day = sc.most_active_day.strftime("%b %-d") if sc.most_active_day else "—"
    first_d = sc.first_session_date.strftime("%b %-d, %Y") if sc.first_session_date else "—"
    line1 = (
        f"  Total tokens   {fmt_count(sc.total_tokens):>7}"
        f"   ·   Total messages  {sc.total_messages:>5}"
        f"   ·   Sessions  {sc.total_sessions}"
    )
    line2 = (
        f"  Favorite model {sc.favorite_model}  ({fmt_count(sc.favorite_model_tokens)} tokens)"
    )
    line3 = (
        f"  Most active    {most_day}  ({fmt_count(sc.most_active_day_tokens)} tokens)"
        f"   ·   Streak  {sc.current_streak}d"
        f"   ·   Since  {first_d}"
    )
    return _panel("/usage parity", [line1, line2, line3], inner)


def render_activity(act: ActivityStats, width: int) -> list[str]:
    inner = max(60, width - 2)
    fav_label = act.favorite_model or "—"
    fav_n = fmt_count(act.favorite_model_output) if act.favorite_model_output else "0"
    most_day = act.most_active_day.strftime("%b %-d") if act.most_active_day else "—"
    most_n = fmt_count(act.most_active_day_billable) if act.most_active_day_billable else "0"
    streak = act.current_streak
    line1 = f"  Favorite model   {fav_label}  ({fav_n} out tokens)"
    line2 = f"  Most active day  {most_day}  ({most_n} billable)   ·   Streak  {streak}d"
    return _panel("Stats", [line1, line2], inner)


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
    blocks.extend(render_activity(summary.activity, width))
    blocks.extend(render_stats_cache(stats_cache, width))
    blocks.append("")
    blocks.extend(render_table(sessions, width))
    return blocks


def render_tabs_header(active: int, names: list[str]) -> str:
    parts = []
    for i, name in enumerate(names, start=1):
        if i == active:
            parts.append(BOLD + f" {i} {name} " + RESET)
        else:
            parts.append(DIM + f" {i} {name} " + RESET)
    return "  " + "│".join(parts)


def render_frame(
    *,
    tab: str,
    summary: TokenSummary,
    sessions: Sequence[Session],
    stats_cache: StatsCacheSummary,
    agents: Sequence[AgentDetail],
    width: int,
) -> str:
    tab_names = ["overview", "agents"]
    active = 1 if tab == "overview" else 2
    out: list[str] = []
    out.append(BOLD + "cerebro" + RESET + DIM + " — claude code activity" + RESET)
    out.append(render_tabs_header(active, tab_names))
    out.append("")
    if tab == "agents":
        out.extend(render_agents(agents, width))
    else:
        out.extend(render_overview(summary, sessions, stats_cache, width))
    out.append("")
    out.append(DIM + "  [1] overview  ·  [2] agents  ·  [r] refresh  ·  [q] quit" + RESET)
    return "\n".join(out)


def _terminal_size() -> tuple[int, int]:
    try:
        size = shutil.get_terminal_size((100, 30))
        return size.columns, size.lines
    except OSError:
        return 100, 30


def _read_key(timeout: float) -> str | None:
    """Wait up to `timeout` seconds for a single keystroke. Returns the char or None."""
    try:
        rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return None
    if not rlist:
        return None
    try:
        ch = sys.stdin.read(1)
    except OSError:
        return None
    return ch or None


def live(interval: float, include_branch: bool = True, tab: str = "overview") -> None:
    """Run the live dashboard until the user exits."""
    from . import stats_cache as sc_mod  # noqa: PLC0415

    aggregator = TokenAggregator()
    needs_redraw = {"flag": True}

    def on_resize(_signum, _frame):
        needs_redraw["flag"] = True

    signal.signal(signal.SIGWINCH, on_resize)

    # Switch stdin to cbreak so we can read single keys without Enter, but only if it's a tty
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

    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()
    try:
        while True:
            cols, _ = _terminal_size()
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
            sys.stdout.write(HOME + CLEAR_TO_END + frame)
            sys.stdout.flush()
            needs_redraw["flag"] = False
            # Wait for either a keystroke or the refresh interval, whichever comes first
            slept = 0.0
            slice_s = 0.2
            while slept < interval and not needs_redraw["flag"]:
                key = _read_key(min(slice_s, interval - slept)) if fd is not None else None
                if key:
                    if key in ("q", "Q", "\x03"):  # ctrl-C falls through too
                        raise KeyboardInterrupt
                    if key == "1":
                        tab = "overview"
                        needs_redraw["flag"] = True
                    elif key == "2":
                        tab = "agents"
                        needs_redraw["flag"] = True
                    elif key in ("r", "R"):
                        needs_redraw["flag"] = True
                    elif key == "\t":
                        tab = "agents" if tab == "overview" else "overview"
                        needs_redraw["flag"] = True
                else:
                    slept += slice_s if fd is not None else interval  # no tty → just sleep full interval
                    if fd is None:
                        time.sleep(interval)
                        break
    except KeyboardInterrupt:
        pass
    finally:
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
