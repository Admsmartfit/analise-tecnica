"""Synthetic market data on the real B3 calendar (shared by tests)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from ts.core.calendar import B3Calendar


def make_bars(
    sessions: list[date],
    seed: int,
    start_price: float = 30.0,
    drift: float = 0.0006,
    vol: float = 0.018,
) -> pd.DataFrame:
    """Random walk with intraday range, volume bursts and occasional gaps (deterministic per seed)."""
    rng = np.random.default_rng(seed)
    n = len(sessions)
    rets = rng.normal(drift, vol, n)
    # regime-ish trends so setups actually fire
    rets += 0.004 * np.sin(np.arange(n) / 37.0 + seed)
    close = start_price * np.exp(np.cumsum(rets))
    gap = rng.normal(0, vol / 3, n)
    open_ = np.r_[close[0], close[:-1] * np.exp(gap[1:])]
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0.006, 0.004, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0.006, 0.004, n)))
    volume = rng.integers(200_000, 600_000, n) * np.where(rng.random(n) < 0.12, 2.5, 1.0)
    return pd.DataFrame({
        "session_date": sessions,
        "ts_utc": [pd.Timestamp(d).tz_localize("UTC") for d in sessions],
        "open": np.round(open_, 2),
        "high": np.round(hi, 2),
        "low": np.round(lo, 2),
        "close": np.round(close, 2),
        "real_volume": volume.astype("int64"),
        "tick_volume": rng.integers(1000, 5000, n),
        "spread": np.ones(n, dtype="int32"),
        "adj_factor": 1.0,
        "source": "mt5:TEST",
        "ingested_at": datetime(2026, 1, 1, tzinfo=UTC),
    }).assign(
        high=lambda d: d[["open", "high", "close"]].max(axis=1),
        low=lambda d: d[["open", "low", "close"]].min(axis=1),
    )


def market(n_sessions: int = 420, tickers: tuple[str, ...] = ("AAAA3", "BBBB4", "CCCC3", "DDDD3"),
           end: date = date(2024, 6, 28)) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, list[date]]:
    cal = B3Calendar()
    sessions = cal.sessions_between(date(2019, 1, 2), end)[-n_sessions:]
    bars = {t: make_bars(sessions, seed=i + 1, start_price=20 + 5 * i) for i, t in enumerate(tickers)}
    ibov = make_bars(sessions, seed=99, start_price=100_000, drift=0.0008, vol=0.011)
    return bars, ibov, sessions
