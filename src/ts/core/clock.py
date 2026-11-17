"""Clock protocol and implementations for time control."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Protocol for getting current time in UTC."""

    def now_utc(self) -> datetime:
        """Return current datetime with timezone UTC."""
        ...


class SystemClock:
    """Real system clock using timezone.utc."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Controllable clock for reproducible testing and simulation."""

    def __init__(self, current_time: datetime | None = None) -> None:
        if current_time is None:
            self._current_time = datetime(2024, 1, 2, 10, 0, 0, tzinfo=UTC)
        else:
            if current_time.tzinfo is None:
                self._current_time = current_time.replace(tzinfo=UTC)
            else:
                self._current_time = current_time.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        if new_time.tzinfo is None:
            self._current_time = new_time.replace(tzinfo=UTC)
        else:
            self._current_time = new_time.astimezone(UTC)

    def advance(self, **kwargs: float) -> datetime:
        from datetime import timedelta

        self._current_time += timedelta(**kwargs)
        return self._current_time
