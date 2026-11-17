"""Unit tests for FakeMT5 mock and fault injection."""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from ts.data.fake_mt5 import (
    MT5_RATES_DTYPE,
    TIMEFRAME_D1,
    FakeMT5,
)


def test_fake_mt5_dtype_and_rates(sample_daily_df: pd.DataFrame) -> None:
    fake = FakeMT5()
    fake.set_bars("PETR4", sample_daily_df)

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    rates = fake.copy_rates_range("PETR4", TIMEFRAME_D1, start, end)

    assert isinstance(rates, np.ndarray)
    assert rates.dtype == MT5_RATES_DTYPE
    assert len(rates) > 0
    assert "real_volume" in rates.dtype.names
    assert "spread" in rates.dtype.names


def test_fake_mt5_scripted_responses() -> None:
    fake = FakeMT5()
    fake.script("copy_rates_range", [None, None, np.array([], dtype=MT5_RATES_DTYPE)])

    d1 = datetime(2024, 1, 1, tzinfo=UTC)
    d2 = datetime(2024, 1, 5, tzinfo=UTC)

    # First two calls return None (simulating IPC errors)
    assert fake.copy_rates_range("VALE3", TIMEFRAME_D1, d1, d2) is None
    assert fake.copy_rates_range("VALE3", TIMEFRAME_D1, d1, d2) is None
    # Third call returns empty array
    res = fake.copy_rates_range("VALE3", TIMEFRAME_D1, d1, d2)
    assert isinstance(res, np.ndarray)


def test_fake_mt5_disconnect_after() -> None:
    fake = FakeMT5()
    fake.initialize()
    fake.disconnect_after(calls=2)

    assert fake.terminal_info().connected is True  # Call 1
    info = fake.terminal_info()                    # Call 2 -> drops connection
    assert info is None or fake.terminal_info() is None


def test_fake_mt5_truncate_history(sample_daily_df: pd.DataFrame) -> None:
    fake = FakeMT5()
    fake.set_bars("PETR4", sample_daily_df)
    fake.truncate_history("PETR4", keep_last=10)

    d1 = datetime(2024, 1, 1, tzinfo=UTC)
    d2 = datetime(2024, 12, 31, tzinfo=UTC)
    rates = fake.copy_rates_range("PETR4", TIMEFRAME_D1, d1, d2)
    assert len(rates) == 10
