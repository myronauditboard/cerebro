"""cerebro — live CLI dashboard for Claude Code activity.

Usage:
  cerebro [-n SECS] [--no-branch]   live dashboard (default)
  cerebro live [-n SECS] [--no-branch]
  cerebro sessions [--no-branch] [--json]
  cerebro tokens [--json]
  cerebro --once [--no-branch] [--json]
  cerebro help
"""

from __future__ import annotations

import argparse
import json
import sys

from . import render
from .sessions import list_sessions
from .tokens import TokenAggregator


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-branch", action="store_true", help="skip git branch lookup")
    p.add_argument("--json", action="store_true", help="emit JSON, force --once")
    p.add_argument("--once", action="store_true", help="render one frame and exit")
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
    sub.add_parser("help", add_help=False)
    _add_common_flags(parser)
    return parser


def _print_help() -> None:
    print(__doc__.strip())


def _emit_json(include_sessions: bool, include_tokens: bool, include_branch: bool) -> None:
    out: dict = {}
    if include_tokens:
        out["tokens"] = TokenAggregator().summarize().to_dict()
    if include_sessions:
        out["sessions"] = [s.to_dict() for s in list_sessions(include_branch=include_branch)]
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
        )
        return 0

    if cmd == "sessions":
        # one-shot table without summary
        from .render import render_table, _terminal_size  # noqa: PLC0415
        cols, _ = _terminal_size()
        for line in render_table(list_sessions(include_branch=include_branch), cols):
            print(line)
        return 0

    if cmd == "tokens":
        from .render import render_activity, render_summary, _terminal_size  # noqa: PLC0415
        cols, _ = _terminal_size()
        summary = TokenAggregator().summarize()
        for line in render_summary(summary, cols):
            print(line)
        for line in render_activity(summary.activity, cols):
            print(line)
        return 0

    # live
    if args.once:
        render.once(include_branch=include_branch)
    else:
        render.live(interval=args.interval, include_branch=include_branch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
