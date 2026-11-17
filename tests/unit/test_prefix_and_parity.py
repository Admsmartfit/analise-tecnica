"""PRD principle 2 (no lookahead, prefix test) and principle 1 (backtest = paper parity)."""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
from helpers import market

from ts.backtest.engine import BacktestEngine
from ts.core.calendar import B3Calendar
from ts.core.config import CostsConfig, StrategyConfig, strategy_config_hash
from ts.indicators.pivots import PivotTracker, detect_pivot_events, detect_pivots, replay
from ts.indicators.ta import atr, ema, rma, rsi
from ts.strategy.features import compute_features
from ts.strategy.signal_engine import SignalEngine, prepare_ticker


def loose_cfg() -> StrategyConfig:
    """Thresholds relaxed so synthetic data produces signals of every kind."""
    cfg = StrategyConfig()
    cfg.market_filter.require_bull_regime = False
    for s in ("A", "C", "D"):
        getattr(cfg.setups, s).rvol_min = 0.8
    cfg.setups.A.adx_threshold = 10
    cfg.setups.C.adx_threshold = 10
    cfg.setups.B.rsi_max = 70
    cfg.setups.B.min_rr_filter = 0
    cfg.setups.E.min_rr_filter = 0
    cfg.setups.D.squeeze_percentile_q = 30
    cfg.regime.tiers = []
    cfg.portfolio.max_correlated_risk_pct = 0.05
    return cfg


# ----------------------------------------------------------------------------- indicators reference
def test_ema_and_rma_are_sma_seeded() -> None:
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    e = ema(s, 3)
    assert np.isnan(e.iloc[1]) and e.iloc[2] == 2.0  # seed = SMA(1,2,3)
    assert abs(e.iloc[3] - (2.0 + 0.5 * (4.0 - 2.0))) < 1e-12
    r = rma(s, 3)
    assert abs(r.iloc[3] - (2.0 + (4.0 - 2.0) / 3)) < 1e-12


def test_atr_seed_is_mean_of_first_true_ranges() -> None:
    h = pd.Series([10.0, 11.0, 12.0, 11.5, 12.5])
    lo = pd.Series([9.0, 10.0, 10.5, 10.0, 11.0])
    c = pd.Series([9.5, 10.5, 11.5, 10.5, 12.0])
    a = atr(h, lo, c, 3)
    tr = [max(11 - 10, abs(11 - 9.5), abs(10 - 9.5)), max(12 - 10.5, abs(12 - 10.5), abs(10.5 - 10.5)),
          max(11.5 - 10, abs(11.5 - 11.5), abs(10 - 11.5))]
    assert np.isnan(a.iloc[2]) and abs(a.iloc[3] - np.mean(tr)) < 1e-12


def test_rsi_prefix_invariance() -> None:
    bars, _, _ = market(300)
    c = bars["AAAA3"]["close"]
    full = rsi(c, 14)
    for t in (50, 120, 299):
        assert np.allclose(rsi(c.iloc[: t + 1], 14).to_numpy(), full.iloc[: t + 1].to_numpy(), equal_nan=True)


def test_features_prefix_invariance() -> None:
    bars, _, _ = market(330)
    cfg = StrategyConfig()
    df = bars["BBBB4"]
    full = compute_features(df, cfg)
    for t in (200, 280, 329):
        part = compute_features(df.iloc[: t + 1], cfg)
        num = [c for c in part.columns if part[c].dtype.kind == "f"]
        np.testing.assert_allclose(part[num].to_numpy(), full[num].iloc[: t + 1].to_numpy(), rtol=0, atol=1e-8)


# ----------------------------------------------------------------------------- pivots
def test_pivot_replacement_is_not_leaked_to_the_past() -> None:
    """A valley later replaced by a lower one must still be visible at the dates it was the valid pivot."""
    lows = [10, 9, 8, 9, 10, 12, 14, 12, 11, 10, 9, 7, 8, 9, 10, 13]
    highs = [x + 1 for x in lows]
    tr = PivotTracker("X", window_n=2, magnitude_atr_k=100.0, atr_period=2)  # magnitude blocks the peaks
    d0 = date(2024, 1, 1)
    days = [date.fromordinal(d0.toordinal() + i) for i in range(len(lows))]
    for d, h, lo in zip(days, highs, lows, strict=True):
        tr.update(d, (h + lo) / 2, h, lo, (h + lo) / 2)
    final = tr.confirmed_pivots
    assert len(final) == 1 and final[0].price == 7  # the 8 was replaced by the lower 7
    first_valley_confirm = days[4]
    # old (buggy) approach: filter the final state by confirmation date -> sees nothing on day 4..12
    assert [p for p in final if p.confirmed_date <= days[8]] == []
    # correct: replay the event log -> the valley at 8 existed on day 8
    asof = tr.get_confirmed_pivots_asof(days[8])
    assert len(asof) == 1 and asof[0].price == 8 and asof[0].confirmed_date == first_valley_confirm


