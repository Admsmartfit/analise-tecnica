"""Technical indicators in NumPy/Pandas (PRD Sprint 2), causal and TA-Lib-style seeded.

Seeding convention (matches TA-Lib and Wilder's original definitions):

* ``ema(n)``: first value = SMA of the first ``n`` valid inputs, then ``alpha = 2/(n+1)``.
* ``rma(n)`` (Wilder): first value = SMA of the first ``n`` valid inputs, then ``alpha = 1/n``.
* ``atr(n)``: True Range starts on the 2nd bar (needs the previous close), ATR = RMA(TR).
* ``rsi(n)``: average gain/loss seeded by the SMA of the first ``n`` changes, then Wilder.
* ``adx(n)``: +DM/-DM/TR smoothed by Wilder RMA; ADX = RMA(DX).

Every function is causal: the value at ``t`` only uses inputs ``<= t`` (verified by the prefix test).
Leading NaNs are skipped; an interior NaN propagates (the recursion restarts after it) so a data hole is
never silently smoothed over.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _seeded_recursive(values: np.ndarray, period: int, alpha: float) -> np.ndarray:
    out = np.full(values.shape[0], np.nan)
    n = values.shape[0]
    i = 0
    while i < n:
        # find a run of `period` consecutive valid values to seed
        while i < n and np.isnan(values[i]):
            i += 1
        j = i
        while j < n and not np.isnan(values[j]) and j - i < period:
            j += 1
        if j - i < period:
            i = j
            continue
        seed_end = i + period - 1
        prev = float(np.mean(values[i : seed_end + 1]))
        out[seed_end] = prev
        k = seed_end + 1
        while k < n and not np.isnan(values[k]):
            prev = prev + alpha * (values[k] - prev)
            out[k] = prev
            k += 1
        i = k
    return out


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average (alpha = 2/(period+1)), SMA-seeded."""
    vals = series.to_numpy(dtype=float)
    return pd.Series(_seeded_recursive(vals, period, 2.0 / (period + 1.0)), index=series.index)


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's running moving average (alpha = 1/period), SMA-seeded."""
    vals = series.to_numpy(dtype=float)
    return pd.Series(_seeded_recursive(vals, period, 1.0 / period), index=series.index)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range. NaN on the first bar (no previous close), as in TA-Lib."""
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr[prev_close.isna()] = np.nan
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's RMA smoothing."""
    return rma(true_range(high, low, close), period)


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """+DI, -DI and ADX with Wilder smoothing (declared explicitly, PRD 1.1.4)."""
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=low.index)
    first = high.shift(1).isna()
    plus_dm[first] = np.nan
    minus_dm[first] = np.nan

    atr_s = rma(true_range(high, low, close), period)
    plus_di = 100.0 * rma(plus_dm, period) / atr_s.replace(0, np.nan)
    minus_di = 100.0 * rma(minus_dm, period) / atr_s.replace(0, np.nan)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return plus_di, minus_di, rma(dx, period)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's RMA."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    out = out.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return out.where(avg_gain.notna())


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal_period: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Moving Average Convergence Divergence."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal_period)
    return macd_line, signal_line, macd_line - signal_line


def bollinger_bands(
    series: pd.Series, period: int = 20, num_std: float = 2.0, ddof: int = 0
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands with population std (ddof=0, as Bollinger defines) and BandWidth."""
    middle = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std(ddof=ddof)
    upper = middle + (std * num_std)
    lower = middle - (std * num_std)
    bandwidth = (upper - lower) / middle
    return upper, middle, lower, bandwidth


def donchian(high: pd.Series, low: pd.Series, period: int = 20) -> tuple[pd.Series, pd.Series]:
    """Donchian channel strictly excluding the current bar t: max(H[t-period..t-1])."""
    upper = high.shift(1).rolling(window=period, min_periods=period).max()
    lower = low.shift(1).rolling(window=period, min_periods=period).min()
    return upper, lower


def rvol(volume: pd.Series, period: int = 20) -> pd.Series:
    """Relative Volume: volume[t] / mean(volume[t-period..t-1]). Strictly excludes the current bar."""
    baseline = volume.shift(1).rolling(window=period, min_periods=period).mean()
    return volume / baseline.replace(0, np.nan)


def efficiency_ratio(close: pd.Series, period: int = 10) -> pd.Series:
    """Kaufman Efficiency Ratio: |C_t - C_{t-n}| / sum(|ΔC|) over n bars. 1 = straight line, ~0 = choppy."""
    net = (close - close.shift(period)).abs()
    path = close.diff().abs().rolling(window=period, min_periods=period).sum()
    return (net / path.replace(0, np.nan)).clip(0.0, 1.0)


def kama(close: pd.Series, er_period: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """Kaufman Adaptive Moving Average (seeded with the close where ER first exists)."""
    er = efficiency_ratio(close, er_period).to_numpy()
    c = close.to_numpy(dtype=float)
    fsc, ssc = 2.0 / (fast + 1), 2.0 / (slow + 1)
    out = np.full(len(c), np.nan)
    prev = np.nan
    for i in range(len(c)):
        if np.isnan(er[i]):
            continue
        if np.isnan(prev):
            prev = c[i]
        else:
            sc = (er[i] * (fsc - ssc) + ssc) ** 2
            prev = prev + sc * (c[i] - prev)
        out[i] = prev
    return pd.Series(out, index=close.index)


def diamond_phase(close: pd.Series, sma50: pd.Series, sma200: pd.Series) -> pd.Series:
    """Método do Diamante (Lemos): 6 cyclic phases from close, SMA50 and SMA200 (NaN while warming up).

    1 Recuperação (C>S50, C<S200, S50<S200)   2 Acumulação (C>S50, C>S200, S50<S200)
    3 Altista     (C>S50, C>S200, S50>S200)   4 Aviso      (C<S50, C>S200, S50>S200)
    5 Distribuição(C<S50, C<S200, S50>S200)   6 Baixista   (C<S50, C<S200, S50<S200)
    (The two remaining sign combinations are impossible: S50<S200<C implies C>S50, etc.)
    """
    above50, above200, golden = close > sma50, close > sma200, sma50 > sma200
    phase = np.select(
        [above50 & ~above200 & ~golden, above50 & above200 & ~golden, above50 & above200 & golden,
         ~above50 & above200 & golden, ~above50 & ~above200 & golden, ~above50 & ~above200 & ~golden],
        [1, 2, 3, 4, 5, 6], default=0,
    ).astype(float)
    out = pd.Series(phase, index=close.index)
    out[sma200.isna() | sma50.isna() | (out == 0)] = np.nan
    return out


def gap_quantile(open_: pd.Series, close: pd.Series, q: float = 0.01, lookback: int = 500,
                 min_periods: int = 120) -> pd.Series:
    """Rolling q-quantile of the opening gap (open[t]/close[t-1]-1), causal. Used for the gap cap (1.2.1)."""
    gap = open_ / close.shift(1) - 1.0
    return gap.rolling(window=lookback, min_periods=min_periods).quantile(q)
