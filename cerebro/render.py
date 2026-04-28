"""Live render loop + table/summary formatting."""

from __future__ import annotations

import shutil
import signal
import sys
import time
from typing import Sequence

from .sessions import Session, list_sessions
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
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


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
            f"raw {fmt_count(b.raw_input_tokens):>5}  ·  "
            f"cache {fmt_count(b.cache_tokens):>6}  ·  "
            f"out {fmt_count(b.output_tokens):>6}  ·  "
            f"billable {fmt_count(b.billable_tokens):>6}  ·  "
            f"{b.msgs:>4} msg  ·  {b.sessions:>3} sess"
        )

    return _panel(
        "Tokens",
        [line(summary.today), line(summary.week), line(summary.lifetime)],
        inner,
    )


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


def render_frame(summary: TokenSummary, sessions: Sequence[Session], width: int) -> str:
    blocks: list[str] = []
    blocks.append(BOLD + "cerebro" + RESET + DIM + " — claude code activity" + RESET)
    blocks.append("")
    blocks.extend(render_summary(summary, width))
    blocks.extend(render_activity(summary.activity, width))
    blocks.append("")
    blocks.extend(render_table(sessions, width))
    blocks.append("")
    blocks.append(DIM + "  ctrl-C to exit" + RESET)
    return "\n".join(blocks)


def _terminal_size() -> tuple[int, int]:
    try:
        size = shutil.get_terminal_size((100, 30))
        return size.columns, size.lines
    except OSError:
        return 100, 30


def live(interval: float, include_branch: bool = True) -> None:
    """Run the live dashboard until the user exits."""
    aggregator = TokenAggregator()
    needs_redraw = {"flag": True}

    def on_resize(_signum, _frame):
        needs_redraw["flag"] = True

    signal.signal(signal.SIGWINCH, on_resize)

    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()
    try:
        while True:
            cols, _ = _terminal_size()
            summary = aggregator.summarize()
            sessions = list_sessions(include_branch=include_branch)
            frame = render_frame(summary, sessions, cols)
            sys.stdout.write(HOME + CLEAR_TO_END + frame)
            sys.stdout.flush()
            needs_redraw["flag"] = False
            # Sleep in small slices so SIGWINCH gets timely response
            slept = 0.0
            slice_s = 0.1
            while slept < interval and not needs_redraw["flag"]:
                time.sleep(slice_s)
                slept += slice_s
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()


def once(include_branch: bool = True) -> None:
    aggregator = TokenAggregator()
    cols, _ = _terminal_size()
    summary = aggregator.summarize()
    sessions = list_sessions(include_branch=include_branch)
    print(render_frame(summary, sessions, cols))
