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


class DataSettings(BaseModel):
    """Market-data source. ``mt5`` = broker terminal (production); ``yahoo`` = free D1 bars for testing
    without a broker login. Orders are never sent through Yahoo — it only feeds sync/EOD in paper mode."""

    provider: Literal["mt5", "yahoo"] = "mt5"
    yahoo_suffix: str = ".SA"
    yahoo_index_symbol: str = "^BVSP"
    yahoo_max_workers: int = 4
    yahoo_timeout_s: float = 20.0


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
    data: DataSettings = Field(default_factory=DataSettings)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


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
    # Hipótese H1: após o lucro máximo atingir X R, o stop nunca fica abaixo de entrada + custos. 0 = desligado.
    breakeven_after_r: float = 0.0


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
    # Hipótese H2 (Tratado 2.4): fechamento precisa superar a máxima de N dias em pelo menos X. 0 = desligado.
    breakout_min_pct: float = 0.0


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


class SetupFConfig(BaseModel):
    """Reversão em mercado de baixa (Connors IFR2 condicionado ao regime). Só opera com o IBOV em BAIXA
    (close < MM200 e MM50 < MM200), quando os setups de tendência estão bloqueados; não exige tendência do ativo."""

    name: str = "Reversao_Baixa"
    trade_class: Literal["MEAN_REVERSION"] = "MEAN_REVERSION"
    enabled: bool = False
    rsi_period: int = 2
    rsi_max: float = 10.0
    exit_sma: int = 5  # sai na abertura seguinte quando o fechamento passa acima desta média
    time_stop_bars: int = 10
    stop_atr_k: float = 2.8  # stop de proteção (o dimensionamento aceita no máximo stop_atr_max_k ATR)
    entry_type: Literal["OPEN_AUCTION"] = "OPEN_AUCTION"


class SetupGConfig(BaseModel):
    """Força de longo prazo + virada no diário: IFR(14) MENSAL (só meses encerrados) >= rsi_month_min e o
    histograma do MACD diário cruza acima de zero. Saída: stop móvel Chandelier ativo desde a entrada."""

    name: str = "Mensal_Forte_MACD"
    trade_class: Literal["TREND"] = "TREND"
    enabled: bool = False
    rsi_month_period: int = 14
    rsi_month_min: float = 60.0
    stop_atr_k: float = 2.8
    trailing_chandelier_k: float = 3.0
    all_regimes: bool = False  # True = opera em qualquer regime do IBOV, sem o filtro de tendência do ativo
    entry_type: Literal["OPEN_AUCTION"] = "OPEN_AUCTION"


class SetupsContainer(BaseModel):
    A: SetupAConfig = Field(default_factory=SetupAConfig)
    B: SetupBConfig = Field(default_factory=SetupBConfig)
    C: SetupCConfig = Field(default_factory=SetupCConfig)
    D: SetupDConfig = Field(default_factory=SetupDConfig)
    E: SetupEConfig = Field(default_factory=SetupEConfig)
    F: SetupFConfig = Field(default_factory=SetupFConfig)
    G: SetupGConfig = Field(default_factory=SetupGConfig)


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
    # Teto de risco embutido na ordem: preço máximo de execução = stop + risco-alvo*(1+x)/qtd. 0 = sem tolerância.
    max_entry_risk_overshoot_pct: float = 0.0


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
    # Quarentena também por sequência de perdas (PRD v2.1 ajustado). 0 desliga.
    quarantine_consecutive_losses: int = 6
    # Setup suspenso pelo monitor continua sendo acompanhado na sombra (sem dinheiro) e volta sozinho quando as
    # últimas N operações-sombra têm média positiva. 0 = volta só por decisão do trader (comportamento antigo).
    setup_resume_trades: int = 20
    # CUSUM unilateral sobre R (detecção precoce de queda de média do setup).
    cusum_k_sigma: float = 0.5
    cusum_h_sigma: float = 5.0


