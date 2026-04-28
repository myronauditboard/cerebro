"""Incremental aggregator for token usage across all session jsonl files."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass
class DayTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    msgs: int = 0


@dataclass
class FileCache:
    mtime: float = 0.0
    offset: int = 0
    by_day: dict[date, DayTotals] = field(default_factory=dict)


@dataclass
class Bucket:
    label: str
    input_tokens: int = 0
    output_tokens: int = 0
    msgs: int = 0
    sessions: int = 0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "msgs": self.msgs,
            "sessions": self.sessions,
        }


@dataclass
class TokenSummary:
    today: Bucket
    week: Bucket
    lifetime: Bucket

    def to_dict(self) -> dict:
        return {
            "today": self.today.to_dict(),
            "week": self.week.to_dict(),
            "lifetime": self.lifetime.to_dict(),
        }


class TokenAggregator:
    """Maintains an mtime-keyed cache of per-file per-day totals.

    Scanning is incremental: on each `summarize()` call, files whose mtime
    hasn't changed since the last scan are skipped, and changed files are
    read only from the previously-recorded byte offset onward.
    """

    def __init__(self) -> None:
        self._cache: dict[Path, FileCache] = {}

    def _scan_file(self, path: Path) -> None:
        try:
            st = path.stat()
        except FileNotFoundError:
            self._cache.pop(path, None)
            return

        cache = self._cache.setdefault(path, FileCache())
        if cache.mtime == st.st_mtime and cache.offset == st.st_size:
            return  # unchanged

        try:
            with path.open("rb") as f:
                f.seek(cache.offset)
                # If the file got truncated/rotated, restart from 0
                if cache.offset > st.st_size:
                    f.seek(0)
                    cache.offset = 0
                    cache.by_day.clear()
                buf = f.read()
                cache.offset += len(buf)
        except OSError:
            return

        # The last partial line (if any) is intentionally ignored — reset offset
        # back to the start of that line so the next tick re-reads it whole.
        last_newline = buf.rfind(b"\n")
        if last_newline < 0:
            # No complete line read this tick; rewind offset by full buf length
            cache.offset -= len(buf)
            return
        partial_tail = len(buf) - last_newline - 1
        cache.offset -= partial_tail
        usable = buf[: last_newline + 1]

        for raw in usable.splitlines():
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            usage = msg.get("usage") or {}
            ts_raw = d.get("timestamp")
            if not ts_raw:
                continue
            try:
                # Timestamps look like 2026-04-22T16:28:12.773Z
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            local_day = ts.astimezone().date()

            day = cache.by_day.setdefault(local_day, DayTotals())
            day.input_tokens += int(usage.get("input_tokens", 0) or 0)
            day.input_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
            day.input_tokens += int(usage.get("cache_read_input_tokens", 0) or 0)
            day.output_tokens += int(usage.get("output_tokens", 0) or 0)
            day.msgs += 1

        cache.mtime = st.st_mtime

    def _refresh(self) -> None:
        if not PROJECTS_DIR.exists():
            return
        seen: set[Path] = set()
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl in project_dir.iterdir():
                if jsonl.suffix != ".jsonl":
                    continue
                seen.add(jsonl)
                self._scan_file(jsonl)
        # Drop cache entries for files that have been deleted
        for stale in list(self._cache.keys()):
            if stale not in seen:
                self._cache.pop(stale, None)

    def summarize(self) -> TokenSummary:
        self._refresh()
        today_d = datetime.now().astimezone().date()
        week_start = today_d - timedelta(days=6)  # last 7 days inclusive of today

        today = Bucket("today")
        week = Bucket("week")
        lifetime = Bucket("lifetime")
        today_files: set[Path] = set()
        week_files: set[Path] = set()
        lifetime_files: set[Path] = set()

        for path, fcache in self._cache.items():
            for day, totals in fcache.by_day.items():
                lifetime.input_tokens += totals.input_tokens
                lifetime.output_tokens += totals.output_tokens
                lifetime.msgs += totals.msgs
                lifetime_files.add(path)
                if day >= week_start:
                    week.input_tokens += totals.input_tokens
                    week.output_tokens += totals.output_tokens
                    week.msgs += totals.msgs
                    week_files.add(path)
                if day == today_d:
                    today.input_tokens += totals.input_tokens
                    today.output_tokens += totals.output_tokens
                    today.msgs += totals.msgs
                    today_files.add(path)

        today.sessions = len(today_files)
        week.sessions = len(week_files)
        lifetime.sessions = len(lifetime_files)
        return TokenSummary(today=today, week=week, lifetime=lifetime)
