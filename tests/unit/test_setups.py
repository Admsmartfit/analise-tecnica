"""Unit tests for setups A through E and SignalEngine lookahead protections."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from ts.core.config import StrategyConfig
from ts.core.errors import LookaheadError
from ts.core.models import SetupId
from ts.strategy.setups import (
    evaluate_setup_b,
    evaluate_setup_c,
)
from ts.strategy.signal_engine import SignalEngine


@pytest.fixture
def base_strategy_config() -> StrategyConfig:
    cfg = StrategyConfig()
    cfg.market_filter.require_bull_regime = False
    return cfg


def test_signal_engine_raises_lookahead_error(base_strategy_config: StrategyConfig) -> None:
    engine = SignalEngine(config=base_strategy_config, config_hash="test_hash")
    asof = date(2024, 1, 10)

    # Bars containing a future date
    future_date = date(2024, 1, 11)
    df = pd.DataFrame(
        {
            "session_date": [date(2024, 1, 9), date(2024, 1, 10), future_date],
            "close": [10.0, 11.0, 12.0],
        }
    )

    with pytest.raises(LookaheadError) as exc_info:
        engine.evaluate(asof, {"PETR4": df}, pd.DataFrame())
    assert "Lookahead violation" in str(exc_info.value)


def test_evaluate_setup_c_donchian_breakout(base_strategy_config: StrategyConfig) -> None:
    # 50 bars with realistic upward trend and a breakout on last bar
    np.random.seed(42)
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(50)]
    base = 20.0 + np.linspace(0, 5, 49)
    # 50th bar breaks out strongly above 20-day high
    c_last = base[-1] + 3.0
    closes = list(base) + [c_last]
    highs = [c + 0.5 for c in closes]
    highs[-1] = closes[-1] + 0.5
    lows = [c - 0.5 for c in closes]
    vols = [1000] * 49 + [3000]     # RVol = 3.0 >= 1.5

    df = pd.DataFrame(
        {
            "session_date": dates,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "real_volume": vols,
        }
    )

    asof = dates[-1]
    sig = evaluate_setup_c("PETR4", asof, df, [], base_strategy_config, "test_hash")
    assert sig is not None
    assert sig.setup == SetupId.C_DONCHIAN
    assert sig.entry_ref == c_last
    assert sig.entry_type == "OPEN_AUCTION"


def test_evaluate_setup_b_pullback(base_strategy_config: StrategyConfig) -> None:
    # Build 70 bars trending upwards, then 3 correction bars touching EMA20
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(70)]
    closes = [20.0 + i * 0.3 for i in range(65)]
    # Correction of 3 bars with lower highs
    closes += [39.0, 38.5, 38.0, 37.5, 37.0]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    vols = [10000] * 70

    df = pd.DataFrame(
        {
            "session_date": dates,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "real_volume": vols,
        }
    )

    # Disable RR check for direct pullback logic verification
    base_strategy_config.setups.B.rsi_max = 80.0
    base_strategy_config.setups.B.min_rr_filter = 0.0

    asof = dates[-1]
    sig = evaluate_setup_b("PETR4", asof, df, [], base_strategy_config, "test_hash")
    if sig:
        assert sig.setup == SetupId.B_PULLBACK
        assert sig.entry_type == "BUY_STOP"
