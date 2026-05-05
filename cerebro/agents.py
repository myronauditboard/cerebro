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
from dataclasses import dataclass, field
from pathlib import Path

from .sessions import Session, list_sessions


PROJECTS_DIR = Path.home() / ".claude" / "projects"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# A sub-agent is "active" if its transcript was written within this many seconds.
# Older transcripts are still shown but only up to SUB_AGENT_RECENT_LIMIT of them.
SUB_AGENT_ACTIVE_SECS = 60
SUB_AGENT_RECENT_LIMIT = 10


@dataclass
class Interaction:
    """One prompt/response pair from a session's jsonl."""
    user_text: str          # the user's prompt text (untruncated)
    user_ts: str            # ISO timestamp of the user message (or "")
    assistant_text: str     # final assistant text in this turn (or "" if still running)
    assistant_ts: str
    tool_calls: int         # number of tool_use blocks issued in this turn
    in_progress: bool       # True if no final assistant text yet

    def to_dict(self) -> dict:
        return {
            "user_text": self.user_text,
            "user_ts": self.user_ts,
            "assistant_text": self.assistant_text,
            "assistant_ts": self.assistant_ts,
            "tool_calls": self.tool_calls,
            "in_progress": self.in_progress,
        }


@dataclass
class SubAgentDetail:
    agent_id: str                    # from filename: agent-<id>.jsonl
    agent_type: str                  # from meta.json["agentType"]
    description: str                 # from meta.json["description"]
    prompt: str                      # full prompt the parent sent (untruncated)
    prompt_excerpt: str              # first user record's prompt, ≤ 200 chars
    last_activity_secs: int          # seconds since last jsonl write; -1 if unknown
    last_event: str
    last_event_detail: str
    status: str
    is_active: bool                  # mtime within SUB_AGENT_ACTIVE_SECS
    recent_interactions: list[Interaction] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "description": self.description,
            "prompt": self.prompt,
            "prompt_excerpt": self.prompt_excerpt,
            "last_activity_secs": self.last_activity_secs,
            "last_event": self.last_event,
            "last_event_detail": self.last_event_detail,
            "status": self.status,
            "is_active": self.is_active,
            "recent_interactions": [i.to_dict() for i in self.recent_interactions],
        }


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
    sub_agents: list[SubAgentDetail] = field(default_factory=list)
    recent_interactions: list[Interaction] = field(default_factory=list)

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
            "sub_agents": [sa.to_dict() for sa in self.sub_agents],
            "recent_interactions": [i.to_dict() for i in self.recent_interactions],
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


def _read_first_record(path: Path, hard_cap_bytes: int = 256_000) -> dict | None:
    """Read the first jsonl line and return the decoded record.

    A single line can be many KB (long prompts), so we read in 16 KB chunks
    until we hit a newline or the hard cap.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as f:
            while total < hard_cap_bytes:
                buf = f.read(16_384)
                if not buf:
                    break
                nl = buf.find(b"\n")
                if nl >= 0:
                    chunks.append(buf[:nl])
                    break
                chunks.append(buf)
                total += len(buf)
    except OSError:
        return None
    line = b"".join(chunks).decode("utf-8", errors="ignore").strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _tail_lines(path: Path, max_bytes: int = 256_000) -> list[dict]:
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


def _is_user_text_record(rec: dict) -> bool:
    if rec.get("type") != "user":
        return False
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        s = content.strip()
        return bool(s) and not s.startswith("<command-name>") and not s.startswith("[Request interrupted")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = (c.get("text") or "").strip()
                if t and not t.startswith("<command-name>"):
                    return True
    return False


def _read_until_n_user_records(
    path: Path, target: int = 10, chunk_size: int = 256_000, max_bytes: int = 16_000_000
) -> list[dict]:
    """Walk the jsonl backwards in chunks until we have ≥ target user-text records,
    or we hit the start of the file, or we've read max_bytes. Returns records in
    chronological order. Avoids pulling huge sessions in full."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []

    parsed: list[dict] = []  # newest chunks at the front; we prepend earlier chunks
    user_count = 0
    pos = size
    bytes_read = 0
    try:
        with path.open("rb") as f:
            while pos > 0 and bytes_read < max_bytes:
                new_pos = max(0, pos - chunk_size)
                read_len = pos - new_pos
                f.seek(new_pos)
                data = f.read(read_len)
                pos = new_pos
                bytes_read += read_len

                # Drop the first (partial) line unless we've reached the start.
                if pos > 0:
                    nl = data.find(b"\n")
                    if nl < 0:
                        # No line boundary in this chunk — keep going.
                        continue
                    chunk_text = data[nl + 1 :].decode("utf-8", errors="ignore")
                else:
                    chunk_text = data.decode("utf-8", errors="ignore")

                chunk_records: list[dict] = []
                for line in chunk_text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk_records.append(r)
                    if _is_user_text_record(r):
                        user_count += 1
                # Earlier chunks go to the front
                parsed = chunk_records + parsed
                if user_count >= target:
                    break
    except OSError:
        return parsed
    return parsed


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


