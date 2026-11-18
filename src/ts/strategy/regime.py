"""Camada 1 market filters, volatility regime (PRD 1.2.4) and market-state classification (G0 regimes)."""
from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from ts.core.config import MarketFilterConfig, RegimeConfig, RegimeTier
from ts.indicators.ta import sma


def is_market_filter_bullish(ibov_df: pd.DataFrame, cfg: MarketFilterConfig | None = None) -> bool:
    """IBOV: Close > SMA200 and SMA50 > SMA200 (Dow primary tide)."""
    cfg = cfg or MarketFilterConfig()
    if ibov_df.empty or len(ibov_df) < cfg.ibov_sma_long:
        return False
    c = ibov_df["close"].astype(float)
    last_c = c.iloc[-1]
    s_long = sma(c, cfg.ibov_sma_long).iloc[-1]
    s_short = sma(c, cfg.ibov_sma_short).iloc[-1]
    if np.isnan(s_long) or np.isnan(s_short):
        return False
    return bool(last_c > s_long and s_short > s_long)


def is_market_bearish(ibov_df: pd.DataFrame, cfg: MarketFilterConfig | None = None) -> bool:
    """IBOV in a primary downtrend: Close < SMA200 and SMA50 < SMA200 (mirror of the bull filter)."""
    cfg = cfg or MarketFilterConfig()
    if ibov_df.empty or len(ibov_df) < cfg.ibov_sma_long:
        return False
    c = ibov_df["close"].astype(float)
    s_long = sma(c, cfg.ibov_sma_long).iloc[-1]
    s_short = sma(c, cfg.ibov_sma_short).iloc[-1]
    if np.isnan(s_long) or np.isnan(s_short):
        return False
    return bool(c.iloc[-1] < s_long and s_short < s_long)


def is_asset_primary_trend_bullish(asset_df: pd.DataFrame, period: int = 200) -> bool:
    """Asset primary trend: Close > SMA200."""
    if asset_df.empty or len(asset_df) < period:
        return False
    c = asset_df["close"].astype(float)
    s = sma(c, period).iloc[-1]
    return bool(not np.isnan(s) and c.iloc[-1] > s)


def realized_vol_percentile_series(ibov_df: pd.DataFrame, cfg: RegimeConfig) -> pd.Series:
    """Causal percentile (0..100) of the 20-day realized vol of the IBOV within a rolling 5-year window."""
    c = ibov_df["close"].astype(float).reset_index(drop=True)
    vol = np.log(c).diff().rolling(cfg.realized_vol_days, min_periods=cfg.realized_vol_days).std(ddof=0)
    window = 252 * cfg.lookback_years
    min_p = max(cfg.realized_vol_days * 3, 120)

    def pct_rank(x: np.ndarray) -> float:
        last = x[-1]
        valid = x[~np.isnan(x)]
        if np.isnan(last) or len(valid) == 0:
            return float("nan")
        return 100.0 * float(np.mean(valid <= last))

    pr = vol.rolling(window, min_periods=min_p).apply(pct_rank, raw=True)
    pr.index = ibov_df.index
    return pr


def regime_tier(percentile: float, cfg: RegimeConfig) -> RegimeTier:
    """Tier for a vol percentile. Unknown percentile (warm-up) -> most conservative non-blocking tier."""
    tiers = sorted(cfg.tiers, key=lambda t: t.max_percentile)
    if not tiers:
        return RegimeTier(max_percentile=100.0, risk_multiplier=1.0, heat_max_pct=1.0)
    if np.isnan(percentile):
        usable = [t for t in tiers if t.risk_multiplier > 0]
        return usable[-1] if usable else tiers[-1]
    for t in tiers:
        if percentile <= t.max_percentile:
            return t
    return tiers[-1]


MarketState = Literal["BULL", "SIDEWAYS", "BEAR"]


def market_state_series(ibov_df: pd.DataFrame, cfg: MarketFilterConfig | None = None) -> pd.Series:
    """BULL: C>SMA200 and SMA50>SMA200; BEAR: C<SMA200 and SMA50<SMA200; else SIDEWAYS (G0: 2 of 3)."""
    cfg = cfg or MarketFilterConfig()
    c = ibov_df["close"].astype(float)
    sl, ss = sma(c, cfg.ibov_sma_long), sma(c, cfg.ibov_sma_short)
    state = np.where((c > sl) & (ss > sl), "BULL", np.where((c < sl) & (ss < sl), "BEAR", "SIDEWAYS"))
    out = pd.Series(state, index=ibov_df.index)
    out[sl.isna()] = "SIDEWAYS"
    return out
