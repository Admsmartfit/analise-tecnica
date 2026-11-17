"""Per-ticker feature frame: every indicator the setups read, computed once and causally.

All columns are causal (value at t uses bars <= t), so computing them on the full history and reading
row ``t`` gives exactly what a live run on ``df[:t+1]`` would compute — this is what lets the backtest
precompute features once instead of recomputing every indicator for every day (verified by the prefix
test in ``tests/unit/test_prefix.py``).
"""
from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ts.core.config import StrategyConfig
from ts.indicators.ta import (
    adx,
    atr,
    bollinger_bands,
    donchian,
    ema,
    gap_quantile,
    macd,
    rsi,
    rvol,
    sma,
)

BASE_COLUMNS = ("session_date", "open", "high", "low", "close", "real_volume")


def compute_features(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Return a copy of ``df`` (sorted, positional index) with indicator columns added."""
    f = df.sort_values(by="session_date").reset_index(drop=True).copy()
    for c in ("open", "high", "low", "close"):
        f[c] = f[c].astype(float)
    f["real_volume"] = f["real_volume"].astype(float)
    if "adj_factor" not in f.columns:
        f["adj_factor"] = 1.0
    for c in ("open", "high", "low", "close"):
        if f"raw_{c}" not in f.columns:
            f[f"raw_{c}"] = f[c] / f["adj_factor"]

    c, h, lo, v = f["close"], f["high"], f["low"], f["real_volume"]
    sb, sc, sd, se = cfg.setups.B, cfg.setups.C, cfg.setups.D, cfg.setups.E

    f["sma_trend"] = sma(c, cfg.asset_trend_sma)
    f["sma_b"] = sma(c, sb.sma_medium)
    f["ema_b"] = ema(c, sb.ema_short)
    f["atr14"] = atr(h, lo, c, cfg.pivot.atr_period)
    f["atr22"] = atr(h, lo, c, cfg.setups.A.chandelier_atr_period)
    f["adx"] = adx(h, lo, c, 14)[2]
    f["rsi_b"] = rsi(c, sb.rsi_period)
    f["rsi_e"] = rsi(c, se.rsi_period)
    f["macd_hist"] = macd(c)[2]
    up, mid, _, bw = bollinger_bands(c, sd.bollinger_period, sd.bollinger_std, sd.bollinger_ddof)
    f["bb_upper"], f["bb_mid"], f["bb_bw"] = up, mid, bw
    f["donch_entry_up"] = donchian(h, lo, sc.donchian_entry_period)[0]
    f["donch_exit_low"] = donchian(h, lo, sc.donchian_exit_period)[1]
    f["rvol"] = rvol(v, 20)
    f["gap_p01"] = gap_quantile(f["raw_open"], f["raw_close"], 0.01, cfg.risk.gap_lookback_days)
    rs_n = cfg.portfolio.relative_strength_days
    f["roc_rs"] = c / c.shift(rs_n) - 1.0
    f["ret1"] = c.pct_change()
    return f


class FeatureView:
    """Read-only view of a feature frame positioned at bar ``k`` (the decision bar t)."""

    __slots__ = ("_cols", "k", "_dates")

    def __init__(self, cols: dict[str, np.ndarray], dates: np.ndarray, k: int) -> None:
        self._cols = cols
        self._dates = dates
        self.k = k

    @classmethod
    def from_frame(cls, feat: pd.DataFrame, k: int | None = None) -> FeatureView:
        cols = {name: feat[name].to_numpy() for name in feat.columns if name != "session_date"}
        dates = feat["session_date"].to_numpy()
        return cls(cols, dates, len(feat) - 1 if k is None else k)

    def at_k(self, k: int) -> FeatureView:
        return FeatureView(self._cols, self._dates, k)

    @property
    def n_bars(self) -> int:
        return self.k + 1

    @property
    def date(self) -> date:
        return self._dates[self.k]  # type: ignore[no-any-return]

    def date_at(self, idx: int) -> date:
        return self._dates[idx]  # type: ignore[no-any-return]

    def has(self, name: str) -> bool:
        return name in self._cols

    def v(self, name: str, offset: int = 0) -> float:
        """Value of ``name`` at ``t + offset`` (offset <= 0). NaN if out of range."""
        idx = self.k + offset
        if offset > 0:
            raise IndexError("FeatureView never reads the future")
        if idx < 0:
            return float("nan")
        return float(self._cols[name][idx])

    def at(self, name: str, idx: int) -> float:
        """Value at absolute position ``idx`` (must be <= t)."""
        if idx > self.k:
            raise IndexError("FeatureView never reads the future")
        return float(self._cols[name][idx])

    def window(self, name: str, start_offset: int, end_offset: int) -> np.ndarray:
        """Values at ``t+start_offset .. t+end_offset-1`` (end_offset <= 1)."""
        if end_offset > 1:
            raise IndexError("FeatureView never reads the future")
        lo = max(0, self.k + start_offset)
        return np.asarray(self._cols[name][lo : self.k + end_offset], dtype=float)


def ensure_view(data: Any, cfg: StrategyConfig) -> FeatureView:
    """Accept a raw bar frame, a feature frame or a FeatureView (for convenience in tests/live)."""
    if isinstance(data, FeatureView):
        return data
    if isinstance(data, pd.DataFrame):
        feat = data if "atr14" in data.columns else compute_features(data, cfg)
        return FeatureView.from_frame(feat)
    raise TypeError(f"Unsupported data type for features: {type(data)!r}")
