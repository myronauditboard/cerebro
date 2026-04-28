"""Live render loop + table/summary formatting."""

from __future__ import annotations

import shutil
import signal
import sys
import time
from typing import Sequence

from .sessions import Session, list_sessions
from .tokens import Bucket, TokenAggregator, TokenSummary


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


def render_summary(summary: TokenSummary, width: int) -> list[str]:
    inner = max(40, width - 2)
    title = " Tokens "
    pad = (inner - len(title)) // 2
    top = "┌" + "─" * pad + title + "─" * (inner - pad - len(title)) + "┐"
    bot = "└" + "─" * inner + "┘"

    def line(b: Bucket) -> str:
        body = (
            f"  {b.label.capitalize():<8} "
            f"{fmt_count(b.input_tokens):>7} in   ·   "
            f"{fmt_count(b.output_tokens):>6} out   ·   "
            f"{b.msgs:>5} msgs across {b.sessions:>3} sessions"
        )
        if len(body) > inner:
            body = body[:inner]
        return "│" + body.ljust(inner) + "│"

    return [top, line(summary.today), line(summary.week), line(summary.lifetime), bot]


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
