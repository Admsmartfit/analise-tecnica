"""Unit tests for deterministic PivotTracker and prefix lookahead tests."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from ts.indicators.pivots import PivotTracker, detect_pivots


def test_pivot_alternation_and_consecutive_lows() -> None:
    # Construct sequence with two consecutive lows: second is lower
    # Short ATR so it is warmed up within this 15-bar series (no pivot is accepted before ATR exists).
    tracker = PivotTracker("TEST", window_n=2, magnitude_atr_k=0.1, atr_period=2)

    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(15)]
    # Highs and lows designed to produce valleys and peaks
    lows =  [20, 19, 18, 19, 21, 25, 23, 20, 17, 18, 22, 26, 25, 24, 25]
    highs = [22, 21, 20, 21, 23, 27, 25, 22, 20, 21, 24, 28, 27, 26, 27]

    for d, h, lo in zip(dates, highs, lows, strict=False):
        tracker.update(d, open_p=(h + lo) / 2, high=h, low=lo, close=(h + lo) / 2)

    pivots = tracker.confirmed_pivots
    assert len(pivots) >= 2
    # Ensure alternation
    for i in range(len(pivots) - 1):
        assert pivots[i].kind != pivots[i + 1].kind


def test_pivot_prefix_test_zero_lookahead(sample_daily_df: pd.DataFrame) -> None:
    """Prefix test: detector on df[:t] == full-run event log replayed up to t.

    Filtering the *final* pivot state by confirmed_date is not enough: alternation can replace a pivot that
    was valid at t with one confirmed later (see test_prefix_and_parity.test_pivot_replacement_*).
    """
    from ts.indicators.pivots import detect_pivot_events, replay

    df = sample_daily_df.sort_values(by="session_date").reset_index(drop=True)
    events = detect_pivot_events(df, "PETR4", window_n=2, magnitude_atr_k=0.5)

    for t_idx in range(25, len(df), 5):
        asof_date = df.loc[t_idx, "session_date"]
        prefix_df = df.iloc[: t_idx + 1]

        prefix_pivots = detect_pivots(prefix_df, "PETR4", window_n=2, magnitude_atr_k=0.5)
        expected_pivots = replay(events, asof_date)

        assert len(prefix_pivots) == len(expected_pivots), f"Lookahead mismatch at t={asof_date}"
        for p1, p2 in zip(prefix_pivots, expected_pivots, strict=False):
            assert p1.kind == p2.kind
            assert p1.pivot_date == p2.pivot_date
            assert p1.confirmed_date == p2.confirmed_date
            assert np.isclose(p1.price, p2.price, atol=1e-6)
