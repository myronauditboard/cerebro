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
    raw_input_tokens: int = 0
    cache_tokens: int = 0
    output_tokens: int = 0
    msgs: int = 0


@dataclass
class FileCache:
    mtime: float = 0.0
    offset: int = 0
    by_day: dict[date, DayTotals] = field(default_factory=dict)
    by_model_billable: dict[str, int] = field(default_factory=dict)


@dataclass
class Bucket:
    label: str
    raw_input_tokens: int = 0
    cache_tokens: int = 0
    output_tokens: int = 0
    msgs: int = 0
    sessions: int = 0

    @property
    def billable_tokens(self) -> int:
        """raw_input + output — same definition as /usage's "Total tokens"."""
        return self.raw_input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "raw_input_tokens": self.raw_input_tokens,
            "cache_tokens": self.cache_tokens,
            "output_tokens": self.output_tokens,
            "billable_tokens": self.billable_tokens,
            "msgs": self.msgs,
            "sessions": self.sessions,
        }


@dataclass
class ActivityStats:
    favorite_model: str
    favorite_model_billable: int
    most_active_day: date | None
    most_active_day_billable: int
    current_streak: int

    def to_dict(self) -> dict:
        return {
            "favorite_model": self.favorite_model,
            "favorite_model_billable": self.favorite_model_billable,
            "most_active_day": self.most_active_day.isoformat() if self.most_active_day else None,
            "most_active_day_billable": self.most_active_day_billable,
            "current_streak": self.current_streak,
        }


@dataclass
class TokenSummary:
    today: Bucket
    week: Bucket
    lifetime: Bucket
    activity: ActivityStats

    def to_dict(self) -> dict:
        return {
            "today": self.today.to_dict(),
            "week": self.week.to_dict(),
            "lifetime": self.lifetime.to_dict(),
            "activity": self.activity.to_dict(),
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

            raw_in = int(usage.get("input_tokens", 0) or 0)
            cache_in = int(usage.get("cache_creation_input_tokens", 0) or 0) + int(
                usage.get("cache_read_input_tokens", 0) or 0
            )
            out = int(usage.get("output_tokens", 0) or 0)
            model = (msg.get("model") or "unknown").strip() or "unknown"

            day = cache.by_day.setdefault(local_day, DayTotals())
            day.raw_input_tokens += raw_in
            day.cache_tokens += cache_in
            day.output_tokens += out
            day.msgs += 1
            cache.by_model_billable[model] = (
                cache.by_model_billable.get(model, 0) + raw_in + out
            )

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

        # Day-level rollup across all files for streak / most-active-day / per-day total
        days_total_billable: dict[date, int] = defaultdict(int)
        active_days: set[date] = set()
        model_billable: dict[str, int] = defaultdict(int)

        def add(b: Bucket, t: DayTotals) -> None:
            b.raw_input_tokens += t.raw_input_tokens
            b.cache_tokens += t.cache_tokens
            b.output_tokens += t.output_tokens
            b.msgs += t.msgs

        for path, fcache in self._cache.items():
            for model, n in fcache.by_model_billable.items():
                model_billable[model] += n
            for day, totals in fcache.by_day.items():
                add(lifetime, totals)
                lifetime_files.add(path)
                days_total_billable[day] += totals.raw_input_tokens + totals.output_tokens
                if totals.msgs > 0:
                    active_days.add(day)
                if day >= week_start:
                    add(week, totals)
                    week_files.add(path)
                if day == today_d:
                    add(today, totals)
                    today_files.add(path)

        today.sessions = len(today_files)
        week.sessions = len(week_files)
        lifetime.sessions = len(lifetime_files)

        # Activity-stats panel
        if model_billable:
            fav_model, fav_billable = max(model_billable.items(), key=lambda kv: kv[1])
        else:
            fav_model, fav_billable = "—", 0
        if days_total_billable:
            most_day, most_billable = max(days_total_billable.items(), key=lambda kv: kv[1])
        else:
            most_day, most_billable = None, 0
        # Streak: count consecutive days back from today_d that were active
        streak = 0
        cur = today_d
        while cur in active_days:
            streak += 1
            cur -= timedelta(days=1)

        activity = ActivityStats(
            favorite_model=fav_model,
            favorite_model_billable=fav_billable,
            most_active_day=most_day,
            most_active_day_billable=most_billable,
            current_streak=streak,
        )

        return TokenSummary(today=today, week=week, lifetime=lifetime, activity=activity)
