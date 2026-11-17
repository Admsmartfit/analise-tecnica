"""Unit tests for sync_universe routine."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from ts.core.calendar import B3Calendar
from ts.core.config import Settings
from ts.data.fake_mt5 import FakeMT5
from ts.data.mt5_gateway import MT5Gateway
from ts.data.ohlcv_store import OhlcvStore
from ts.data.sync import sync_universe
from ts.data.validation import DataValidator
from ts.persistence.db import DatabaseManager


def test_sync_universe_skips_holiday(tmp_path: Path, b3_calendar: B3Calendar) -> None:
    settings = Settings()
    settings.system.data_dir = str(tmp_path / "data")
    settings.system.db_path = str(tmp_path / "trading.db")

    holiday = date(2024, 1, 1)  # New year
    report = sync_universe(asof=holiday, settings=settings, calendar=b3_calendar)
    assert report.status == "SKIPPED_HOLIDAY"
    assert report.exit_code == 0


def test_sync_universe_success_and_idempotence(
    tmp_path: Path, sample_daily_df: pd.DataFrame, b3_calendar: B3Calendar
) -> None:
    settings = Settings()
    data_dir = tmp_path / "data"
    settings.system.data_dir = str(data_dir)
    settings.system.db_path = str(tmp_path / "trading.db")
    settings.mt5.ibov_symbol = "IBOV"

    store = OhlcvStore(data_dir=data_dir / "ohlcv" / "d1")
    db = DatabaseManager(db_path=settings.system.db_path)
    validator = DataValidator(calendar=b3_calendar)

    # Setup FakeMT5 with valid session data for PETR4 and IBOV
    clean_df = sample_daily_df[sample_daily_df["session_date"].apply(b3_calendar.is_session)].copy()
    fake = FakeMT5()
    fake.set_bars("PETR4", clean_df)
    fake.set_bars("IBOV", clean_df)
    gateway = MT5Gateway(mt5_module=fake, settings=settings)

    asof = clean_df["session_date"].max()
    report1 = sync_universe(
        asof=asof,
        tickers=["PETR4"],
        gateway=gateway,
        store=store,
        validator=validator,
        db=db,
        calendar=b3_calendar,
        settings=settings,
    )
    assert report1.status == "OK"
    assert report1.exit_code == 0
    assert "PETR4" in report1.successful_tickers
    assert "IBOV" in report1.successful_tickers

    # Second run should succeed with zero alterations
    report2 = sync_universe(
        asof=asof,
        tickers=["PETR4"],
        gateway=gateway,
        store=store,
        validator=validator,
        db=db,
        calendar=b3_calendar,
        settings=settings,
    )
    assert report2.status == "OK"


def test_sync_universe_partial_failure_continues(
    tmp_path: Path, sample_daily_df: pd.DataFrame, b3_calendar: B3Calendar
) -> None:
    settings = Settings()
    data_dir = tmp_path / "data"
    settings.system.data_dir = str(data_dir)
    settings.system.db_path = str(tmp_path / "trading.db")
    settings.mt5.ibov_symbol = "IBOV"

    store = OhlcvStore(data_dir=data_dir / "ohlcv" / "d1")
    db = DatabaseManager(db_path=settings.system.db_path)
    validator = DataValidator(calendar=b3_calendar)

    clean_df = sample_daily_df[sample_daily_df["session_date"].apply(b3_calendar.is_session)].copy()
    fake = FakeMT5()
    fake.set_bars("PETR4", clean_df)
    fake.set_bars("IBOV", clean_df)
    # Ticker "FAIL1", "FAIL2", "FAIL3" are not added to fake -> DataUnavailable
    gateway = MT5Gateway(mt5_module=fake, settings=settings)

    asof = clean_df["session_date"].max()
    report = sync_universe(
        asof=asof,
        tickers=["PETR4", "FAIL1", "FAIL2", "FAIL3"],
        gateway=gateway,
        store=store,
        validator=validator,
        db=db,
        calendar=b3_calendar,
        settings=settings,
    )
    # PETR4 and IBOV succeeded, while FAIL1..3 failed
    assert "PETR4" in report.successful_tickers
    assert "IBOV" in report.successful_tickers
    failed_names = [t[0] for t in report.failed_tickers]
    assert "FAIL1" in failed_names
    assert "FAIL2" in failed_names
    assert "FAIL3" in failed_names
