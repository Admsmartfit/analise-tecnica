"""B3 trading calendar and session utilities."""
from __future__ import annotations

from datetime import date, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from ts.core.errors import CalendarError


class B3Calendar:
    """B3 Trading Calendar handling holidays, weekends, special hours and daylight saving changes."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        if config_path is None:
            # Default to config/b3_calendar.yaml relative to current working dir or repo root
            candidate = Path("config/b3_calendar.yaml")
            if not candidate.exists():
                candidate = Path(__file__).resolve().parent.parent.parent.parent / "config" / "b3_calendar.yaml"
            config_path = candidate

        self.config_path = Path(config_path)
        if not self.config_path.exists():
            raise CalendarError(f"Calendar config file not found at: {self.config_path}")

        with open(self.config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        cal_data = data.get("calendar", {})
        self.holidays: set[date] = {
            date.fromisoformat(h) for h in cal_data.get("holidays", [])
        }
        self.special_sessions: dict[date, dict[str, Any]] = {
            date.fromisoformat(item["date"]): {
                "open_time": time.fromisoformat(item["open_time"]),
                "close_time": time.fromisoformat(item["close_time"]),
            }
            for item in cal_data.get("special_sessions", [])
        }
        self.periods: list[dict[str, Any]] = [
            {
                "start_date": date.fromisoformat(p["start_date"]),
                "end_date": date.fromisoformat(p["end_date"]),
                "open_time": time.fromisoformat(p["open_time"]),
                "close_time": time.fromisoformat(p["close_time"]),
                "pre_close_auction": time.fromisoformat(p.get("pre_close_auction", "16:55:00")),
            }
            for p in cal_data.get("trading_hours_periods", [])
        ]

    def is_weekend(self, d: date) -> bool:
        """Check if date falls on Saturday (5) or Sunday (6)."""
        return d.weekday() >= 5

    def is_holiday(self, d: date) -> bool:
        """Check if date is a configured B3 holiday."""
        return d in self.holidays

    def is_session(self, d: date) -> bool:
        """A date is a trading session if not weekend and not holiday."""
        if self.is_weekend(d):
            return False
        if self.is_holiday(d):
            return False
        return True

    def sessions_between(self, start: date, end: date) -> list[date]:
        """Return all trading sessions between start and end (inclusive), chronologically."""
        if start > end:
            return []
        sessions: list[date] = []
        curr = start
        while curr <= end:
            if self.is_session(curr):
                sessions.append(curr)
            curr += timedelta(days=1)
        return sessions

    def previous_session(self, d: date) -> date:
        """Return the nearest trading session strictly before d."""
        curr = d - timedelta(days=1)
        while not self.is_session(curr):
            curr -= timedelta(days=1)
            # Guard against infinite loop
            if (d - curr).days > 30:
                raise CalendarError(f"No trading session found within 30 days prior to {d}")
        return curr

    def next_session(self, d: date) -> date:
        """Return the nearest trading session strictly after d."""
        curr = d + timedelta(days=1)
        while not self.is_session(curr):
            curr += timedelta(days=1)
            if (curr - d).days > 30:
                raise CalendarError(f"No trading session found within 30 days after {d}")
        return curr

    def open_time(self, d: date) -> time:
        """Return open time for a trading session."""
        if not self.is_session(d):
            raise CalendarError(f"{d} is not a trading session")
        if d in self.special_sessions:
            return self.special_sessions[d]["open_time"]
        for p in self.periods:
            if p["start_date"] <= d <= p["end_date"]:
                return p["open_time"]
        # Default standard B3 open
        return time(10, 0, 0)

    def close_time(self, d: date) -> time:
        """Return regular market close time for a trading session."""
        if not self.is_session(d):
            raise CalendarError(f"{d} is not a trading session")
        if d in self.special_sessions:
            return self.special_sessions[d]["close_time"]
        for p in self.periods:
            if p["start_date"] <= d <= p["end_date"]:
                return p["close_time"]
        # Default standard B3 close
        return time(17, 0, 0)

    def pre_close_auction_time(self, d: date) -> time:
        """Return start of pre-closing auction."""
        if not self.is_session(d):
            raise CalendarError(f"{d} is not a trading session")
        for p in self.periods:
            if p["start_date"] <= d <= p["end_date"]:
                return p["pre_close_auction"]
        return time(16, 55, 0)


# Module-level convenience functions
_default_calendar: B3Calendar | None = None


def get_calendar() -> B3Calendar:
    global _default_calendar
    if _default_calendar is None:
        _default_calendar = B3Calendar()
    return _default_calendar


def is_session(d: date) -> bool:
    return get_calendar().is_session(d)


def sessions_between(start: date, end: date) -> list[date]:
    return get_calendar().sessions_between(start, end)


def previous_session(d: date) -> date:
    return get_calendar().previous_session(d)


def next_session(d: date) -> date:
    return get_calendar().next_session(d)


def close_time(d: date) -> time:
    return get_calendar().close_time(d)