class RouterSetupRule(BaseModel):
    """Where a setup may trade: allowed Diamond phases and whether it needs a trending tape (ER)."""

    phases: list[int] = Field(default_factory=lambda: [2, 3])
    trend_only: bool = False


def _default_router_rules() -> dict[str, RouterSetupRule]:
    # Defaults from the reference books (Tratado, seções 1.3, 1.4, 2.1, 2.2, 4.1):
    #  A pivô de alta / C Donchian: tendência em construção ou plena, com fita direcional (ER alto).
    #  B pullback: só na fase 3 ("compras plenas e pullbacks").
    #  D squeeze: nasce de volatilidade baixa — ER baixo é esperado, não exige tendência.
    #  E divergência: reversão dentro de tendência primária (fases 3 e 4).
    #  Fase 4 (Aviso: perda de momentum) bloqueia entradas de tendência.
    return {
        "A": RouterSetupRule(phases=[2, 3], trend_only=True),
        "B": RouterSetupRule(phases=[3], trend_only=False),
        "C": RouterSetupRule(phases=[2, 3], trend_only=True),
        "D": RouterSetupRule(phases=[2, 3], trend_only=False),
        "E": RouterSetupRule(phases=[3, 4], trend_only=False),
    }


class RegimeRouterConfig(BaseModel):
    """Regime router (hypothesis under validation): disabled by default; enable only after the regime report."""

    enabled: bool = False
    er_period: int = 10
    er_trend_min: float = 0.30  # Kaufman ER at or above this = trending tape
    breadth_min_pct_above_sma50: float = 0.0  # share of the universe above its SMA50 (0 disables)
    rules: dict[str, RouterSetupRule] = Field(default_factory=_default_router_rules)


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
    regime_router: RegimeRouterConfig = Field(default_factory=RegimeRouterConfig)


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

    # Machine-specific overrides (login, provider, paths) live in settings.local.yaml, which is git-ignored,
    # so the auto-updater can `git reset` the repository without touching them.
    local = p.with_name("settings.local.yaml")
    if local.exists():
        try:
            raw = _deep_merge(raw, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
        except Exception as e:
            raise ConfigError(f"Error parsing YAML in {local}: {e}") from e

    try:
        settings = Settings.model_validate(raw)
        if settings.execution.mode == "live" and settings.data.provider != "mt5":
            raise ConfigError("Invalid configuration at 'data.provider': live mode requires provider 'mt5'")
        return settings
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

    cfg, cfg_hash, _ = load_effective_strategy(p, raw)
    return cfg, cfg_hash


def _validate_strategy(raw: dict[str, Any]) -> StrategyConfig:
    try:
        return StrategyConfig.model_validate(raw)
    except ValidationError as e:
        first_err = e.errors()[0]
        loc = ".".join(str(x) for x in first_err.get("loc", []))
        msg = first_err.get("msg", "Invalid value")
        raise ConfigError(f"Invalid strategy configuration at '{loc}': {msg}") from e


def load_effective_strategy(path: Path, raw: dict[str, Any] | None = None) -> tuple[StrategyConfig, str, Any]:
    """Base strategy.yaml + approved override, validated against the guardrails (strategy_default.yaml).

    Returns (config, hash of the effective config, EffectiveConfig with override status / base hash).
    """
    from ts.core.guardrails import apply_override, check_limits, load_limits, load_override

    if raw is None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base_cfg = _validate_strategy(raw)
    base_hash = strategy_config_hash(base_cfg)
    base_dump = base_cfg.model_dump(mode="json")
    limits = load_limits(path.parent)
    eff = apply_override(base_dump, base_hash, load_override(path.parent), limits)
    if eff.violations:
        raise ConfigError("strategy.override.yaml viola as travas: " + "; ".join(eff.violations))
    cfg = _validate_strategy(eff.raw) if eff.override_status == "applied" else base_cfg
    if limits is not None:
        problems = check_limits(cfg.model_dump(mode="json"), limits)
        if problems:
            raise ConfigError("Configuração fora das travas de strategy_default.yaml: " + "; ".join(problems))
    return cfg, strategy_config_hash(cfg), eff


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
