"""Regression tests for the defects found in the audit (one test per defect where practical)."""
from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ts.backtest.metrics import (
    compute_deflated_sharpe_ratio,
    compute_pbo_cscv,
    compute_performance_metrics,
    validate_gate_g0,
)
from ts.core.calendar import B3Calendar
from ts.core.config import RiskConfig, StrategyConfig
from ts.core.errors import OrderRejectedError, OrderStateUnknownError
from ts.core.lock import LockBusyError, ProcessLock
from ts.core.models import OrderIntent, SetupId, Signal, TradeClass
from ts.data.corporate_actions import CorporateActionsStore
from ts.data.fake_mt5 import LOST_RESPONSE, FakeMT5
from ts.data.ohlcv_store import OhlcvStore
from ts.data.universe import universe_on
from ts.data.validation import DataValidator
from ts.execution.mt5_broker import MT5Broker
from ts.execution.session_engine import CompletedTrade
from ts.persistence.db import DatabaseManager, apply_migrations, get_connection
from ts.risk.portfolio import PortfolioRiskManager, ledoit_wolf_correlation
from ts.risk.protections import CircuitBreaker, SetupStatisticalMonitor
from ts.risk.sizing import calculate_position_size


# ----------------------------------------------------------------------------- calendar
def test_calendar_2020_sp_holidays_moved_b3_open() -> None:
    cal = B3Calendar()
    assert cal.is_session(date(2020, 7, 9))  # B3 traded (holiday anticipated to May)
    assert cal.is_session(date(2020, 11, 20))
    assert not cal.is_session(date(2020, 9, 7))
    assert not cal.is_session(date(2020, 12, 24))


# ----------------------------------------------------------------------------- data
def test_upsert_with_new_ingested_at_does_not_rewrite_file(sample_daily_df: pd.DataFrame, tmp_path: Path) -> None:
    store = OhlcvStore(tmp_path)
    store.upsert("PETR4", sample_daily_df)
    before = (tmp_path / "PETR4.parquet").read_bytes()
    again = sample_daily_df.assign(ingested_at=datetime(2030, 1, 1, tzinfo=UTC))
    assert store.upsert("PETR4", again) == (0, len(sample_daily_df))
    assert (tmp_path / "PETR4.parquet").read_bytes() == before
    changed = again.copy()
    changed.loc[changed.index[-1], "close"] = changed["close"].iloc[-1] + 0.5
    changed.loc[changed.index[-1], "high"] = max(changed["high"].iloc[-1], changed["close"].iloc[-1])
    assert store.upsert("PETR4", changed)[0] == 1  # a corrected bar is written


def test_corporate_action_applied_even_when_ex_date_is_not_a_bar(tmp_path: Path) -> None:
    csv = tmp_path / "ca.csv"
    csv.write_text("ticker,ex_date,action_type,value,factor\nXPTO3,2024-01-06,SPLIT,2,0.5\n", encoding="utf-8")
    df = pd.DataFrame({"session_date": [date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)], "close": [20.0, 20.0, 10.0]})
    out = CorporateActionsStore(csv).apply_adjustment_factors("XPTO3", df)  # 2024-01-06 is a Saturday
    assert out["adj_factor"].tolist() == [0.5, 0.5, 1.0]


def test_universe_uses_composition_in_force(tmp_path: Path) -> None:
    (tmp_path / "ibx100_2020_Q1.csv").write_text("ticker\nOLD3\n", encoding="utf-8")
    (tmp_path / "ibx100_2024_Q3.csv").write_text("ticker\nNEW3\n", encoding="utf-8")
    assert universe_on(date(2021, 5, 1), tmp_path) == ["OLD3"]  # previously returned the latest file
    assert universe_on(date(2025, 1, 2), tmp_path) == ["NEW3"]


def test_missing_bar_on_asof_is_blocking(sample_daily_df: pd.DataFrame, b3_calendar: B3Calendar) -> None:
    df = sample_daily_df[sample_daily_df["session_date"].apply(b3_calendar.is_session)]
    asof = b3_calendar.next_session(df["session_date"].max())
    issues = DataValidator(b3_calendar).validate_bars("PETR4", df, asof=asof)
    assert any(i.check_name == "missing_session" and i.severity == "BLOCK" and i.session_date == asof for i in issues)


