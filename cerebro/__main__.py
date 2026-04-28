"""cerebro — live CLI dashboard for Claude Code activity.

Usage:
  cerebro [-n SECS] [--no-branch]   live dashboard (default, opens overview tab)
  cerebro live [-n SECS] [--tab agents]
  cerebro sessions [--no-branch] [--json]
  cerebro tokens [--json]
  cerebro agents [--json]           one-shot per-session detail
  cerebro --once [--tab agents] [--json]
  cerebro help

Live keys:  [1] overview  [2] agents  [tab] cycle  [r] refresh  [q] quit
"""

from __future__ import annotations

import argparse
import json
import sys

from . import render
from . import stats_cache as sc_mod
from .agents import list_agents
from .sessions import list_sessions
from .tokens import TokenAggregator


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-branch", action="store_true", help="skip git branch lookup")
    p.add_argument("--json", action="store_true", help="emit JSON, force --once")
    p.add_argument("--once", action="store_true", help="render one frame and exit")
    p.add_argument(
        "--tab",
        choices=("overview", "agents"),
        default="overview",
        help="which tab to start on (default: overview)",
    )
    p.add_argument(
        "-n",
        "--interval",
        type=float,
        default=2.0,
        help="refresh interval in seconds (default: 2)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cerebro", add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    live_p = sub.add_parser("live", add_help=False)
    _add_common_flags(live_p)
    sess_p = sub.add_parser("sessions", add_help=False)
    _add_common_flags(sess_p)
    tok_p = sub.add_parser("tokens", add_help=False)
    _add_common_flags(tok_p)
    agents_p = sub.add_parser("agents", add_help=False)
    _add_common_flags(agents_p)
    sub.add_parser("help", add_help=False)
    _add_common_flags(parser)
    return parser


def _print_help() -> None:
    print(__doc__.strip())


def _emit_json(
    include_sessions: bool,
    include_tokens: bool,
    include_branch: bool,
    include_agents: bool = False,
) -> None:
    out: dict = {}
    if include_tokens:
        out["tokens"] = TokenAggregator().summarize().to_dict()
        out["stats_cache"] = sc_mod.load().to_dict()
    if include_sessions:
        out["sessions"] = [s.to_dict() for s in list_sessions(include_branch=include_branch)]
    if include_agents:
        out["agents"] = [a.to_dict() for a in list_agents()]
    json.dump(out, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.help or args.cmd == "help":
        _print_help()
        return 0

    cmd = args.cmd or "live"
    include_branch = not args.no_branch

    if args.json:
        _emit_json(
            include_sessions=(cmd in ("live", "sessions")),
            include_tokens=(cmd in ("live", "tokens")),
            include_branch=include_branch,
            include_agents=(cmd in ("live", "agents") or args.tab == "agents"),
        )
        return 0

    if cmd == "agents":
        from .render import render_agents, _terminal_size  # noqa: PLC0415
        cols, _ = _terminal_size()
        for line in render_agents(list_agents(), cols):
            print(line)
        return 0

    if cmd == "sessions":
        # one-shot table without summary
        from .render import render_table, _terminal_size  # noqa: PLC0415
        cols, _ = _terminal_size()
        for line in render_table(list_sessions(include_branch=include_branch), cols):
            print(line)
        return 0

    if cmd == "tokens":
        from .render import render_activity, render_stats_cache, render_summary, _terminal_size  # noqa: PLC0415
        cols, _ = _terminal_size()
        summary = TokenAggregator().summarize()
        for line in render_summary(summary, cols):
            print(line)
        for line in render_activity(summary, cols):
            print(line)
        for line in render_stats_cache(sc_mod.load(), cols):
            print(line)
        return 0

    # live
    if args.once:
        render.once(include_branch=include_branch, tab=args.tab)
    else:
        render.live(interval=args.interval, include_branch=include_branch, tab=args.tab)
    return 0


if __name__ == "__main__":
    sys.exit(main())