def _user_text_content(rec: dict) -> str:
    """Return the human-prompt text from a user record, or '' if it's a tool_result-only
    record or an internal stub like a slash-command invocation."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("<command-name>") or s.startswith("[Request interrupted"):
            return ""
        return s
    if isinstance(content, list):
        pieces: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text") or ""
                if t.strip().startswith("<command-name>"):
                    continue
                pieces.append(t)
        return "\n".join(pieces).strip()
    return ""


def _assistant_text_content(rec: dict) -> str:
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        pieces = [
            c.get("text") or ""
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return "\n".join(pieces).strip()
    return ""


def _count_tool_uses(rec: dict) -> int:
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return sum(
            1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use"
        )
    return 0


def _recent_interactions(
    convo: list[dict], limit: int = 10, seed_prompt: str | None = None
) -> list[Interaction]:
    """Walk conversational records and pair them into prompt/response interactions.

    A new interaction starts whenever a user record has actual text content
    (not a tool_result-only record). Subsequent assistant records contribute
    their tool_use count and final text to the in-progress interaction.
    Returns the most recent `limit` interactions in chronological order.

    If `seed_prompt` is given (sub-agent mode), produces exactly one interaction
    pairing that prompt with the assistant activity in `convo`.
    """
    if seed_prompt is not None:
        tool_count = 0
        asst = ""
        asst_ts = ""
        for rec in convo:
            if rec.get("type") == "assistant":
                tool_count += _count_tool_uses(rec)
                t = _assistant_text_content(rec)
                if t:
                    asst = t
                    asst_ts = rec.get("timestamp", "")
        return [
            Interaction(
                user_text=seed_prompt,
                user_ts="",
                assistant_text=asst,
                assistant_ts=asst_ts,
                tool_calls=tool_count,
                in_progress=(asst == ""),
            )
        ]

    interactions: list[Interaction] = []
    current: Interaction | None = None
    for rec in convo:
        rtype = rec.get("type")
        if rtype == "user":
            user_text = _user_text_content(rec)
            if user_text:
                if current is not None:
                    interactions.append(current)
                current = Interaction(
                    user_text=user_text,
                    user_ts=rec.get("timestamp", ""),
                    assistant_text="",
                    assistant_ts="",
                    tool_calls=0,
                    in_progress=True,
                )
        elif rtype == "assistant" and current is not None:
            current.tool_calls += _count_tool_uses(rec)
            asst = _assistant_text_content(rec)
            if asst:
                current.assistant_text = asst
                current.assistant_ts = rec.get("timestamp", "")
                current.in_progress = False
    if current is not None:
        interactions.append(current)
    return interactions[-limit:]


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


def _extract_prompt_text(record: dict) -> str:
    """Pull a usable prompt excerpt from a sub-agent's first user record."""
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                return c.get("text") or ""
        # Fall back: stringify whatever's there
        return " ".join(str(c) for c in content if c)
    return ""


def _list_sub_agents(parent_jsonl: Path, now: float) -> list[SubAgentDetail]:
    """Return sub-agents for a parent session.

    Includes all sub-agents whose transcript was written within
    SUB_AGENT_ACTIVE_SECS (active/just-finished) plus up to
    SUB_AGENT_RECENT_LIMIT older completed ones (most recent first).
    """
    subagents_dir = parent_jsonl.parent / parent_jsonl.stem / "subagents"
    if not subagents_dir.is_dir():
        return []

    files: list[tuple[Path, float]] = []
    for p in subagents_dir.glob("agent-*.jsonl"):
        try:
            files.append((p, p.stat().st_mtime))
        except OSError:
            continue
    if not files:
        return []
    files.sort(key=lambda t: t[1], reverse=True)

    active: list[tuple[Path, float]] = []
    completed: list[tuple[Path, float]] = []
    for path, mtime in files:
        if (now - mtime) < SUB_AGENT_ACTIVE_SECS:
            active.append((path, mtime))
        else:
            completed.append((path, mtime))
    kept = active + completed[:SUB_AGENT_RECENT_LIMIT]

    out: list[SubAgentDetail] = []
    for path, mtime in kept:
        agent_id = path.stem
        if agent_id.startswith("agent-"):
            agent_id = agent_id[len("agent-"):]
        meta_path = path.with_suffix(".meta.json")
        agent_type = "agent"
        description = ""
        if meta_path.exists():
            try:
                with meta_path.open() as f:
                    meta = json.load(f)
                agent_type = meta.get("agentType") or agent_type
                description = meta.get("description") or ""
            except (OSError, json.JSONDecodeError):
                pass

        first = _read_first_record(path)
        prompt_full = ""
        prompt_excerpt = ""
        if first is not None:
            text = _extract_prompt_text(first)
            prompt_full = text
            prompt_excerpt = text.replace("\n", " ").strip()[:200]

        records = _tail_lines(path)
        convo = [r for r in records if r.get("type") in ("assistant", "user")]
        last_event = ""
        last_detail = ""
        if convo:
            last_event, last_detail = _summarize_event(convo[-1])
        last_age = int(now - mtime)
        status = _derive_status(convo, last_age)
        is_active = last_age < SUB_AGENT_ACTIVE_SECS

        out.append(
            SubAgentDetail(
                agent_id=agent_id,
                agent_type=agent_type,
                description=description,
                prompt=prompt_full,
                prompt_excerpt=prompt_excerpt,
                last_activity_secs=last_age,
                last_event=last_event,
                last_event_detail=last_detail,
                status=status,
                is_active=is_active,
                recent_interactions=_recent_interactions(convo, seed_prompt=prompt_full),
            )
        )
    return out


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
        sub_agents: list[SubAgentDetail] = []
        recent: list[Interaction] = []
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
            sub_agents = _list_sub_agents(jsonl, now)
            # Read further back to cover at least 10 user prompts. For long
            # sessions the 256 KB tail above only covers a handful of turns.
            deep_records = _read_until_n_user_records(jsonl, target=10)
            deep_convo = [r for r in deep_records if r.get("type") in ("assistant", "user")]
            recent = _recent_interactions(deep_convo)
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
                sub_agents=sub_agents,
                recent_interactions=recent,
            )
        )
    out.sort(key=lambda a: (not a.is_current, -1 if a.last_activity_secs < 0 else a.last_activity_secs, a.pid))
    return out
