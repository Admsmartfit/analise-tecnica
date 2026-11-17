"""Calendar, clock, universe, store and gateway edge cases (PRD S1 acceptance: >= 90% on core/data)."""
from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pandas as pd
import pytest

import ts.core.calendar as calmod
from ts.core.calendar import B3Calendar
from ts.core.clock import FakeClock, SystemClock
from ts.core.errors import CalendarError, ConnectivityError, DataError, TerminalNotRunningError
from ts.data.fake_mt5 import FakeMT5
from ts.data.mt5_gateway import MT5Gateway
from ts.data.ohlcv_store import OhlcvStore
from ts.data.universe import get_quarter_str, universe_on


def test_calendar_hours_and_dst_switches_2025() -> None:
    cal = B3Calendar()
    # US DST switches in 2025 move the B3 close (PRD Passo 3)
    assert cal.close_time(date(2025, 3, 7)) == time(18, 0)
    assert cal.close_time(date(2025, 3, 10)) == time(17, 0)
    assert cal.close_time(date(2025, 10, 31)) == time(17, 0)
    assert cal.close_time(date(2025, 11, 3)) == time(18, 0)
    assert cal.pre_close_auction_time(date(2025, 11, 3)) == time(17, 55)
    assert cal.open_time(date(2025, 3, 5)) == time(13, 0)  # Ash Wednesday
    assert cal.close_time(date(2025, 3, 5)) == time(17, 55)
    assert cal.open_time(date(2023, 6, 1)) == time(10, 0)  # outside configured periods -> default
    assert cal.close_time(date(2023, 6, 1)) == time(17, 0)
    assert cal.pre_close_auction_time(date(2023, 6, 1)) == time(16, 55)
    for fn in (cal.open_time, cal.close_time, cal.pre_close_auction_time):
        with pytest.raises(CalendarError):
            fn(date(2025, 12, 25))


def test_calendar_navigation_and_module_helpers() -> None:
    cal = B3Calendar()
    assert not cal.is_session(date(2025, 3, 3))  # Carnaval
    assert not cal.is_session(date(2025, 6, 19))  # Corpus Christi
    assert not cal.is_session(date(2025, 12, 31))  # véspera de Ano-Novo
    assert cal.previous_session(date(2025, 3, 5)) == date(2025, 2, 28)
    assert cal.next_session(date(2025, 12, 23)) == date(2025, 12, 26)
    assert cal.sessions_between(date(2025, 1, 10), date(2025, 1, 1)) == []
    assert calmod.is_session(date(2025, 1, 2))
    assert calmod.sessions_between(date(2025, 1, 1), date(2025, 1, 3)) == [date(2025, 1, 2), date(2025, 1, 3)]
    assert calmod.previous_session(date(2025, 1, 2)) == date(2024, 12, 30)
    assert calmod.next_session(date(2024, 12, 30)) == date(2025, 1, 2)
    assert calmod.close_time(date(2025, 1, 2)) == time(18, 0)
    with pytest.raises(CalendarError):
        B3Calendar(Path("does/not/exist.yaml"))


def test_calendar_guard_against_runaway_search(tmp_path: Path) -> None:
    holidays = "\n".join(f'    - "{d.date()}"' for d in pd.date_range("2030-01-01", "2030-03-15"))
    p = tmp_path / "cal.yaml"
    p.write_text(f"calendar:\n  holidays:\n{holidays}\n", encoding="utf-8")
    cal = B3Calendar(p)
    with pytest.raises(CalendarError):
        cal.previous_session(date(2030, 3, 1))
    with pytest.raises(CalendarError):
        cal.next_session(date(2030, 1, 1))


def test_clocks() -> None:
    fc = FakeClock()
    assert fc.now_utc().tzinfo is not None
    fc.set_time(datetime(2025, 1, 2, 12, 0))
    assert fc.now_utc().tzinfo == UTC
    fc.set_time(datetime(2025, 1, 2, 12, 0, tzinfo=UTC))
    assert fc.advance(hours=1).hour == 13
    assert FakeClock(datetime(2025, 1, 1)).now_utc().tzinfo == UTC
    assert FakeClock(datetime(2025, 1, 1, tzinfo=UTC)).now_utc().year == 2025
    assert SystemClock().now_utc().tzinfo is not None


def test_universe_helpers(tmp_path: Path) -> None:
    assert get_quarter_str(date(2024, 8, 1)) == "2024_Q3"
    with pytest.raises(FileNotFoundError):
        universe_on(date(2024, 1, 1), tmp_path)
    (tmp_path / "ibx100_2023_Q2.csv").write_text("ticker\nAAA3\n", encoding="utf-8")
    assert universe_on(date(2020, 1, 1), tmp_path) == ["AAA3"]  # before first file: earliest, with warning
    assert "PETR4" in universe_on(date(2025, 1, 2))  # repository default directory


def test_store_reads_and_edge_cases(sample_daily_df: pd.DataFrame, tmp_path: Path) -> None:
    store = OhlcvStore(tmp_path)
    assert store.read("NONE").empty
    assert store.upsert("X", sample_daily_df.iloc[0:0]) == (0, 0)
    minimal = sample_daily_df.drop(columns=["adj_factor", "source", "spread"])
    store.upsert("MIN", minimal)
    back = store.read("MIN", adjusted=True)
    assert (back["adj_factor"] == 1.0).all() and (back["source"] == "unknown").all()
    assert store.exists("MIN") and not store.exists("NONE")


def test_store_replace_failure_raises(sample_daily_df: pd.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ts.data.ohlcv_store as mod

    store = OhlcvStore(tmp_path)

    def locked(*a: object, **k: object) -> None:
        raise PermissionError("file in use")

    monkeypatch.setattr(mod.os, "replace", locked)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    with pytest.raises(DataError):
        store.upsert("PETR4", sample_daily_df)
    assert not (tmp_path / "PETR4.parquet.tmp").exists()


def test_gateway_connect_failures_and_tick_age(sample_daily_df: pd.DataFrame) -> None:
    fake = FakeMT5()
    fake.script("initialize", [False] * 5)
    gw = MT5Gateway(fake)
    gw.connect_backoff_base_s = 0
    with pytest.raises(TerminalNotRunningError):
        gw.connect()

    fake2 = FakeMT5()
    fake2.script("initialize", [RuntimeError("ipc"), True])
    gw2 = MT5Gateway(fake2)
    gw2.connect_backoff_base_s = 0
    gw2.connect()
    assert gw2.last_tick_age_seconds("PETR4") is not None
    with gw2:
        pass

    fake3 = FakeMT5()
    fake3.disconnect_after(1)
    gw3 = MT5Gateway(fake3)
    with pytest.raises(ConnectivityError):
        gw3.connect()

    fake4 = FakeMT5()
    fake4.script("symbol_info", [None])
    gw4 = MT5Gateway(fake4)
    gw4.ensure_symbol("NEWX3")  # unknown -> symbol_select
    assert fake4.symbol_info("NEWX3").visible
