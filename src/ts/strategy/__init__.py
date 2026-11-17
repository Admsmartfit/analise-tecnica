"""Trading strategy layer with regime filters, setups, and signal evaluation."""
from ts.strategy.regime import is_asset_primary_trend_bullish, is_market_filter_bullish
from ts.strategy.setups import (
    evaluate_setup_a,
    evaluate_setup_b,
    evaluate_setup_c,
    evaluate_setup_d,
    evaluate_setup_e,
)
from ts.strategy.signal_engine import SignalEngine

__all__ = [
    "SignalEngine",
    "evaluate_setup_a",
    "evaluate_setup_b",
    "evaluate_setup_c",
    "evaluate_setup_d",
    "evaluate_setup_e",
    "is_asset_primary_trend_bullish",
    "is_market_filter_bullish",
]
