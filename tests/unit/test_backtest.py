"""Unit tests for event-driven backtesting, costs, metrics and Portão G0."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from ts.backtest.engine import CompletedTrade
from ts.backtest.metrics import (
    compute_expectancy_bootstrap,
    compute_performance_metrics,
    validate_gate_g0,
)
from ts.backtest.walkforward import WalkForwardEngine
from ts.core.models import TradeClass


def test_expectancy_bootstrap_bounds() -> None:
    # Set of positive R trades
    trades_r = [1.5, 3.0, -1.0, 1.5, 2.0, -1.0, 3.0, 1.5] * 5
    mean_r, ci_lower, ci_upper = compute_expectancy_bootstrap(trades_r, n_boot=200)

    assert mean_r > 0.0
    assert ci_lower <= mean_r <= ci_upper
    assert ci_lower > 0.0


def test_gate_g0_validation_logic() -> None:
    # 1. Winning trade history
    trades = [
        CompletedTrade(
            position_id=f"T{i}",
            ticker="PETR4",
            setup="A",
            trade_class=TradeClass.TREND,
            entry_date=date(2024, 1, 1),
            exit_date=date(2024, 1, 10),
            entry_price=30.0,
            exit_price=32.0 if i % 3 != 0 else 29.0,
            qty=100,
            risk_r=1.0,
            r_multiple=2.0 if i % 3 != 0 else -1.0,
            pnl_brl=200.0 if i % 3 != 0 else -100.0,
            fees_brl=1.0,
            net_pnl_brl=199.0 if i % 3 != 0 else -101.0,
            exit_reason="TRAILING",
            bars_held=7,
        )
        for i in range(30)
    ]

    eq_curve = pd.DataFrame(
        {
            "session_date": [date(2024, 1, 1) + timedelta(days=i) for i in range(30)],
            "equity": [100000.0 + i * 150.0 for i in range(30)],
            "drawdown_pct": [0.01] * 30,
        }
    )

    metrics = compute_performance_metrics(trades, eq_curve)
    assert metrics.expectancy_r > 0.0
    assert metrics.win_rate > 0.60
    assert metrics.profit_factor > 1.0

    g0 = validate_gate_g0(metrics)
    assert g0["expectancy_positive"] is True
    assert g0["concentration_passed"] is True


def test_walkforward_g0_report_generation() -> None:
    wf = WalkForwardEngine()
    fake_trades = [
        CompletedTrade(
            position_id="T1",
            ticker="VALE3",
            setup="C",
            trade_class=TradeClass.TREND,
            entry_date=date(2024, 1, 2),
            exit_date=date(2024, 1, 15),
            entry_price=50.0,
            exit_price=54.0,
            qty=200,
            risk_r=2.0,
            r_multiple=2.0,
            pnl_brl=800.0,
            fees_brl=4.0,
            net_pnl_brl=796.0,
            exit_reason="TRAILING",
            bars_held=9,
        )
    ]
    eq_curve = pd.DataFrame(
        {
            "session_date": [date(2024, 1, 2), date(2024, 1, 15)],
            "equity": [100000.0, 100796.0],
            "drawdown_pct": [0.0, 0.0],
        }
    )

    from ts.backtest.engine import BacktestResult

    res = BacktestResult(
        trades=fake_trades,
        equity_curve=eq_curve,
        config_hash="abc12345",
        initial_capital=100000.0,
        final_equity=100796.0,
        total_net_pnl=796.0,
        total_fees=4.0,
    )

    rep = wf.generate_g0_report(res)
    assert "Relatório de Robustez" in rep.summary_markdown
    assert "Portão G0" in rep.summary_markdown
