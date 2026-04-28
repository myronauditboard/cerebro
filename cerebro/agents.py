"""Per-session detail for the Agents tab.

Combines three local sources:

  1. ~/.claude/sessions/<pid>.json     session metadata (name, sessionId, kind, ...)
  2. ~/.claude/projects/<enc>/<sid>.jsonl  conversation transcript (tail = current activity)
  3. cerebro.sessions.list_sessions    running claude PIDs (already detected)

The result is a list of AgentDetail records, one per running session.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .sessions import Session, list_sessions


PROJECTS_DIR = Path.home() / ".claude" / "projects"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"


@dataclass
class AgentDetail:
    pid: int
    is_current: bool
    cwd: str
    repo: str
    branch: str
    age: str
    tty: str
    # From sessions/<pid>.json (may be absent → defaults)
    name: str
    session_id: str
    kind: str
    proc_start: str
    # Derived from the session's jsonl tail
    last_activity_secs: int          # seconds since the last jsonl line was written; -1 if unknown
    last_event: str                  # "tool_use:Bash" / "assistant_text" / "user_msg" / "tool_result" / ""
    last_event_detail: str           # short excerpt (≤ 120 chars)
    status: str                      # "working" / "waiting" / "idle" / "unknown"

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "is_current": self.is_current,
            "cwd": self.cwd,
            "repo": self.repo,
            "branch": self.branch,
            "age": self.age,
            "tty": self.tty,
            "name": self.name,
            "session_id": self.session_id,
            "kind": self.kind,
            "proc_start": self.proc_start,
            "last_activity_secs": self.last_activity_secs,
            "last_event": self.last_event,
            "last_event_detail": self.last_event_detail,
            "status": self.status,
        }


def _load_session_meta(pid: int) -> dict:
    p = SESSIONS_DIR / f"{pid}.json"
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _encode_cwd(cwd: str) -> str:
    """Claude Code encodes cwd into the projects directory name by replacing / with -."""
    if not cwd:
        return ""
    return cwd.replace("/", "-")


def _find_jsonl(cwd: str, *session_ids: str) -> Path | None:
    """Locate the active jsonl for a session.

    Tries each candidate session id (resume_id, then metadata sessionId), then falls
    back to the most-recently-modified jsonl in the cwd's encoded project dir —
    that's the one currently being appended to.
    """
    proj_dir = (PROJECTS_DIR / _encode_cwd(cwd)) if cwd else None

    for sid in session_ids:
        if not sid:
            continue
        if proj_dir is not None:
            cand = proj_dir / f"{sid}.jsonl"
            if cand.exists():
                return cand
        # Cross-project fallback for a known sid
        for proj in PROJECTS_DIR.glob("*"):
            cand = proj / f"{sid}.jsonl"
            if cand.exists():
                return cand

    # Last resort: pick the most-recently-modified jsonl in this project dir.
    if proj_dir is not None and proj_dir.is_dir():
        candidates = sorted(
            (p for p in proj_dir.glob("*.jsonl")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def _tail_lines(path: Path, max_bytes: int = 16_000) -> list[dict]:
    """Read the tail of a jsonl file and return decoded records (best-effort)."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="ignore")
    out: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _summarize_event(d: dict) -> tuple[str, str]:
    """Return (short_event_label, short_detail) for one jsonl record."""
    msg = d.get("message") or {}
    role = d.get("type")
    content = msg.get("content", "")
    # Assistant message: pick the most informative content block
    if role == "assistant" and isinstance(content, list):
        # Prefer tool_use over text for "what's happening now"
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                tool_name = c.get("name", "?")
                tinput = c.get("input") or {}
                # Surface a useful field per known tool, else dump the first kv
                hint = ""
                if tool_name == "Bash":
                    hint = (tinput.get("description") or tinput.get("command") or "")[:80]
                elif tool_name in ("Read", "Edit", "Write"):
                    hint = (tinput.get("file_path") or "")[-80:]
                elif tool_name == "Agent":
                    hint = (tinput.get("description") or "")[:80]
                elif tool_name == "Bash" or tool_name.startswith("mcp__"):
                    hint = (tinput.get("description") or tinput.get("query") or "")[:80]
                else:
                    hint = (tinput.get("description") or "")[:80]
                return (f"tool_use: {tool_name}", hint)
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                return ("assistant_text", (c.get("text") or "")[:120].replace("\n", " "))
        for c in content:
            if isinstance(c, dict) and c.get("type") == "thinking":
                return ("thinking", "")
        return ("assistant", "")
    if role == "user":
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    return ("tool_result", "")
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    return ("user_msg", (c.get("text") or "")[:120].replace("\n", " "))
        elif isinstance(content, str):
            return ("user_msg", content[:120].replace("\n", " "))
        return ("user_msg", "")
    return (role or "", "")


def _derive_status(records: list[dict], age_secs: int) -> str:
    if age_secs < 0:
        return "unknown"
    if not records:
        return "idle"
    last = records[-1]
    role = last.get("type")
    msg = last.get("message") or {}
    content = msg.get("content", [])
    last_is_tool_use = False
    if role == "assistant" and isinstance(content, list):
        last_is_tool_use = any(
            isinstance(c, dict) and c.get("type") == "tool_use" for c in content
        )
    if age_secs < 10:
        return "working"
    if last_is_tool_use and age_secs < 60:
        return "waiting"
    if age_secs < 120:
        return "active"
    if age_secs < 600:
        return "idle"
    return "stale"


def list_agents() -> list[AgentDetail]:
    out: list[AgentDetail] = []
    now = time.time()
    for s in list_sessions(include_branch=True):
        meta = _load_session_meta(s.pid)
        meta_sid = meta.get("sessionId") or ""
        session_id = s.resume_id or meta_sid
        name = meta.get("name") or "—"
        kind = meta.get("kind") or ""
        proc_start = meta.get("procStart") or ""
        jsonl = _find_jsonl(s.cwd, s.resume_id, meta_sid)
        last_event = ""
        last_detail = ""
        last_age = -1
        if jsonl:
            try:
                last_age = int(now - jsonl.stat().st_mtime)
            except OSError:
                last_age = -1
            records = _tail_lines(jsonl)
            # Only conversational records carry "what's happening" — skip system pings,
            # pr-link refs, summaries, etc.
            convo = [r for r in records if r.get("type") in ("assistant", "user")]
            if convo:
                last_event, last_detail = _summarize_event(convo[-1])
            status = _derive_status(convo, last_age)
        else:
            status = "unknown"
            records = []
        out.append(
            AgentDetail(
                pid=s.pid,
                is_current=s.is_current,
                cwd=s.cwd,
                repo=s.repo,
                branch=s.branch,
                age=s.age,
                tty=s.tty,
                name=name,
                session_id=session_id,
                kind=kind,
                proc_start=proc_start,
                last_activity_secs=last_age,
                last_event=last_event,
                last_event_detail=last_detail,
                status=status,
            )
        )
    out.sort(key=lambda a: (not a.is_current, -1 if a.last_activity_secs < 0 else a.last_activity_secs, a.pid))
    return out
