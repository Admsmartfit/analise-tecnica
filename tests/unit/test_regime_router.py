"""Regime router: Diamond phases, Kaufman ER, breadth, routing decisions and the regime laboratory."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from helpers import market

from ts.backtest.engine import BacktestEngine
from ts.core.calendar import B3Calendar
from ts.core.config import CostsConfig, RegimeRouterConfig, StrategyConfig
from ts.core.models import SetupId, Signal, TradeClass
from ts.indicators.ta import diamond_phase, efficiency_ratio, kama
from ts.strategy.router import RegimeContext, compute_breadth, route
from ts.strategy.signal_engine import prepare_ticker


def test_efficiency_ratio_and_kama() -> None:
    line = pd.Series(np.arange(30, dtype=float))
    assert efficiency_ratio(line, 10).iloc[-1] == pytest.approx(1.0)
    zigzag = pd.Series([10.0, 11.0] * 15)
    assert efficiency_ratio(zigzag, 10).iloc[-1] < 0.15
    k = kama(line, 10)
    assert np.isnan(k.iloc[9]) and k.iloc[-1] < line.iloc[-1]  # lags but follows a clean trend


def test_diamond_phases() -> None:
    c = pd.Series([10.0, 12.0, 12.0, 9.0, 7.0, 7.0])
    s50 = pd.Series([9.0, 11.0, 11.0, 10.0, 9.0, 8.0])
    s200 = pd.Series([11.0, 11.5, 10.0, 8.0, 8.0, 9.0])
    assert diamond_phase(c, s50, s200).tolist() == [1, 2, 3, 4, 5, 6]


def _sig(setup: SetupId) -> Signal:
    return Signal("x", "PETR4", setup, TradeClass.TREND, date(2024, 6, 27), "OPEN_AUCTION", 10, 9, None, {}, "h")


def test_route_rules() -> None:
    cfg = RegimeRouterConfig()
    trend = RegimeContext(3, 0.45, 0.6, 10)
    chop = RegimeContext(3, 0.10, 0.6, 10)
    assert route(_sig(SetupId.A_PIVOT), trend, cfg) is None
    assert "exige tendência" in route(_sig(SetupId.A_PIVOT), chop, cfg)
    assert route(_sig(SetupId.D_SQUEEZE), chop, cfg) is None  # squeeze born from low ER
    assert "fase Aviso" in route(_sig(SetupId.B_PULLBACK), RegimeContext(4, 0.4, 0.6, 0), cfg)
    cfg.breadth_min_pct_above_sma50 = 0.4
    assert "amplitude fraca" in route(_sig(SetupId.C_DONCHIAN), RegimeContext(3, 0.5, 0.2, 0), cfg)


def test_breadth_from_universe() -> None:
    bars, _, sessions = market(300)
    cfg = StrategyConfig()
    prepared = {t: prepare_ticker(t, df, cfg) for t, df in bars.items()}
    b = compute_breadth(prepared)
    last = b.loc[sessions[-1]]
    assert 0.0 <= last["pct_above_sma50"] <= 1.0 and last["adv"] + last["dec"] <= len(bars)
    assert np.isfinite(last["mcclellan"])


def test_router_tags_and_rejects_in_engine() -> None:
    bars, ibov, _ = market(480)
    cfg = StrategyConfig()
    cfg.min_history_bars = 220
    cfg.market_filter.require_bull_regime = False
    for s in ("A", "C", "D"):
        getattr(cfg.setups, s).rvol_min = 0.8
    cfg.setups.A.adx_threshold = cfg.setups.C.adx_threshold = 10
    off = BacktestEngine(cfg, CostsConfig(), 100_000, B3Calendar()).run(bars, ibov)
    assert off.signals and all("phase" in r.signal.features and "er" in r.signal.features for r in off.signals)
    cfg.regime_router.enabled = True
    cfg.regime_router.er_trend_min = 0.9  # almost nothing qualifies as trend -> A and C get routed out
    on = BacktestEngine(cfg, CostsConfig(), 100_000, B3Calendar()).run(bars, ibov)
    routed = [r for r in on.signals if r.disposition == "REJECTED_REGIME" and "Roteador" in (r.reason or "")]
    assert routed
    assert not [t for t in on.trades if t.setup in ("A", "C")] or len(on.trades) < len(off.trades)


def test_regime_report_runs() -> None:
    from ts.backtest.regime_report import regime_report

    bars, ibov, _ = market(520)
    cfg = StrategyConfig()
    cfg.min_history_bars = 220
    cfg.market_filter.require_bull_regime = False
    text, data = regime_report(bars, ibov, cfg, CostsConfig(), train_sessions=150, test_sessions=60, min_cell_trades=2)
    assert "Laboratório de regime" in text and ("Recomendação" in text or "Inconclusivo" in text)
    assert data["off"].total_trades >= data["on"].total_trades
