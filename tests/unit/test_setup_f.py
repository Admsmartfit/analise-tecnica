"""Setup F (bear-market IFR2 reversal): regime gating in the signal engine and its exit rule."""
from __future__ import annotations

from datetime import date

from helpers import market

from ts.core.config import StrategyConfig
from ts.core.models import ExitReason, TradeClass
from ts.risk.exits import PositionRecord, evaluate_position_exit, exit_params_for
from ts.strategy.signal_engine import SignalEngine, prepare_ticker


def _cfg(enabled: bool = True) -> StrategyConfig:
    cfg = StrategyConfig()
    cfg.min_history_bars = 120
    cfg.setups.F.enabled = enabled
    cfg.setups.F.rsi_max = 30.0  # synthetic data: make the trigger frequent enough to test the gating
    return cfg


def test_setup_f_only_in_bear_market_and_never_with_trend_setups() -> None:
    bars, ibov, sessions = market(320)
    cfg = _cfg()
    prepared = {t: prepare_ticker(t, df, cfg) for t, df in bars.items()}
    eng = SignalEngine(cfg, "h")
    bear_hits = 0
    for d in sessions[-150:]:
        bear = eng.evaluate_prepared(d, prepared, ibov, market_ok=False, market_bear=True)
        assert all(s.setup.value == "F" for s in bear)
        bear_hits += len(bear)
        assert eng.evaluate_prepared(d, prepared, ibov, market_ok=False, market_bear=False) == []  # lateral
        bull = eng.evaluate_prepared(d, prepared, ibov, market_ok=True, market_bear=False)
        assert all(s.setup.value != "F" for s in bull)
    assert bear_hits > 0
    for s in bear if bear else []:
        assert s.entry_type == "OPEN_AUCTION" and s.stop_price < s.entry_ref and s.target_price is None


def test_setup_f_disabled_keeps_old_behaviour() -> None:
    bars, ibov, sessions = market(320)
    cfg = _cfg(enabled=False)
    prepared = {t: prepare_ticker(t, df, cfg) for t, df in bars.items()}
    eng = SignalEngine(cfg, "h")
    assert all(eng.evaluate_prepared(d, prepared, ibov, market_ok=False, market_bear=True) == [] for d in sessions[-60:])


def test_suspended_setup_resumes_after_positive_probation() -> None:
    from ts.risk.protections import SetupStatisticalMonitor

    m = SetupStatisticalMonitor(window=10, resume_trades=5)
    for _ in range(10):
        m.record_trade("F", -1.0)
    assert m.is_suspended("F")
    for r in (-1.0, 0.5, 0.5, 0.5):
        assert not m.record_probation("F", r)          # < 5 shadow trades, or mean not positive yet
    assert m.record_probation("F", 0.5)                # last 5: mean +0.2 -> resumed
    assert not m.is_suspended("F") and m.trades_r_by_setup["F"] == []  # fresh live window
    m0 = SetupStatisticalMonitor(window=10, resume_trades=0)  # 0 = manual review only (old behaviour)
    for _ in range(10):
        m0.record_trade("F", -1.0)
    assert not any(m0.record_probation("F", 2.0) for _ in range(50)) and m0.is_suspended("F")


def test_probation_book_survives_serialization() -> None:
    import json

    from ts.execution.session_engine import BookState, PendingOrder

    cfg = StrategyConfig()
    st = BookState.new(100_000, cfg)
    st.monitor.suspended_setups["F"] = "teste"
    st.monitor.probation_r["F"] = [0.3, -0.2]
    st.probation["F:PETR4"] = PositionRecord("PBF:PETR4", "PETR4", "F", TradeClass.MEAN_REVERSION, date(2024, 1, 2),
                                             30.0, 28.0, 28.0, None, 1, 1, 2.0)
    st.probation_pending.append(PendingOrder("PBF:VALE3", None, "VALE3", "BUY", "OPEN_AUCTION", 1, 60.0, 61.8, 57.0,
                                             None, "F", "MEAN_REVERSION", date(2024, 1, 3), reason="probation"))
    back = BookState.from_dict(json.loads(json.dumps(st.to_dict())), cfg)
    assert back.monitor.probation_r == {"F": [0.3, -0.2]} and back.monitor.resume_trades == cfg.protections.setup_resume_trades
    assert back.probation["F:PETR4"].entry_price == 30.0 and back.probation_pending[0].ticker == "VALE3"


def test_monthly_rsi_uses_only_completed_months() -> None:
    """Setup G's monthly RSI on any day of month m must come from months <= m-1 (no look-ahead)."""
    import pandas as pd

    from ts.strategy.features import compute_features

    bars, _, sessions = market(320)
    df = next(iter(bars.values()))
    cfg = StrategyConfig()
    full = compute_features(df, cfg).set_index("session_date")["rsi_month"]
    for d in sessions[-80::7]:
        part = compute_features(df[df["session_date"] <= d], cfg).set_index("session_date")["rsi_month"]
        assert (pd.isna(part.loc[d]) and pd.isna(full.loc[d])) or abs(part.loc[d] - full.loc[d]) < 1e-9
    days = pd.Series(full.index)
    same_month = days.map(lambda x: (x.year, x.month))
    # constant inside each calendar month (it only changes when a month closes)
    assert full.groupby(same_month.values).nunique(dropna=False).max() == 1


def test_setup_f_exit_when_close_above_short_sma() -> None:
    cfg = _cfg()
    params = exit_params_for("F", cfg)
    pos = PositionRecord("p", "X", "F", TradeClass.MEAN_REVERSION, date(2024, 1, 2), 10.0, 9.0, 9.0, None, 100, 100, 1.0)
    d = date(2024, 1, 3)
    assert not evaluate_position_exit(pos, d, 10.0, 10.2, 9.8, 10.1, params=params, sma_exit=10.3).should_exit
    dec = evaluate_position_exit(pos, d, 10.1, 10.6, 10.0, 10.5, params=params, sma_exit=10.3)
    assert dec.should_exit and dec.at_next_open and dec.exit_reason == ExitReason.TARGET
    pos2 = PositionRecord("q", "X", "F", TradeClass.MEAN_REVERSION, date(2024, 1, 2), 10.0, 9.0, 9.0, None, 100, 100, 1.0)
    for _ in range(cfg.setups.F.time_stop_bars - 1):
        assert not evaluate_position_exit(pos2, d, 10.0, 10.1, 9.9, 10.0, params=params, sma_exit=10.5).should_exit
    assert evaluate_position_exit(pos2, d, 10.0, 10.1, 9.9, 10.0, params=params, sma_exit=10.5).exit_reason == ExitReason.TIME
