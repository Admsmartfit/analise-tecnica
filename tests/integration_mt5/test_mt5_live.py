"""Integration tests with real MetaTrader 5 terminal (requires live MT5 installation)."""
from __future__ import annotations

from datetime import date

import pytest

from ts.core.config import load_settings
from ts.data.mt5_gateway import MT5Gateway


@pytest.mark.mt5
def test_real_mt5_connection_and_sync() -> None:
    settings = load_settings()
    gateway = MT5Gateway(settings=settings)
    try:
        gateway.connect()
        health = gateway.health()
        assert health.connected is True

        ibov = settings.mt5.ibov_symbol
        today = date.today()
        # Fetch 30 days
        bars = gateway.daily_bars(ibov, date(2024, 1, 1), today)
        assert len(bars) >= 5
        assert "close" in bars.columns
    finally:
        gateway.shutdown()
