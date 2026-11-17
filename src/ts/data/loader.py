"""Load bars from the Parquet store with corporate-action adjustment (adj for signals, raw for orders)."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd

from ts.data.corporate_actions import CorporateActionsStore, to_signal_frame
from ts.data.ohlcv_store import OhlcvStore


def load_signal_bars(
    store: OhlcvStore,
    tickers: Iterable[str],
    corp: CorporateActionsStore | None = None,
    until: date | None = None,
) -> dict[str, pd.DataFrame]:
    """Return ``{ticker: frame}`` with adjusted OHLCV and ``raw_*`` columns, sorted, up to ``until``."""
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        if not store.exists(t):
            continue
        df = store.read(t)
        if df.empty:
            continue
        if until is not None:
            df = df[df["session_date"] <= until]
        df = corp.apply_adjustment_factors(t, df) if corp is not None else df.assign(adj_factor=1.0)
        out[t] = to_signal_frame(df).reset_index(drop=True)
    return out
