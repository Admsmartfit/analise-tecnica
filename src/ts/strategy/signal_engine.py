"""Signal engine: Camada 1 (filters) + Camada 2 (setups) with strict lookahead protection.

The same code runs in backtest, paper and live (PRD principle 1). Live/paper call :meth:`evaluate` with
bars up to ``asof``; the backtest prepares each ticker once (:func:`prepare_ticker`) and calls
:meth:`evaluate_prepared`, which positions a :class:`FeatureView` at ``asof`` and the pivot state as of
``asof`` — identical results because features and pivot replays are causal (prefix test).
"""
from __future__ import annotations

import bisect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from ts.core.config import StrategyConfig, load_strategy_config, strategy_config_hash
from ts.core.errors import LookaheadError
from ts.core.models import Signal
from ts.indicators.pivots import PivotHistory, detect_pivot_events
from ts.strategy.features import FeatureView, compute_features
from ts.strategy.regime import is_market_bearish, is_market_filter_bullish
from ts.strategy.setups import BEAR_EVALUATORS, SETUP_EVALUATORS

logger = logging.getLogger("ts.strategy.signal_engine")


@dataclass
class TickerData:
    ticker: str
    feat: pd.DataFrame
    view: FeatureView
    dates: list[date]
    pivots: PivotHistory

    def index_of(self, d: date) -> int | None:
        """Position of the bar of session ``d`` or None if the ticker has no bar that day."""
        k = bisect.bisect_right(self.dates, d) - 1
        if k < 0 or self.dates[k] != d:
            return None
        return k

    def last_index_upto(self, d: date) -> int:
        return bisect.bisect_right(self.dates, d) - 1


def prepare_ticker(ticker: str, df: pd.DataFrame, cfg: StrategyConfig) -> TickerData:
    feat = compute_features(df, cfg)
    events = detect_pivot_events(
        feat, ticker, cfg.pivot.window_n, cfg.pivot.magnitude_atr_k, cfg.pivot.atr_period
    )
    return TickerData(
        ticker=ticker,
        feat=feat,
        view=FeatureView.from_frame(feat),
        dates=list(feat["session_date"]),
        pivots=PivotHistory(events),
    )


def _check_lookahead(name: str, df: pd.DataFrame, asof: date) -> None:
    if df is not None and not df.empty:
        mx = df["session_date"].max()
        if mx > asof:
            raise LookaheadError(f"Lookahead violation in {name}: latest date {mx} > asof {asof}")


class SignalEngine:
    """Evaluates trading signals across the universe for a decision date."""

    def __init__(self, config: StrategyConfig | None = None, config_hash: str | None = None) -> None:
        if config is None:
            config, file_hash = load_strategy_config()
            config_hash = config_hash or file_hash
        self.config = config
        self.config_hash = config_hash or strategy_config_hash(config)

    # ------------------------------------------------------------------ live / paper
    def evaluate(self, asof: date, bars: Mapping[str, pd.DataFrame], index_bars: pd.DataFrame) -> list[Signal]:
        _check_lookahead("index_bars", index_bars, asof)
        for t, df in bars.items():
            _check_lookahead(t, df, asof)
        prepared = {
            t: prepare_ticker(t, df, self.config)
            for t, df in bars.items()
            if df is not None and not df.empty
        }
        return self.evaluate_prepared(asof, prepared, index_bars)

    # ------------------------------------------------------------------ backtest (cached)
    def evaluate_prepared(
        self,
        asof: date,
        prepared: Mapping[str, TickerData],
        index_bars: pd.DataFrame,
        market_ok: bool | None = None,
        market_bear: bool | None = None,
    ) -> list[Signal]:
        if market_ok is None:
            _check_lookahead("index_bars", index_bars, asof)
            market_ok = (not self.config.market_filter.require_bull_regime) or is_market_filter_bullish(
                index_bars, self.config.market_filter
            )
        if not market_ok:
            if not self.config.setups.F.enabled:
                logger.debug("Market filter (IBOV) not bullish on %s — no signals", asof)
                return []
            if market_bear is None:
                _check_lookahead("index_bars", index_bars, asof)
                market_bear = is_market_bearish(index_bars, self.config.market_filter)
            return self._evaluate_bear(asof, prepared) if market_bear else []

        signals: list[Signal] = []
        for ticker, td in prepared.items():
            k = td.index_of(asof)
            if k is None:
                # No closed bar for this session (suspended / stale data): fail safe, no decision.
                continue
            if k + 1 < self.config.min_history_bars:
                continue
            fv = td.view.at_k(k)
            c_t, trend = fv.v("close"), fv.v("sma_trend")
            if np.isnan(trend) or c_t <= trend:
                continue
            pivots = td.pivots.asof(asof)
            for evaluator in SETUP_EVALUATORS:
                sig = evaluator(ticker, asof, fv, pivots, self.config, self.config_hash)
                if sig is not None:
                    signals.append(sig)
        return signals

    def _evaluate_bear(self, asof: date, prepared: Mapping[str, TickerData]) -> list[Signal]:
        """Bear-market setups (F): the trend setups are blocked, and there is no asset-trend filter."""
        signals: list[Signal] = []
        for ticker, td in prepared.items():
            k = td.index_of(asof)
            if k is None or k + 1 < self.config.min_history_bars:
                continue
            fv = td.view.at_k(k)
            for evaluator in BEAR_EVALUATORS:
                sig = evaluator(ticker, asof, fv, (), self.config, self.config_hash)
                if sig is not None:
                    signals.append(sig)
        return signals
