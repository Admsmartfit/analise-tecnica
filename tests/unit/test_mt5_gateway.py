"""Unit tests for MT5Gateway with FakeMT5 fault injection."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ts.core.errors import (
    NotLoggedInError,
)
from ts.data.fake_mt5 import FakeMT5
from ts.data.mt5_gateway import MT5Gateway


def test_gateway_connect_and_health() -> None:
    fake = FakeMT5()
    gateway = MT5Gateway(mt5_module=fake)
    gateway.connect()

    health = gateway.health()
    assert health.ok is True
    assert health.connected is True
    assert health.trade_allowed is True
    gateway.shutdown()


def test_gateway_not_logged_in_error() -> None:
    fake = FakeMT5()
    fake.set_logged_in(False)
    gateway = MT5Gateway(mt5_module=fake)

    with pytest.raises(NotLoggedInError):
        gateway.connect()


def test_gateway_copy_rates_retry_on_ipc_failure(sample_daily_df: pd.DataFrame) -> None:
    fake = FakeMT5()
    fake.set_bars("PETR4", sample_daily_df)
    # Simulate 2 IPC failures followed by actual data
    rates_raw = fake._bars["PETR4"]
    fake.script("copy_rates_range", [None, None, rates_raw])

    gateway = MT5Gateway(mt5_module=fake)
    gateway.read_backoff_base_s = 0.0
    gateway.connect()

    df = gateway.daily_bars("PETR4", date(2024, 1, 1), date(2024, 2, 1))
    assert not df.empty
    assert "real_volume" in df.columns
    assert "session_date" in df.columns
    gateway.shutdown()


def test_gateway_converts_server_time_to_utc(sample_daily_df: pd.DataFrame) -> None:
    """MT5 stamps D1 bars at 00:00 *server* time; session_date keeps the server date, ts_utc is real UTC."""
    fake = FakeMT5()
    fake.set_bars("PETR4", sample_daily_df)  # fixture: 00:00 of each date
    gateway = MT5Gateway(mt5_module=fake)
    gateway.settings.mt5.server_tz = "America/Sao_Paulo"
    gateway.connect()
    df = gateway.daily_bars("PETR4", date(2024, 1, 1), date(2024, 2, 1))
    first = df.iloc[0]
    assert first["session_date"] == sample_daily_df["session_date"].iloc[0]
    assert first["ts_utc"].hour == 3  # 00:00 BRT (UTC-3) == 03:00 UTC
    gateway.shutdown()


def test_gateway_gives_up_after_three_read_failures(sample_daily_df: pd.DataFrame) -> None:
    from ts.core.errors import DataUnavailableError

    fake = FakeMT5()
    fake.set_bars("PETR4", sample_daily_df)
    fake.script("copy_rates_range", [None, None, None])
    gateway = MT5Gateway(mt5_module=fake)
    gateway.read_backoff_base_s = 0.0
    gateway.connect()
    with pytest.raises(DataUnavailableError):
        gateway.daily_bars("PETR4", date(2024, 1, 1), date(2024, 2, 1))


def test_gateway_symbol_select_called_when_invisible(sample_daily_df: pd.DataFrame) -> None:
    fake = FakeMT5()
    fake.set_bars("VALE3", sample_daily_df)
    # Mark symbol as invisible
    fake._symbols["VALE3"] = fake._symbols["VALE3"]._replace(visible=False)

    gateway = MT5Gateway(mt5_module=fake)
    gateway.connect()
    assert fake.symbol_info("VALE3").visible is False

    # Calling daily_bars should trigger ensure_symbol and make it visible
    gateway.daily_bars("VALE3", date(2024, 1, 1), date(2024, 2, 1))
    assert fake.symbol_info("VALE3").visible is True
    gateway.shutdown()
