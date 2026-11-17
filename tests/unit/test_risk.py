"""Unit tests for risk management, position sizing, exits, and portfolio protections."""
from __future__ import annotations

from datetime import date

from ts.core.config import RiskConfig
from ts.core.models import ExitReason, TradeClass
from ts.risk.exits import PositionRecord, evaluate_position_exit
from ts.risk.protections import AssetCooldownTracker, CircuitBreaker
from ts.risk.sizing import calculate_position_size


def test_position_sizing_prd_example() -> None:
    # Example from PRD 1.2.1:
    # Equity = 100,000, Risk = 1% (1,000), Price = 50.0, ATR = 1.0, Support = 47.90
    risk_cfg = RiskConfig(
        risk_per_trade_pct=0.01,
        stop_atr_min_k=1.0,
        stop_atr_max_k=3.0,
        capital_cap_pct=0.20,
        gap_cap_pct=0.02,
        round_lot=100,
        tick_size=0.01,
    )

    res = calculate_position_size(
        entry_price=50.0,
        technical_stop=47.90,
        current_atr14=1.00,
        equity=100000.0,
        risk_cfg=risk_cfg,
    )
    assert res.accepted is True
    # Q capped by 20% capital cap (20,000 / 50 = 400 shares)
    assert res.qty == 400
    assert res.capital_allocated_brl == 20000.0


def test_position_sizing_rejects_stop_beyond_3_atr() -> None:
    risk_cfg = RiskConfig(stop_atr_max_k=3.0)
    # Stop distance = 40.0 - 30.0 = 10.0 > 3.0 * ATR (3 * 2.0 = 6.0)
    res = calculate_position_size(
        entry_price=40.0,
        technical_stop=30.0,
        current_atr14=2.0,
        equity=100000.0,
        risk_cfg=risk_cfg,
    )
    assert res.accepted is False
    assert res.disposition == "REJECTED_SIZE"
    assert "exceeds 3.0x ATR" in str(res.rejection_reason)


def test_mean_reversion_partial_and_time_stop() -> None:
    pos = PositionRecord(
        position_id="P1",
        ticker="PETR4",
        setup="B",
        trade_class=TradeClass.MEAN_REVERSION,
        entry_date=date(2024, 1, 1),
        entry_price=30.0,
        initial_stop=28.0,
        current_stop=28.0,
        target_price=36.0,
        qty_initial=400,
        qty_open=400,
        risk_r=2.0,
    )

    # 1. Bar reaches 33.5 (+1.75R >= +1.5R) -> Partial 50%
    dec = evaluate_position_exit(
        pos,
        bar_date=date(2024, 1, 2),
        open_p=30.0,
        high_p=33.5,
        low_p=29.5,
        close_p=33.0,
    )
    assert dec.should_exit is True
    assert dec.is_partial is True
    assert dec.exit_qty == 200
    assert dec.exit_reason == ExitReason.TARGET
    assert pos.status == "PARTIAL"
    assert pos.current_stop > 30.0  # Moved to breakeven + costs


def test_trend_stagnation_exit() -> None:
    pos = PositionRecord(
        position_id="P2",
        ticker="VALE3",
        setup="A",
        trade_class=TradeClass.TREND,
        entry_date=date(2024, 1, 1),
        entry_price=60.0,
        initial_stop=58.0,
        current_stop=58.0,
        target_price=None,
        qty_initial=200,
        qty_open=200,
        risk_r=2.0,
        bars_held=14,  # Next bar will be bar 15
        highest_price_seen=61.0,  # MFE = 0.5R < 1.0R
    )

    dec = evaluate_position_exit(
        pos,
        bar_date=date(2024, 1, 20),
        open_p=60.5,
        high_p=60.8,
        low_p=59.5,
        close_p=60.0,
    )
    assert dec.should_exit is True
    assert dec.exit_reason == ExitReason.STAGNATION


def test_asset_cooldown_tracker() -> None:
    tracker = AssetCooldownTracker(cooldown_stops_count=2, lookback_days=30, pause_days=10)
    # Stop 1
    assert not tracker.record_stop("PETR4", date(2024, 1, 5))
    assert not tracker.is_paused("PETR4", date(2024, 1, 6))

    # Stop 2 within 30 days
    assert tracker.record_stop("PETR4", date(2024, 1, 15))
    assert tracker.is_paused("PETR4", date(2024, 1, 16))
    assert tracker.is_paused("PETR4", date(2024, 1, 25))
    # Pause is 10 *sessions* (PRD 1.2.5): Jan 15 + 10 weekdays = Jan 29 still paused, Jan 30 free.
    assert tracker.is_paused("PETR4", date(2024, 1, 29))
    assert not tracker.is_paused("PETR4", date(2024, 1, 30))


def test_circuit_breaker_and_shadow_curve() -> None:
    cb = CircuitBreaker(dd_level1=0.08, dd_level2=0.12, shadow_sma_period=5)
    # Drop equity to trigger level 2 (12% DD)
    cb.update_equity(87000.0, shadow_delta=-1000.0)
    assert cb.is_halted is True
    assert not cb.can_open_new_positions()

    # Shadow curve rallies above its SMA5
    for _ in range(6):
        cb.update_equity(87000.0, shadow_delta=3000.0)

    assert cb.is_halted is False
    assert cb.can_open_new_positions()
