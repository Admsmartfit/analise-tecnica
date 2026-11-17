"""Pytest configuration and shared fixtures."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ts.core.calendar import B3Calendar
from ts.core.config import Settings
from ts.persistence.db import DatabaseManager


@pytest.fixture
def b3_calendar() -> B3Calendar:
    return B3Calendar()


@pytest.fixture
def sample_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "trading.db")

    settings = Settings()
    settings.system.data_dir = str(data_dir)
    settings.system.db_path = db_path
    settings.system.logs_dir = str(logs_dir)
    return settings


@pytest.fixture
def test_db(tmp_path: Path) -> DatabaseManager:
    db_file = tmp_path / "test_trading.db"
    return DatabaseManager(db_path=str(db_file))


@pytest.fixture
def sample_daily_df() -> pd.DataFrame:
    """Generate 60 trading days of synthetic OHLCV data."""
    dates = pd.date_range("2024-01-02", periods=60, freq="B")
    n = len(dates)
    np.random.seed(42)
    base = 30.0 + np.cumsum(np.random.normal(0.05, 0.5, n))
    highs = base + np.abs(np.random.normal(0.5, 0.2, n))
    lows = base - np.abs(np.random.normal(0.5, 0.2, n))
    opens = (highs + lows) / 2.0
    closes = base
    real_volume = np.random.randint(100000, 500000, n)
    tick_volume = np.random.randint(1000, 5000, n)
    spread = np.ones(n, dtype=int)

    df = pd.DataFrame(
        {
            "session_date": dates.date,
            "ts_utc": pd.to_datetime(dates).tz_localize("UTC"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "real_volume": real_volume,
            "tick_volume": tick_volume,
            "spread": spread,
            "adj_factor": 1.0,
            "source": "mt5:XP-PRD",
            "ingested_at": datetime.now(UTC),
        }
    )
    return df