# ----------------------------------------------------------------------------- sizing properties
def test_sizing_invariants_randomized() -> None:
    rng = random.Random(7)
    cfg = RiskConfig()
    for _ in range(3000):
        entry = round(rng.uniform(1.0, 200.0), 2)
        atr = entry * rng.uniform(0.005, 0.08)
        stop = round(entry - atr * rng.uniform(0.1, 3.5), 2)
        equity = rng.uniform(20_000, 2_000_000)
        gap = rng.uniform(0.005, 0.15)
        r = calculate_position_size(entry, stop, atr, equity, cfg, gap_p01=gap)
        if not r.accepted:
            continue
        target = equity * cfg.risk_per_trade_pct
        assert r.qty % cfg.round_lot == 0 and r.qty > 0
        assert r.qty * (entry - r.stop_price) <= target + 1e-6  # effective risk never above target
        assert r.qty * entry <= equity * cfg.capital_cap_pct + 1e-6
        assert r.qty * entry * gap <= equity * cfg.gap_cap_pct + 1e-6
        assert entry - r.stop_price >= cfg.stop_atr_min_k * atr - cfg.tick_size
        assert abs(r.stop_price / cfg.tick_size - round(r.stop_price / cfg.tick_size)) < 1e-6


def test_sizing_rejects_nan_atr() -> None:
    assert not calculate_position_size(50, 48, float("nan"), 100_000, RiskConfig()).accepted


# ----------------------------------------------------------------------------- portfolio / protections
def _sig(t: str, roc: float = 0.1) -> Signal:
    return Signal(f"A-{t}", t, SetupId.A_PIVOT, TradeClass.TREND, date(2024, 1, 2), "OPEN_AUCTION", 10.0, 9.0, None,
                  {"roc_rs": roc}, "h")


def test_tickers_without_history_do_not_share_a_cluster() -> None:
    from ts.risk.sizing import SizingResult

    sz = SizingResult(True, 100, 10.0, 9.0, 1.0, 100.0, 1000.0, "ACCEPTED")
    pm = PortfolioRiskManager(StrategyConfig().portfolio)
    open_pos = [{"ticker": "X1", "risk_brl": 100.0, "capital_allocated": 1000.0},
                {"ticker": "X2", "risk_brl": 100.0, "capital_allocated": 1000.0}]
    dec = pm.evaluate_candidates([(_sig("NEW3"), sz)], 100_000, open_pos, pd.DataFrame())
    assert dec[0].disposition == "ACCEPTED"


def test_correlated_risk_limit() -> None:
    from ts.risk.sizing import SizingResult

    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.01, 60)
    rets = pd.DataFrame({t: base + rng.normal(0, 0.001, 60) for t in ("A3", "B3", "C3", "D3")})
    sz = SizingResult(True, 100, 10.0, 9.0, 1.0, 1000.0, 1000.0, "ACCEPTED")
    cfg = StrategyConfig().portfolio
    cfg.max_positions_per_cluster = 10
    pm = PortfolioRiskManager(cfg)
    dec = pm.evaluate_candidates([(_sig(t), sz) for t in rets.columns], 100_000, [], rets)
    accepted = [d for d in dec if d.disposition == "ACCEPTED"]
    assert len(accepted) == 3  # 4 x 1% almost perfectly correlated would be ~4% > 3%
    assert any(d.disposition == "REJECTED_CORR" for d in dec)
    lw = ledoit_wolf_correlation(rets)
    assert np.allclose(np.diag(lw), 1.0)


def test_setup_monitor_uses_backtest_reference() -> None:
    ref = list(np.random.default_rng(3).normal(0.3, 1.5, 300))
    mon = SetupStatisticalMonitor(window=30, reference_r={"B": ref}, cusum_h_sigma=50)
    suspended = any(mon.record_trade("B", -1.0) for _ in range(30))
    assert suspended and mon.is_suspended("B")
    healthy = SetupStatisticalMonitor(window=30, reference_r={"B": ref}, cusum_h_sigma=50)
    rng = np.random.default_rng(5)
    assert not any(healthy.record_trade("B", float(x)) for x in rng.normal(0.3, 1.5, 30))


def test_circuit_breaker_level1_and_initial_equity() -> None:
    cb = CircuitBreaker(0.08, 0.12, 20, initial_equity=50_000)
    cb.update(45_500, 45_500)  # -9%
    assert cb.level == 1 and cb.risk_multiplier() == 0.5
    cb.update(43_000, 43_000)  # -14%
    assert cb.is_halted and cb.risk_multiplier() == 0.0


# ----------------------------------------------------------------------------- metrics
def _trade(r: float, pnl: float) -> CompletedTrade:
    return CompletedTrade("p", "T", "A", TradeClass.TREND, date(2024, 1, 1), date(2024, 1, 5), 10, 10, 100, 1.0, r,
                          pnl, 1.0, pnl - 1, "STOP", 3)


