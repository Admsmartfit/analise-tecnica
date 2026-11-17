"""Configuration loading and validation with Pydantic and SHA-256 hashing."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from ts.core.errors import ConfigError


class SystemSettings(BaseModel):
    root_path: str = "C:\\TradingSystem"
    data_dir: str = "data"
    db_path: str = "data/db/trading.db"
    logs_dir: str = "logs"
    reports_dir: str = "reports"
    corporate_actions_path: str = "config/corporate_actions.csv"
    lock_path: str = "data/ts.lock"


class ExecutionSettings(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    magic_number: int = 20261117
    client_id_prefix: str = "TS"
    fractional_shares: bool = False
    reference_capital: float = 100000.0


class MT5Settings(BaseModel):
    terminal_path: str = "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
    server: str = "XP-PRD"
    login: int = 0
    timeout_ms: int = 60000
    ibov_symbol: str = "IBOV"
    keyring_service: str = "ts-mt5"
    # Fuso do relógio do servidor MT5 (o MT5 entrega o horário da barra no fuso do servidor).
    server_tz: str = "America/Sao_Paulo"
    # Ativo líquido usado para medir a idade do último tick (desconexão silenciosa).
    heartbeat_symbol: str = "PETR4"
    max_tick_age_seconds: float = 120.0


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_lines: bool = True
    directory: str = "logs"


class TelegramSettings(BaseModel):
    enabled: bool = False
    chat_id: str = ""
    keyring_service: str = "ts-telegram"


class Settings(BaseModel):
    system: SystemSettings = Field(default_factory=SystemSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    mt5: MT5Settings = Field(default_factory=MT5Settings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)


class MarketFilterConfig(BaseModel):
    ibov_sma_long: int = 200
    ibov_sma_short: int = 50
    require_bull_regime: bool = True


class PivotConfig(BaseModel):
    window_n: int = 2
    magnitude_atr_k: float = 1.0
    atr_period: int = 14


class SetupAConfig(BaseModel):
    name: str = "Pivo_de_Alta"
    trade_class: Literal["TREND"] = "TREND"
    enabled: bool = True
    adx_threshold: float = 20.0
    rvol_min: float = 1.5
    entry_type: Literal["OPEN_AUCTION"] = "OPEN_AUCTION"
    trailing_chandelier_k: float = 3.0
    chandelier_atr_period: int = 22
    stagnation_bars: int = 15
    stagnation_mfe_r: float = 1.0


class SetupBConfig(BaseModel):
    name: str = "Pullback"
    trade_class: Literal["MEAN_REVERSION"] = "MEAN_REVERSION"
    enabled: bool = True
    ema_short: int = 20
    sma_medium: int = 50
    pullback_bars_min: int = 2
    pullback_bars_max: int = 5
    rsi_period: int = 14
    rsi_max: float = 45.0
    adx_recent_bars: int = 10
    adx_threshold: float = 20.0
    entry_type: Literal["BUY_STOP"] = "BUY_STOP"
    partial_target_r: float = 1.5
    partial_pct: float = 0.50
    final_target_r: float = 3.0
    time_stop_bars: int = 8
    min_rr_filter: float = 3.0


class SetupCConfig(BaseModel):
    name: str = "Donchian_20_10"
    trade_class: Literal["TREND"] = "TREND"
    enabled: bool = True
    donchian_entry_period: int = 20
    donchian_exit_period: int = 10
    adx_threshold: float = 20.0
    rvol_min: float = 1.5
    entry_type: Literal["OPEN_AUCTION"] = "OPEN_AUCTION"
    stagnation_bars: int = 15
    stagnation_mfe_r: float = 1.0


class SetupDConfig(BaseModel):
    name: str = "Bollinger_Squeeze"
    trade_class: Literal["TREND"] = "TREND"
    enabled: bool = True
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    bollinger_ddof: int = 0
    squeeze_lookback: int = 120
    squeeze_percentile_q: float = 10.0
    rvol_min: float = 1.5
    entry_type: Literal["OPEN_AUCTION"] = "OPEN_AUCTION"
    trailing_chandelier_k: float = 3.0
    chandelier_atr_period: int = 22
    stagnation_bars: int = 15
    stagnation_mfe_r: float = 1.0


class SetupEConfig(BaseModel):
    name: str = "Divergencia_Alta"
    trade_class: Literal["MEAN_REVERSION"] = "MEAN_REVERSION"
    enabled: bool = True
    bars_between_pivots_min: int = 5
    bars_between_pivots_max: int = 30
    price_tolerance: float = 1.005
    rsi_delta_min: float = 3.0
    rsi_period: int = 14
    entry_type: Literal["BUY_STOP"] = "BUY_STOP"
    partial_target_r: float = 1.5
    partial_pct: float = 0.50
    final_target_r: float = 3.0
    time_stop_bars: int = 8
    min_rr_filter: float = 3.0


class SetupsContainer(BaseModel):
    A: SetupAConfig = Field(default_factory=SetupAConfig)
    B: SetupBConfig = Field(default_factory=SetupBConfig)
    C: SetupCConfig = Field(default_factory=SetupCConfig)
    D: SetupDConfig = Field(default_factory=SetupDConfig)
    E: SetupEConfig = Field(default_factory=SetupEConfig)


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 0.01
    stop_atr_min_k: float = 1.0
    stop_atr_max_k: float = 3.0
    capital_cap_pct: float = 0.20
    gap_cap_pct: float = 0.02
    default_gap_p01: float = 0.035
    gap_lookback_days: int = 500
    round_lot: int = 100
    tick_size: float = 0.01
    # Abaixo deste % do risco-alvo, o arredondamento do lote é registrado (PRD 1.2.1, trava 3).
    min_risk_efficiency: float = 0.70
    # Ordem de abertura = limitada com preço de proteção (PRD 1.3.1, leilão). Sem fill se a abertura passar disso.
    auction_limit_protection_pct: float = 0.03


class PortfolioConfig(BaseModel):
    max_heat_pct: float = 0.05
    max_correlated_risk_pct: float = 0.03
    max_positions_per_cluster: int = 2
    cluster_correlation_threshold: float = 0.70
    max_beta: float = 1.20
    correlation_lookback_days: int = 60
    beta_lookback_days: int = 120
    relative_strength_days: int = 60


class RegimeTier(BaseModel):
    max_percentile: float
    risk_multiplier: float
    heat_max_pct: float


class RegimeConfig(BaseModel):
    lookback_years: int = 5
    realized_vol_days: int = 20
    tiers: list[RegimeTier] = Field(default_factory=list)


class ProtectionsConfig(BaseModel):
    cooldown_stops_count: int = 2
    cooldown_lookback_days: int = 30
    cooldown_pause_days: int = 10
    setup_monitor_window: int = 30
    setup_bootstrap_quantile: float = 0.05
    circuit_breaker_dd_level1: float = 0.08
    circuit_breaker_dd_level2: float = 0.12
    # Multiplicador de risco enquanto o drawdown estiver entre o nível 1 e o nível 2.
    circuit_breaker_level1_risk_multiplier: float = 0.50
    shadow_sma_period: int = 20
    # CUSUM unilateral sobre R (detecção precoce de queda de média do setup).
    cusum_k_sigma: float = 0.5
    cusum_h_sigma: float = 5.0


class StrategyConfig(BaseModel):
    # Ativos com menos barras que isso ficam fora do universo (aquecimento de EMA/RMA, PRD 1.1.5).
    min_history_bars: int = 260
    asset_trend_sma: int = 200
    market_filter: MarketFilterConfig = Field(default_factory=MarketFilterConfig)
    pivot: PivotConfig = Field(default_factory=PivotConfig)
    setups: SetupsContainer = Field(default_factory=SetupsContainer)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    protections: ProtectionsConfig = Field(default_factory=ProtectionsConfig)


class TaxConfig(BaseModel):
    swing_trade_rate: float = 0.15
    monthly_sales_exemption_brl: float = 20000.0


class CostsDetails(BaseModel):
    effective_date: str = "2024-01-01"
    b3_emoluments_pct: float = 0.0003
    brokerage_per_order: float = 0.0
    iss_pct: float = 0.0
    slippage_pct: float = 0.0005
    tax: TaxConfig = Field(default_factory=TaxConfig)


class CostsConfig(BaseModel):
    costs: CostsDetails = Field(default_factory=CostsDetails)


def compute_normalized_yaml_hash(content: str | dict[str, Any]) -> str:
    """Compute canonical SHA-256 hash from normalized YAML/dict representation."""
    if isinstance(content, str):
        parsed = yaml.safe_load(content)
    else:
        parsed = content
    normalized_str = yaml.dump(parsed, sort_keys=True)
    return hashlib.sha256(normalized_str.encode("utf-8")).hexdigest()[:16]


def strategy_config_hash(cfg: StrategyConfig) -> str:
    """Hash of the *effective* configuration (defaults included).

    Uses the validated model, so two YAML files that resolve to the same parameters share a hash and
    a config built in code (walk-forward, plateau) gets its own hash instead of the file's.
    """
    return compute_normalized_yaml_hash(cfg.model_dump(mode="json"))


def load_settings(path: str | Path | None = None) -> Settings:
    """Load and validate settings.yaml."""
    if path is None:
        candidate = Path("config/settings.yaml")
        if not candidate.exists():
            candidate = Path(__file__).resolve().parent.parent.parent.parent / "config" / "settings.yaml"
        path = candidate

    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Settings file not found at: {p}")

    with open(p, encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f) or {}
        except Exception as e:
            raise ConfigError(f"Error parsing YAML in {p}: {e}") from e

    try:
        return Settings.model_validate(raw)
    except ValidationError as e:
        # Extract problem key for explicit error reporting
        first_err = e.errors()[0]
        loc = ".".join(str(x) for x in first_err.get("loc", []))
        msg = first_err.get("msg", "Invalid value")
        raise ConfigError(f"Invalid configuration at '{loc}': {msg}") from e


def load_strategy_config(path: str | Path | None = None) -> tuple[StrategyConfig, str]:
    """Load, validate strategy.yaml and return (StrategyConfig, config_hash)."""
    if path is None:
        candidate = Path("config/strategy.yaml")
        if not candidate.exists():
            candidate = Path(__file__).resolve().parent.parent.parent.parent / "config" / "strategy.yaml"
        path = candidate

    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Strategy config file not found at: {p}")

    with open(p, encoding="utf-8") as f:
        text = f.read()
        try:
            raw = yaml.safe_load(text) or {}
        except Exception as e:
            raise ConfigError(f"Error parsing YAML in {p}: {e}") from e

    try:
        cfg = StrategyConfig.model_validate(raw)
        return cfg, strategy_config_hash(cfg)
    except ValidationError as e:
        first_err = e.errors()[0]
        loc = ".".join(str(x) for x in first_err.get("loc", []))
        msg = first_err.get("msg", "Invalid value")
        raise ConfigError(f"Invalid strategy configuration at '{loc}': {msg}") from e


def load_costs_config(path: str | Path | None = None) -> CostsConfig:
    """Load and validate costs.yaml."""
    if path is None:
        candidate = Path("config/costs.yaml")
        if not candidate.exists():
            candidate = Path(__file__).resolve().parent.parent.parent.parent / "config" / "costs.yaml"
        path = candidate

    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Costs config file not found at: {p}")

    with open(p, encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f) or {}
        except Exception as e:
            raise ConfigError(f"Error parsing YAML in {p}: {e}") from e

    try:
        return CostsConfig.model_validate(raw)
    except ValidationError as e:
        first_err = e.errors()[0]
        loc = ".".join(str(x) for x in first_err.get("loc", []))
        msg = first_err.get("msg", "Invalid value")
        raise ConfigError(f"Invalid costs configuration at '{loc}': {msg}") from e