def test_pivot_prefix_test_with_replay() -> None:
    bars, _, sessions = market(400)
    df = bars["CCCC3"]
    events = detect_pivot_events(df, "CCCC3", 2, 1.0, 14)
    for t in range(60, len(df), 13):
        prefix_state = detect_pivots(df.iloc[: t + 1], "CCCC3", 2, 1.0, 14)
        assert prefix_state == replay(events, sessions[t]), f"lookahead at {sessions[t]}"


# ----------------------------------------------------------------------------- signals
def test_signal_prefix_test_live_vs_prepared() -> None:
    """evaluate(df[:t]) (live) must equal evaluate_prepared at t on the full history (backtest)."""
    bars, ibov, sessions = market(400)
    cfg = loose_cfg()
    cfg.min_history_bars = 220
    eng = SignalEngine(cfg, "h")
    prepared = {t: prepare_ticker(t, df, cfg) for t, df in bars.items()}
    seen = 0
    for t in range(300, 400, 3):
        d = sessions[t]
        live = eng.evaluate(d, {k: v.iloc[: t + 1] for k, v in bars.items()}, ibov.iloc[: t + 1])
        bt = eng.evaluate_prepared(d, prepared, ibov.iloc[: t + 1])
        assert [(s.signal_id, round(s.entry_ref, 6), round(s.stop_price, 6)) for s in live] == \
               [(s.signal_id, round(s.entry_ref, 6), round(s.stop_price, 6)) for s in bt]
        seen += len(live)
    assert seen > 0, "synthetic data should produce some signals"


def test_signal_ids_are_deterministic() -> None:
    bars, ibov, sessions = market(400)
    cfg = loose_cfg()
    cfg.min_history_bars = 220
    a = SignalEngine(cfg, "abc123").evaluate(sessions[-1], bars, ibov)
    b = SignalEngine(cfg, "abc123").evaluate(sessions[-1], bars, ibov)
    assert [s.signal_id for s in a] == [s.signal_id for s in b]


# ----------------------------------------------------------------------------- backtest / paper parity
def _costs() -> CostsConfig:
    return CostsConfig()


def test_backtest_is_reproducible_and_consistent() -> None:
    bars, ibov, _ = market(520)
    cfg = loose_cfg()
    cfg.min_history_bars = 220
    r1 = BacktestEngine(cfg, _costs(), 100_000, B3Calendar()).run(bars, ibov)
    r2 = BacktestEngine(cfg, _costs(), 100_000, B3Calendar()).run(bars, ibov)
    assert r1.config_hash == strategy_config_hash(cfg)
    assert [(t.position_id, t.net_pnl_brl) for t in r1.trades] == [(t.position_id, t.net_pnl_brl) for t in r2.trades]
    assert len(r1.trades) > 0
    # one CompletedTrade per position (partials aggregated), R computed on the real fill
    assert len({t.position_id for t in r1.trades}) == len(r1.trades)
    for t in r1.trades:
        assert abs(t.r_multiple - t.net_pnl_brl / (t.risk_r * t.qty)) < 1e-9
        assert t.mae_r >= -1e-9 and t.mfe_r >= -1e-9
    # cash accounting: final equity = initial + realized net + open MTM
    assert abs(r1.total_fees - sum(t.fees_brl for t in r1.trades)) < 0.05


def test_paper_step_by_step_with_serialized_state_equals_backtest() -> None:
    """Paper runs one session per day and persists the book as JSON; it must match the backtest exactly."""
    from ts.execution.session_engine import BookState, SessionEngine
    from ts.indicators.ta import sma

    bars, ibov, sessions = market(480)
    cfg = loose_cfg()
    cfg.min_history_bars = 220
    cal = B3Calendar()
    bt = BacktestEngine(cfg, _costs(), 100_000, cal).run(bars, ibov)

    h = strategy_config_hash(cfg)
    eng = SessionEngine(cfg, _costs(), h, cal, order_prefix="BT")
    prepared = {t: prepare_ticker(t, df, cfg) for t, df in bars.items()}
    state = BookState.new(100_000, cfg, cal)
    c = ibov["close"]
    ok = (c > sma(c, 200)) & (sma(c, 50) > sma(c, 200))
    idx_ret = pd.Series(c.pct_change().to_numpy(), index=list(ibov["session_date"]))
    trades = []
    for i, d in enumerate(sessions):
        res = eng.run_session(state, d, prepared, market_ok=True if not cfg.market_filter.require_bull_regime else bool(ok.iloc[i]),
                              index_returns=idx_ret, plan=i < len(sessions) - 1)
        trades += [t for t in res.closed_trades if not t.is_shadow]
        state = BookState.from_dict(json.loads(json.dumps(state.to_dict())), cfg, cal)  # persist + reload
    assert [(t.position_id, round(t.net_pnl_brl, 6)) for t in trades] == \
           [(t.position_id, round(t.net_pnl_brl, 6)) for t in bt.trades]
