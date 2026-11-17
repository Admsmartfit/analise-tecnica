"""Unit tests for technical analysis indicators."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ts.indicators.ta import (
    bollinger_bands,
    donchian,
    ema,
    macd,
    rsi,
    rvol,
    sma,
)


def test_sma_and_ema() -> None:
    s = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    s_ma = sma(s, 3)
    assert np.isnan(s_ma.iloc[0])
    assert np.isnan(s_ma.iloc[1])
    assert s_ma.iloc[2] == 11.0
    assert s_ma.iloc[3] == 12.0
    assert s_ma.iloc[4] == 13.0

    s_ema = ema(s, 3)
    assert not np.isnan(s_ema.iloc[2])


def test_bollinger_bands_properties() -> None:
    s = pd.Series([20.0, 21.0, 19.0, 20.5, 19.5, 20.0, 21.0, 20.0] * 5)
    upper, middle, lower, bw = bollinger_bands(s, period=20, num_std=2.0, ddof=0)

    # Symmetry
    diff_upper = (upper - middle).dropna()
    diff_lower = (middle - lower).dropna()
    np.testing.assert_allclose(diff_upper.values, diff_lower.values, atol=1e-8)

    # Bandwidth definition: (upper - lower) / middle
    expected_bw = (upper - lower) / middle
    np.testing.assert_allclose(bw.dropna().values, expected_bw.dropna().values, atol=1e-8)


def test_rsi_bounds() -> None:
    np.random.seed(123)
    prices = pd.Series(50.0 + np.cumsum(np.random.normal(0, 1, 100)))
    rsi_vals = rsi(prices, period=14).dropna()
    assert (rsi_vals >= 0.0).all()
    assert (rsi_vals <= 100.0).all()


def test_macd_relationship() -> None:
    np.random.seed(456)
    prices = pd.Series(30.0 + np.cumsum(np.random.normal(0.1, 0.5, 100)))
    line, signal, hist = macd(prices, fast=12, slow=26, signal_period=9)

    valid_idx = hist.dropna().index
    calc_hist = line.loc[valid_idx] - signal.loc[valid_idx]
    np.testing.assert_allclose(hist.loc[valid_idx].values, calc_hist.values, atol=1e-8)


def test_donchian_strictly_excludes_current_bar() -> None:
    highs = pd.Series([10.0, 11.0, 12.0, 15.0, 100.0])  # Huge spike at index 4
    lows = pd.Series([9.0, 9.5, 10.0, 11.0, 80.0])

    upper, lower = donchian(highs, lows, period=3)
    # At index 4, Donchian upper must look at index 1, 2, 3 (max is 15.0, NOT 100.0!)
    assert upper.iloc[4] == 15.0


def test_rvol_strictly_excludes_current_bar() -> None:
    volumes = pd.Series([1000.0, 1000.0, 1000.0, 5000.0])
    rvol_series = rvol(volumes, period=3)
    # At index 3, baseline is mean of [1000, 1000, 1000] = 1000. RVol = 5000 / 1000 = 5.0
    assert rvol_series.iloc[3] == 5.0
