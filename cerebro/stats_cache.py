"""Read ~/.claude/stats-cache.json — the same source `/usage` Stats reads from.

Exists so cerebro can display Claude Code's own counts verbatim alongside
its independent jsonl-derived numbers. The two normally drift by a few
percent; the parity panel makes the comparison explicit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


STATS_CACHE = Path.home() / ".claude" / "stats-cache.json"


@dataclass
class StatsCacheSummary:
    available: bool
    total_tokens: int           # sum of dailyModelTokens — matches /usage "Total tokens"
    total_messages: int
    total_sessions: int
    favorite_model: str
    favorite_model_tokens: int
    most_active_day: date | None
    most_active_day_tokens: int
    current_streak: int
    first_session_date: date | None
    last_computed: date | None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "total_tokens": self.total_tokens,
            "total_messages": self.total_messages,
            "total_sessions": self.total_sessions,
            "favorite_model": self.favorite_model,
            "favorite_model_tokens": self.favorite_model_tokens,
            "most_active_day": self.most_active_day.isoformat() if self.most_active_day else None,
            "most_active_day_tokens": self.most_active_day_tokens,
            "current_streak": self.current_streak,
            "first_session_date": self.first_session_date.isoformat() if self.first_session_date else None,
            "last_computed": self.last_computed.isoformat() if self.last_computed else None,
        }


def _empty(available: bool = False) -> StatsCacheSummary:
    return StatsCacheSummary(
        available=available,
        total_tokens=0,
        total_messages=0,
        total_sessions=0,
        favorite_model="—",
        favorite_model_tokens=0,
        most_active_day=None,
        most_active_day_tokens=0,
        current_streak=0,
        first_session_date=None,
        last_computed=None,
    )


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        # firstSessionDate is full ISO with Z; lastComputedDate is YYYY-MM-DD
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None


def load() -> StatsCacheSummary:
    if not STATS_CACHE.exists():
        return _empty(available=False)
    try:
        with STATS_CACHE.open() as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty(available=False)

    daily_tokens = d.get("dailyModelTokens") or []
    total_tokens = sum(sum(e.get("tokensByModel", {}).values()) for e in daily_tokens)

    # Favorite model = highest cumulative tokens across dailyModelTokens
    by_model: dict[str, int] = {}
    for e in daily_tokens:
        for m, n in e.get("tokensByModel", {}).items():
            by_model[m] = by_model.get(m, 0) + int(n or 0)
    if by_model:
        fav_model, fav_n = max(by_model.items(), key=lambda kv: kv[1])
    else:
        fav_model, fav_n = "—", 0

    # Most active day = day with max total tokens
    if daily_tokens:
        most_entry = max(
            daily_tokens,
            key=lambda e: sum(e.get("tokensByModel", {}).values()),
        )
        most_day = _parse_iso_date(most_entry.get("date"))
        most_n = sum(most_entry.get("tokensByModel", {}).values())
    else:
        most_day, most_n = None, 0

    # Streak from dailyActivity: consecutive days with messageCount > 0 ending at today
    activity = d.get("dailyActivity") or []
    active_days = {
        _parse_iso_date(e.get("date"))
        for e in activity
        if (e.get("messageCount") or 0) > 0
    }
    active_days.discard(None)
    today_d = date.today()
    streak = 0
    cur = today_d
    while cur in active_days:
        streak += 1
        cur -= timedelta(days=1)

    return StatsCacheSummary(
        available=True,
        total_tokens=total_tokens,
        total_messages=int(d.get("totalMessages") or 0),
        total_sessions=int(d.get("totalSessions") or 0),
        favorite_model=fav_model,
        favorite_model_tokens=fav_n,
        most_active_day=most_day,
        most_active_day_tokens=most_n,
        current_streak=streak,
        first_session_date=_parse_iso_date(d.get("firstSessionDate")),
        last_computed=_parse_iso_date(d.get("lastComputedDate")),
    )
