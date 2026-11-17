"""Unit tests for DataValidator data quality checks."""
from __future__ import annotations

import pandas as pd

from ts.core.calendar import B3Calendar
from ts.data.validation import DataValidator


def test_validation_clean_data(sample_daily_df: pd.DataFrame, b3_calendar: B3Calendar) -> None:
    validator = DataValidator(calendar=b3_calendar)
    # Filter to only valid trading sessions
    clean_df = sample_daily_df[sample_daily_df["session_date"].apply(b3_calendar.is_session)].copy()

    issues = validator.validate_bars("PETR4", clean_df, check_calendar=True)
    assert not validator.has_blocking_issues(issues)


def test_validation_high_low_inconsistency(sample_daily_df: pd.DataFrame, b3_calendar: B3Calendar) -> None:
    validator = DataValidator(calendar=b3_calendar)
    bad_df = sample_daily_df.copy()
    # Invalidate High: make High lower than Open
    bad_df.loc[0, "high"] = bad_df.loc[0, "open"] - 1.0

    issues = validator.validate_bars("PETR4", bad_df, check_calendar=False)
    assert validator.has_blocking_issues(issues)
    assert any(i.check_name == "high_low_inconsistency" for i in issues)


def test_validation_non_positive_price_and_volume(sample_daily_df: pd.DataFrame, b3_calendar: B3Calendar) -> None:
    validator = DataValidator(calendar=b3_calendar)
    bad_df = sample_daily_df.copy()
    bad_df.loc[1, "close"] = -5.0
    bad_df.loc[2, "real_volume"] = -100

    issues = validator.validate_bars("PETR4", bad_df, check_calendar=False)
    assert validator.has_blocking_issues(issues)
    names = [i.check_name for i in issues]
    assert "non_positive_price" in names
    assert "negative_volume" in names


def test_validation_duplicate_session_date(sample_daily_df: pd.DataFrame, b3_calendar: B3Calendar) -> None:
    validator = DataValidator(calendar=b3_calendar)
    bad_df = pd.concat([sample_daily_df, sample_daily_df.iloc[[0]]], ignore_index=True)

    issues = validator.validate_bars("PETR4", bad_df, check_calendar=False)
    assert validator.has_blocking_issues(issues)
    assert any(i.check_name == "duplicate_date" for i in issues)


def test_validation_excessive_return_jump(sample_daily_df: pd.DataFrame, b3_calendar: B3Calendar) -> None:
    validator = DataValidator(calendar=b3_calendar)
    jump_df = sample_daily_df.copy()
    jump_df.loc[5, "close"] = jump_df.loc[4, "close"] * 1.50  # 50% jump

    issues = validator.validate_bars("PETR4", jump_df, check_calendar=False)
    assert any(i.check_name == "excessive_return_jump" and i.severity == "WARN" for i in issues)
