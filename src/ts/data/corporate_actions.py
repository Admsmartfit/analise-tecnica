"""Corporate actions and backward cumulative adjustment factors (PRD 1.1.5).

CSV format (``config/corporate_actions.csv``)::

    ticker,ex_date,action_type,value,factor
    PETR4,2024-04-22,DIVIDEND,0.54,0.9862
    MGLU3,2024-05-02,REVERSE_SPLIT,10,10.0

``factor`` multiplies every price *before* ``ex_date`` (1.0 from ``ex_date`` on). For a cash dividend
``factor = 1 - value / close_before_ex``; for a 2:1 split ``factor = 0.5``; for a 10:1 reverse split
``factor = 10``. The factor is computed in memory at read time, so a new event never requires rewriting
the Parquet history.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

logger = logging.getLogger("ts.data.corporate_actions")

CSV_COLUMNS = ["ticker", "ex_date", "action_type", "value", "factor"]


@dataclass(frozen=True, slots=True)
class CorporateAction:
    ticker: str
    ex_date: date
    action_type: Literal["DIVIDEND", "JCP", "SPLIT", "REVERSE_SPLIT", "BONUS"]
    value: float
    factor: float


class CorporateActionsStore:
    """Loads corporate actions and computes backward cumulative adjustment factors."""

    def __init__(self, file_path: str | Path = "config/corporate_actions.csv") -> None:
        self.file_path = Path(file_path)
        self._actions: dict[str, list[CorporateAction]] = {}
        self._load()

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        df = pd.read_csv(self.file_path, comment="#")
        if df.empty:
            return
        for _, row in df.iterrows():
            factor = float(row["factor"])
            if not np.isfinite(factor) or factor <= 0:
                raise ValueError(f"Invalid adjustment factor {factor} for {row['ticker']} on {row['ex_date']}")
            action = CorporateAction(
                ticker=str(row["ticker"]).upper(),
                ex_date=pd.to_datetime(row["ex_date"]).date(),
                action_type=str(row["action_type"]).upper(),  # type: ignore[arg-type]
                value=float(row["value"]),
                factor=factor,
            )
            self._actions.setdefault(action.ticker, []).append(action)
        for lst in self._actions.values():
            lst.sort(key=lambda a: a.ex_date)

    def actions_for(self, ticker: str) -> list[CorporateAction]:
        return list(self._actions.get(ticker.upper(), []))

    def apply_adjustment_factors(self, ticker: str, df: pd.DataFrame) -> pd.DataFrame:
        """Attach cumulative ``adj_factor`` (1.0 on the latest bar, compounding backward).

        Every bar with ``session_date < ex_date`` is multiplied by the action factor. This works even
        when the ex-date is not itself a bar in ``df`` (holiday, suspended day, gap in history).
        """
        if df.empty:
            return df
        res = df.sort_values(by="session_date").reset_index(drop=True).copy()
        factors = np.ones(len(res))
        dates = pd.to_datetime(res["session_date"]).dt.date.to_numpy()
        for action in self._actions.get(ticker.upper(), []):
            factors[dates < action.ex_date] *= action.factor
        res["adj_factor"] = factors
        return res


def to_signal_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy whose OHLC/volume are the *adjusted* series used by indicators and signals.

    The raw prices stay available as ``raw_open``/``raw_high``/``raw_low``/``raw_close`` (used for
    order prices) and ``adj_factor`` is kept so a signal price can be mapped back: raw = adj / factor.
    """
    out = df.copy()
    f = out["adj_factor"].astype(float).fillna(1.0) if "adj_factor" in out.columns else pd.Series(1.0, index=out.index)
    out["adj_factor"] = f
    for c in ("open", "high", "low", "close"):
        out[f"raw_{c}"] = out[c].astype(float)
        out[c] = out[c].astype(float) * f
    # Splits change the share count: adjusted volume = raw volume / factor keeps RVol continuous.
    out["real_volume"] = out["real_volume"].astype(float) / f
    return out
