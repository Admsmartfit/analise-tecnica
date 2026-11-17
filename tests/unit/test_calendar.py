"""Unit tests for B3 trading calendar."""
from __future__ import annotations

from datetime import date, time

from ts.core.calendar import B3Calendar


def test_calendar_weekends(b3_calendar: B3Calendar) -> None:
    # Saturday and Sunday
    assert not b3_calendar.is_session(date(2024, 1, 6))
    assert not b3_calendar.is_session(date(2024, 1, 7))
    assert b3_calendar.is_weekend(date(2024, 1, 6))


def test_calendar_carnaval_and_corpus_christi(b3_calendar: B3Calendar) -> None:
    # 2024 Carnaval: Monday 2024-02-12 and Tuesday 2024-02-13
    assert not b3_calendar.is_session(date(2024, 2, 12))
    assert not b3_calendar.is_session(date(2024, 2, 13))

    # 2024 Corpus Christi: 2024-05-30
    assert not b3_calendar.is_session(date(2024, 5, 30))

    # 2024 New Year's Eve: 2024-12-31
    assert not b3_calendar.is_session(date(2024, 12, 31))


def test_calendar_ash_wednesday_special_open(b3_calendar: B3Calendar) -> None:
    # 2024 Ash Wednesday: 2024-02-14 (open at 13:00)
    d = date(2024, 2, 14)
    assert b3_calendar.is_session(d)
    assert b3_calendar.open_time(d) == time(13, 0, 0)
    assert b3_calendar.close_time(d) == time(17, 55, 0)


def test_calendar_sessions_between_and_navigation(b3_calendar: B3Calendar) -> None:
    start = date(2024, 1, 1)  # Holiday
    end = date(2024, 1, 5)    # Friday
    sessions = b3_calendar.sessions_between(start, end)
    assert sessions == [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
    ]

    assert b3_calendar.previous_session(date(2024, 1, 2)) == date(2023, 12, 28)
    assert b3_calendar.next_session(date(2024, 1, 5)) == date(2024, 1, 8)


def test_calendar_trading_hours_and_dst_2025(b3_calendar: B3Calendar) -> None:
    # In 2025:
    # March 10, 2025 to Nov 02, 2025: close at 17:00
    # From Nov 03, 2025: close at 18:00
    assert b3_calendar.close_time(date(2025, 3, 12)) == time(17, 0, 0)
    assert b3_calendar.close_time(date(2025, 11, 5)) == time(18, 0, 0)
