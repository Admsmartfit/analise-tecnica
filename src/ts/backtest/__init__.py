"""Backtest and statistical validation package."""
from ts.backtest.engine import BacktestEngine, BacktestResult, CompletedTrade
from ts.backtest.metrics import (
    PerformanceMetrics,
    compute_deflated_sharpe_ratio,
    compute_expectancy_bootstrap,
    compute_performance_metrics,
    validate_gate_g0,
)
from ts.backtest.walkforward import G0Report, WalkForwardEngine

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "CompletedTrade",
    "G0Report",
    "PerformanceMetrics",
    "WalkForwardEngine",
    "compute_deflated_sharpe_ratio",
    "compute_expectancy_bootstrap",
    "compute_performance_metrics",
    "validate_gate_g0",
]
