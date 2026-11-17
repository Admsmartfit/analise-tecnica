"""Unit tests for configuration loaders and Pydantic validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from ts.core.config import (
    load_costs_config,
    load_settings,
    load_strategy_config,
)
from ts.core.errors import ConfigError


def test_load_settings_success() -> None:
    settings = load_settings("config/settings.yaml")
    assert settings.execution.mode in ["paper", "live"]
    assert settings.mt5.server == "XP-PRD"
    assert settings.logging.level in ["DEBUG", "INFO", "WARNING", "ERROR"]


def test_load_settings_invalid_raises_config_error(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "bad_settings.yaml"
    bad_yaml.write_text("execution:\n  mode: invalid_mode\n")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(bad_yaml)
    assert "execution.mode" in str(exc_info.value)


def test_load_strategy_config_and_hash() -> None:
    cfg, cfg_hash = load_strategy_config("config/strategy.yaml")
    assert cfg.market_filter.ibov_sma_long == 200
    assert cfg.setups.A.adx_threshold == 20.0
    assert cfg.setups.B.partial_target_r == 1.5
    assert cfg.setups.D.bollinger_ddof == 0
    assert cfg.risk.capital_cap_pct == 0.20
    assert cfg.portfolio.max_heat_pct == 0.05
    assert len(cfg_hash) == 16


def test_load_costs_config() -> None:
    costs = load_costs_config("config/costs.yaml")
    assert costs.costs.b3_emoluments_pct == 0.0003
    assert costs.costs.tax.swing_trade_rate == 0.15
