"""Enumerate running Claude Code CLI sessions."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_CLAUDE_CMD_RE = re.compile(r"^claude($|[ ]|--)")
_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


@dataclass
class Session:
    pid: int
    age: str
    tty: str
    cwd: str
    repo: str
    branch: str
    resume_id: str
    is_current: bool
    name: str = ""              # session name from ~/.claude/sessions/<pid>.json
    updated_at_ms: int = 0      # last `updatedAt` from the same file (0 if absent)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "age": self.age,
            "tty": self.tty,
            "cwd": self.cwd,
            "repo": self.repo,
            "branch": self.branch,
            "resume_id": self.resume_id,
            "is_current": self.is_current,
            "name": self.name,
            "updated_at_ms": self.updated_at_ms,
        }


def _session_meta(pid: int) -> tuple[str, int]:
    """Return (name, updated_at_ms) from ~/.claude/sessions/<pid>.json."""
    p = _SESSIONS_DIR / f"{pid}.json"
    try:
        with p.open() as f:
            d = json.load(f)
        name = (d.get("name") or "").strip()
        updated = int(d.get("updatedAt") or 0)
        return (name, updated)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return ("", 0)


def _condense_etime(etime: str) -> str:
    """Convert ps elapsed time to a compact form: 4-06:23:11 → 4d06h, 1:23:45 → 1h23m."""
    s = etime.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = s.split(":")
    if len(parts) == 3:
        h, m, _sec = parts
    elif len(parts) == 2:
        h = "0"
        m, _sec = parts
    else:
        h = "0"
        m = "0"
    h = int(h)
    m = int(m)
    if days > 0:
        return f"{days}d{h:02d}h"
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _ancestor_pids(start: int) -> list[int]:
    """Walk up the parent-PID chain from start to PID 1."""
    chain = [start]
    cur = start
    while cur > 1:
        try:
            out = subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", str(cur)],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if not out:
                break
            ppid = int(out)
        except (subprocess.CalledProcessError, ValueError):
            break
        if ppid == 0 or ppid == cur:
            break
        chain.append(ppid)
        cur = ppid
    return chain


def _cwd_for(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except subprocess.CalledProcessError:
        return ""
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def _branch_for(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        out = subprocess.check_output(
            ["git", "-C", cwd, "branch", "--show-current"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        return out.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _resume_id(cmd: str) -> str:
    parts = cmd.split()
    for i, tok in enumerate(parts):
        if tok == "--resume" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def list_sessions(include_branch: bool = True) -> list[Session]:
    """Return one Session per running `claude` CLI process owned by the current user."""
    user = os.environ.get("USER") or ""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "pid=,etime=,tty=,command=", "-u", user],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []

    cerebro_pid = os.getpid()
    ancestors = set(_ancestor_pids(cerebro_pid))

    sessions: list[Session] = []
    for line in out.splitlines():
        line = line.lstrip()
        if not line:
            continue
        # Split into 4 cols: pid, etime, tty, command (which may contain spaces)
        head, _, command = line.partition(" ")
        try:
            pid = int(head)
        except ValueError:
            continue
        rest = command.lstrip()
        etime, _, rest = rest.partition(" ")
        rest = rest.lstrip()
        tty, _, cmd = rest.partition(" ")
        cmd = cmd.lstrip()

        if not _CLAUDE_CMD_RE.match(cmd):
            continue
        if "unblocked" in cmd:
            continue
        if "native-binary/claude" in cmd:
            continue

        cwd = _cwd_for(pid)
        repo = os.path.basename(cwd) if cwd else ""
        branch = _branch_for(cwd) if include_branch else ""
        name, updated_at_ms = _session_meta(pid)
        sessions.append(
            Session(
                pid=pid,
                age=_condense_etime(etime),
                tty=tty,
                cwd=cwd,
                repo=repo,
                branch=branch,
                resume_id=_resume_id(cmd),
                is_current=(pid in ancestors),
                name=name,
                updated_at_ms=updated_at_ms,
            )
        )

    # Most recently used first. `is_current` and `pid` are tiebreakers so
    # sessions with no metadata file (updated_at_ms = 0) still order stably.
    sessions.sort(key=lambda s: (-s.updated_at_ms, not s.is_current, s.pid))
    return sessions