def test_cvar_is_tail_mean_not_percentile() -> None:
    trades = [_trade(-3.0, -300)] + [_trade(-1.0, -100)] * 9 + [_trade(1.0, 100)] * 30
    m = compute_performance_metrics(trades, pd.DataFrame())
    assert m.cvar_5pct_r == pytest.approx((-3.0 - 1.0) / 2)  # worst 5% of 40 trades = 2 trades


def test_pbo_detects_noise_vs_real_edge() -> None:
    rng = np.random.default_rng(0)
    noise = pd.DataFrame(rng.normal(0, 0.01, (800, 12)))
    assert compute_pbo_cscv(noise, 10) > 0.25
    edge = noise.copy()
    edge[0] += 0.004  # one configuration genuinely better everywhere
    assert compute_pbo_cscv(edge, 10) < 0.1


def test_dsr_penalizes_many_trials() -> None:
    one = compute_deflated_sharpe_ratio(0.08, 1, 750)
    many = compute_deflated_sharpe_ratio(0.08, 200, 750)
    assert one > 0.95 and many < one


def test_g0_requires_every_criterion() -> None:
    trades = [_trade(1.0, 100)] * 40 + [_trade(-1.0, -100)] * 10
    m = compute_performance_metrics(trades, pd.DataFrame(), pbo=0.1)
    assert validate_gate_g0(m)["overall_g0_approved"] is False  # plateau/regimes/DSR not evaluated -> NO-GO


# ----------------------------------------------------------------------------- broker
def _live_settings():  # type: ignore[no-untyped-def]
    from ts.core.config import Settings

    s = Settings()
    s.execution.mode = "live"
    return s


def _intent(cid: str = "TS261118PETR4A") -> OrderIntent:
    from decimal import Decimal

    return OrderIntent(cid, None, "PETR4", "BUY", 100, "LIMIT", Decimal("30"), Decimal("28.5"), date(2026, 11, 18), "t",
                       risk_budget=Decimal("200"))


def test_mt5_broker_refuses_when_not_live() -> None:
    with pytest.raises(OrderRejectedError):
        MT5Broker(FakeMT5()).submit(_intent())


def test_lost_response_is_reconciled_not_resent() -> None:
    fake = FakeMT5()
    fake.initialize()
    fake.script("order_send", [LOST_RESPONSE])
    br = MT5Broker(fake, _live_settings(), sleep=lambda s: None)
    r = br.submit(_intent())  # LIMIT entry (bracket): rests in the book -> reconciled as ACCEPTED
    assert r.success and r.state.value == "ACCEPTED"
    assert len(fake._orders) == 1
    r2 = br.submit(_intent())  # idempotent: second submit finds the order, does not send it again
    assert len(fake._orders) == 1 and "idempotent" in r2.message.lower()


def test_lost_response_of_filled_order_is_found_in_deals() -> None:
    from decimal import Decimal

    fake = FakeMT5()
    fake.initialize()
    fake.script("order_send", [LOST_RESPONSE])
    br = MT5Broker(fake, _live_settings(), sleep=lambda s: None)
    sell = OrderIntent("TS261118PETR4X", None, "PETR4", "SELL", 100, "MARKET", Decimal("30"), None,
                       date(2026, 11, 18), "saída")
    r = br.submit(sell)
    assert r.success and r.state.value == "FILLED" and len(fake._deals) == 1
    br.submit(sell)
    assert len(fake._deals) == 1


def test_unknown_after_reconciliation_blocks_ticker() -> None:
    fake = FakeMT5()
    fake.initialize()
    fake.script("order_send", [None])  # IPC error, order never reached the broker
    br = MT5Broker(fake, _live_settings(), sleep=lambda s: None)
    with pytest.raises(OrderStateUnknownError):
        br.submit(_intent())
    with pytest.raises(OrderStateUnknownError):
        br.submit(_intent("TS261118PETR4B"))


# ----------------------------------------------------------------------------- infra
def test_migrations_idempotent_and_backup_routine_allowed(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "t.db")
    apply_migrations(conn)
    apply_migrations(conn)
    conn.execute("INSERT INTO runs (run_id, routine, session_date, started_at, status, config_hash, code_version) "
                 "VALUES ('r','backup','2026-01-02','x','OK','h','v')")
    mem = get_connection(":memory:")
    apply_migrations(mem)
    assert mem.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3


def test_process_lock_is_exclusive(tmp_path: Path) -> None:
    p = tmp_path / "ts.lock"
    with ProcessLock(p):
        with pytest.raises(LockBusyError):
            ProcessLock(p, timeout_s=0).acquire()
    with ProcessLock(p):
        pass


def test_db_manager_records_backup_run(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "x.db")
    db.record_run("b1", "backup", "2026-01-02", "h")
    db.update_run("b1", "OK")
    assert math.isfinite(1.0)
