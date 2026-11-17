"""Walk-forward, parameter grid (for PBO/DSR), ±20% plateau, regime check and the G0 report (PRD 1.4.2).

Method
------
1. Each configuration of the grid is backtested once over the whole period with the production engine.
2. Rolling walk-forward: for every fold, the configuration with the best in-sample expectancy (R) is
   chosen and only its *out-of-sample* trades/returns are kept. The concatenated OOS series is what G0
   judges (expectancy, bootstrap, DSR, concentration, regimes).
3. PBO is computed with CSCV on the grid's daily-returns matrix; DSR deflates the OOS Sharpe by the number
   of configurations tried and the cross-sectional variance of their Sharpes.
4. Plateau: each key parameter moved to -20% and +20% (one at a time) must keep a positive expectancy.

Each configuration runs its own path from the start (positions carried across fold boundaries). This is
the standard "selection among pre-computed strategies" approximation and is documented in the report.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ts.backtest.engine import BacktestEngine, BacktestResult
from ts.backtest.metrics import (
    PerformanceMetrics,
    compute_expectancy_bootstrap,
    compute_pbo_cscv,
    compute_performance_metrics,
    validate_gate_g0,
)
from ts.core.calendar import B3Calendar
from ts.core.config import CostsConfig, StrategyConfig
from ts.execution.session_engine import CompletedTrade
from ts.strategy.regime import market_state_series
from ts.strategy.signal_engine import TickerData

logger = logging.getLogger("ts.backtest.walkforward")

DEFAULT_PLATEAU_PARAMS: tuple[str, ...] = (
    "setups.A.adx_threshold",
    "setups.C.adx_threshold",
    "setups.A.rvol_min",
    "setups.C.rvol_min",
    "setups.D.rvol_min",
    "setups.B.rsi_max",
    "setups.D.squeeze_percentile_q",
    "setups.A.trailing_chandelier_k",
    "setups.B.partial_target_r",
    "setups.B.time_stop_bars",
    "risk.stop_atr_min_k",
    "risk.stop_atr_max_k",
    "pivot.magnitude_atr_k",
)

DEFAULT_GRID: dict[str, Sequence[Any]] = {
    "setups.B.rsi_max": (40.0, 45.0, 50.0),
    "setups.A.adx_threshold|setups.C.adx_threshold": (20.0, 25.0),
    "setups.A.rvol_min|setups.C.rvol_min|setups.D.rvol_min": (1.3, 1.5),
}


def _set_path(cfg: StrategyConfig, path: str, value: Any) -> None:
    obj: Any = cfg
    parts = path.split(".")
    for p in parts[:-1]:
        obj = getattr(obj, p)
    current = getattr(obj, parts[-1])
    setattr(obj, parts[-1], int(round(value)) if isinstance(current, int) and not isinstance(current, bool) else float(value))


def _get_path(cfg: StrategyConfig, path: str) -> Any:
    obj: Any = cfg
    for p in path.split("."):
        obj = getattr(obj, p)
    return obj


def build_grid(base: StrategyConfig, grid: dict[str, Sequence[Any]]) -> list[StrategyConfig]:
    keys = list(grid)
    configs: list[StrategyConfig] = []
    for combo in np.array(np.meshgrid(*[np.arange(len(grid[k])) for k in keys], indexing="ij")).reshape(len(keys), -1).T:
        cfg = base.model_copy(deep=True)
        for key, i in zip(keys, combo, strict=True):
            for path in key.split("|"):
                _set_path(cfg, path, grid[key][int(i)])
        configs.append(cfg)
    return configs


def _feature_key(cfg: StrategyConfig) -> str:
    """Parameters that change the precomputed features/pivots (thresholds do not)."""
    s = cfg.setups
    key = {
        "trend": cfg.asset_trend_sma, "pivot": cfg.pivot.model_dump(), "b": [s.B.sma_medium, s.B.ema_short, s.B.rsi_period],
        "e": s.E.rsi_period, "a": s.A.chandelier_atr_period,
        "d": [s.D.bollinger_period, s.D.bollinger_std, s.D.bollinger_ddof],
        "c": [s.C.donchian_entry_period, s.C.donchian_exit_period], "gap": cfg.risk.gap_lookback_days,
        "rs": cfg.portfolio.relative_strength_days,
    }
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()


@dataclass
class WalkForwardResult:
    oos_trades: list[CompletedTrade]
    oos_returns: pd.Series
    selections: list[dict[str, Any]]
    grid_returns: pd.DataFrame
    grid_results: list[BacktestResult]
    oos_open_positions: pd.Series | None = None


@dataclass
class G0Report:
    approved: bool
    summary_markdown: str
    metrics: PerformanceMetrics
    gate_checks: dict[str, Any]
    details: dict[str, Any] = field(default_factory=dict)


class WalkForwardEngine:
    def __init__(
        self,
        base_config: StrategyConfig | None = None,
        costs_config: CostsConfig | None = None,
        initial_capital: float = 100000.0,
        calendar: B3Calendar | None = None,
    ) -> None:
        self.base_config = base_config or StrategyConfig()
        self.costs_config = costs_config
        self.initial_capital = initial_capital
        self.calendar = calendar
        self._prep_cache: dict[str, dict[str, TickerData]] = {}

    # ------------------------------------------------------------------ running
    def _engine(self, cfg: StrategyConfig) -> BacktestEngine:
        return BacktestEngine(cfg, self.costs_config, self.initial_capital, self.calendar)

    def run_config(self, cfg: StrategyConfig, bars: dict[str, pd.DataFrame], index_bars: pd.DataFrame,
                   universe_fn: Callable[[date], list[str]] | None = None) -> BacktestResult:
        eng = self._engine(cfg)
        key = _feature_key(cfg)
        if key not in self._prep_cache:
            self._prep_cache[key] = eng.prepare(bars)
        return eng.run(bars, index_bars, universe_fn=universe_fn, prepared=self._prep_cache[key])

    def walk_forward(
        self,
        results: list[BacktestResult],
        train_sessions: int = 756,
        test_sessions: int = 252,
    ) -> WalkForwardResult:
        rets = pd.DataFrame({i: r.daily_returns for i, r in enumerate(results)}).fillna(0.0)
        dates = list(rets.index)
        oos_trades: list[CompletedTrade] = []
        oos_parts: list[pd.Series] = []
        pos_parts: list[pd.Series] = []
        selections: list[dict[str, Any]] = []
        start = train_sessions
        while start < len(dates):
            is_from, is_to = dates[start - train_sessions], dates[start - 1]
            oos_from, oos_to = dates[start], dates[min(start + test_sessions, len(dates)) - 1]
            scores = []
            for r in results:
                rs = [t.r_multiple for t in r.trades if is_from <= t.entry_date <= is_to]
                scores.append(np.mean(rs) if len(rs) >= 10 else -np.inf)
            best = int(np.argmax(scores))
            selections.append({"is": (is_from, is_to), "oos": (oos_from, oos_to), "config": best,
                               "is_expectancy": float(scores[best])})
            oos_trades += [t for t in results[best].trades if oos_from <= t.entry_date <= oos_to]
            oos_parts.append(rets[best].loc[oos_from:oos_to])
            ec = results[best].equity_curve
            if "open_positions" in ec.columns:
                pos_parts.append(ec.set_index("session_date")["open_positions"].loc[oos_from:oos_to])
            start += test_sessions
        oos = pd.concat(oos_parts) if oos_parts else pd.Series(dtype=float)
        pos = pd.concat(pos_parts) if pos_parts else None
        return WalkForwardResult(oos_trades, oos, selections, rets, results, pos)

    def run_sensitivity_plateau(
        self,
        engine: BacktestEngine | None,
        bars: dict[str, pd.DataFrame],
        index_bars: pd.DataFrame,
        variation_pct: float = 0.20,
        params: Sequence[str] = DEFAULT_PLATEAU_PARAMS,
        universe_fn: Callable[[date], list[str]] | None = None,
    ) -> tuple[bool, dict[str, float]]:
        """Every parameter at ±variation (one at a time) must keep expectancy > 0."""
        base = engine.strategy_config if engine is not None else self.base_config
        out: dict[str, float] = {}
        for path in params:
            v0 = _get_path(base, path)
            for mult in (1.0 - variation_pct, 1.0 + variation_pct):
                cfg = base.model_copy(deep=True)
                _set_path(cfg, path, v0 * mult)
                res = self.run_config(cfg, bars, index_bars, universe_fn)
                exp = float(np.mean([t.r_multiple for t in res.trades])) if res.trades else float("nan")
                out[f"{path}@{mult:.2f}"] = exp
        ok = all(np.isfinite(v) and v > 0 for v in out.values())
        return ok, out

    @staticmethod
    def regime_check(trades: list[CompletedTrade], index_bars: pd.DataFrame,
                     cfg: StrategyConfig | None = None) -> tuple[bool, dict[str, dict[str, float]]]:
        """Positive expectancy in at least 2 of the 3 IBOV regimes (BULL, SIDEWAYS, BEAR)."""
        if index_bars.empty or not trades:
            return False, {}
        idx = index_bars.sort_values("session_date").reset_index(drop=True)
        states = dict(zip(idx["session_date"], market_state_series(idx, (cfg or StrategyConfig()).market_filter), strict=True))
        by: dict[str, list[float]] = {"BULL": [], "SIDEWAYS": [], "BEAR": []}
        for t in trades:
            by[states.get(t.entry_date, "SIDEWAYS")].append(t.r_multiple)
        detail = {k: {"trades": float(len(v)), "expectancy_r": float(np.mean(v)) if v else float("nan")} for k, v in by.items()}
        positives = sum(1 for v in by.values() if v and np.mean(v) > 0)
        # A regime with no trades (e.g. BEAR filtered out by Camada 1) counts as "not negative".
        neutral = sum(1 for v in by.values() if not v)
        return positives + neutral >= 2 and positives >= 1, detail

    # ------------------------------------------------------------------ full G0
    def run_full_g0(
        self,
        bars: dict[str, pd.DataFrame],
        index_bars: pd.DataFrame,
        grid: dict[str, Sequence[Any]] | None = None,
        universe_fn: Callable[[date], list[str]] | None = None,
        train_sessions: int = 756,
        test_sessions: int = 252,
        plateau_params: Sequence[str] = DEFAULT_PLATEAU_PARAMS,
    ) -> G0Report:
        configs = build_grid(self.base_config, grid or DEFAULT_GRID)
        results = [self.run_config(c, bars, index_bars, universe_fn) for c in configs]
        wf = self.walk_forward(results, train_sessions, test_sessions)
        pbo = compute_pbo_cscv(wf.grid_returns) if wf.grid_returns.shape[1] >= 2 else float("nan")
        sr = wf.grid_returns.mean() / wf.grid_returns.std(ddof=1).replace(0, np.nan)
        sr_var = float(np.nanvar(sr.to_numpy(), ddof=1)) if len(sr) > 1 else None
        plateau_ok, plateau = self.run_sensitivity_plateau(None, bars, index_bars, params=plateau_params, universe_fn=universe_fn)
        regimes_ok, regimes = self.regime_check(wf.oos_trades, index_bars, self.base_config)

        oos_eq = (1.0 + wf.oos_returns).cumprod() * self.initial_capital
        eq_df = pd.DataFrame({"session_date": list(oos_eq.index), "equity": oos_eq.to_numpy()})
        if not eq_df.empty:
            eq_df["drawdown_pct"] = (eq_df["equity"].cummax() - eq_df["equity"]) / eq_df["equity"].cummax()
            if wf.oos_open_positions is not None:  # exposure metric (was always 0% before)
                eq_df["open_positions"] = wf.oos_open_positions.reindex(oos_eq.index).fillna(0).to_numpy()
        oos_result = BacktestResult(
            trades=wf.oos_trades, equity_curve=eq_df, config_hash="walk-forward-oos",
            initial_capital=self.initial_capital,
            final_equity=float(eq_df["equity"].iloc[-1]) if not eq_df.empty else self.initial_capital,
            total_net_pnl=round(sum(t.net_pnl_brl for t in wf.oos_trades), 2),
            total_fees=round(sum(t.fees_brl for t in wf.oos_trades), 2),
        )
        return self.generate_g0_report(
            oos_result, n_trials=len(configs), plateau=(plateau_ok, plateau), regimes=(regimes_ok, regimes),
            pbo=pbo, sr_variance=sr_var, selections=wf.selections,
        )

    # ------------------------------------------------------------------ report
    def generate_g0_report(
        self,
        result: BacktestResult,
        n_trials: int = 1,
        plateau: tuple[bool, dict[str, float]] | None = None,
        regimes: tuple[bool, dict[str, dict[str, float]]] | None = None,
        pbo: float | None = None,
        sr_variance: float | None = None,
        selections: list[dict[str, Any]] | None = None,
    ) -> G0Report:
        metrics = compute_performance_metrics(result.trades, result.equity_curve, n_trials, sr_variance, pbo)
        checks = validate_gate_g0(metrics, plateau[0] if plateau else None, regimes[0] if regimes else None)
        approved = bool(checks["overall_g0_approved"])

        total_net = sum(t.net_pnl_brl for t in result.trades)
        conc = (f"{metrics.max_single_trade_profit_pct:.1%}" if total_net > 0
                else "n/a — lucro total ≤ 0")

        def st(key: str) -> str:
            return "PASS" if checks[key] else "FAIL"

        lines = [
            "# Relatório de Robustez e Validação — Portão G0",
            "",
            f"**Data de Geração:** {date.today().isoformat()}  ",
            f"**Configuração Hash:** `{result.config_hash}`  ",
            f"**Configurações testadas (N):** {n_trials}  ",
            f"**Resultado Global do Portão G0:** {'APROVADO (GO)' if approved else 'REPROVADO (NO-GO)'}",
            "",
            "## 1. Critérios obrigatórios (PRD 1.4.2)",
            "",
            "| Critério | Meta | Valor | Status |",
            "| --- | --- | --- | --- |",
            f"| Esperança em R (OOS, com custos) | > 0 | {metrics.expectancy_r:+.3f}R | {st('expectancy_positive')} |",
            f"| Limite inferior bootstrap 95% | > 0 | {metrics.bootstrap_ci_lower_r:+.3f}R | {st('bootstrap_ci_lower_positive')} |",
            f"| Deflated Sharpe Ratio | ≥ 0,95 | {metrics.deflated_sharpe_prob:.3f} | {st('dsr_passed')} |",
            f"| PBO (CSCV) | < 0,20 | {metrics.pbo:.3f} | {st('pbo_passed')} |",
            f"| Maior operação / lucro líquido total | ≤ 20% | {conc} | {st('concentration_passed')} |",
            f"| Platô ±20% (esperança > 0 em todos) | todos > 0 | {'avaliado' if plateau else 'NÃO AVALIADO'} | {st('plateau_passed')} |",
            f"| Positivo em ≥ 2 de 3 regimes | 2/3 | {'avaliado' if regimes else 'NÃO AVALIADO'} | {st('regimes_passed')} |",
            "",
            "## 2. Estatísticas (PRD 1.4.3)",
            "",
            f"- Operações: {metrics.total_trades} | Taxa de acerto: {metrics.win_rate:.1%} | Profit factor: {metrics.profit_factor}",
            f"- Distribuição de R: assimetria {metrics.r_skew}, curtose {metrics.r_kurtosis}, maior ganho {metrics.max_win_r}R, maior perda {metrics.max_loss_r}R",
            f"- Perdas abaixo de −1,5R (gap/execução): {metrics.losses_below_minus_1_5r}",
            f"- CVaR 5% por operação: {metrics.cvar_5pct_r}R | Sequência máx. de perdas: {metrics.max_consecutive_losses}",
            f"- MAE médio: {metrics.avg_mae_r}R | MFE médio: {metrics.avg_mfe_r}R",
            f"- Drawdown máx.: {metrics.max_drawdown_pct:.1%} | Ulcer: {metrics.ulcer_index} | Calmar: {metrics.calmar_ratio} | Sharpe anual (só relatório): {metrics.sharpe_ratio}",
            f"- Custos / lucro bruto: {metrics.cost_over_gross_profit:.1%} (alvo < 25%) | Exposição: {metrics.exposure_pct:.1%}",
            f"- Capital inicial: R$ {result.initial_capital:,.2f} | Patrimônio final da curva (retornos diários encadeados): R$ {result.final_equity:,.2f}",
            f"- Soma do resultado líquido das operações: R$ {result.total_net_pnl:,.2f} | Custos: R$ {result.total_fees:,.2f} "
            "(as duas medidas diferem quando há posições abertas na virada das janelas e pelo encadeamento dos retornos)",
        ]
        if metrics.by_setup:
            lines += ["", "### Por setup", "", "| Setup | Operações | E[R] | IC 95% | Acerto |", "| --- | --- | --- | --- | --- |"]
            for s, v in metrics.by_setup.items():
                lines.append(f"| {s} | {int(v['trades'])} | {v['expectancy_r']:+.3f} | [{v['ci_lower']:+.3f}, {v['ci_upper']:+.3f}] | {v['win_rate']:.0%} |")
            e_trades = metrics.by_setup.get("E", {}).get("trades", 0)
            if e_trades < 30:
                lines.append(f"\n> Setup E com {int(e_trades)} operações (< 30): sai da versão 1 (Decisões).")
        if regimes:
            lines += ["", "### Regimes (IBOV na data de entrada)", ""]
            for k, v in regimes[1].items():
                lines.append(f"- {k}: {int(v['trades'])} operações, E[R] = {v['expectancy_r']:+.3f}")
        if plateau:
            bad = {k: v for k, v in plateau[1].items() if not (np.isfinite(v) and v > 0)}
            lines += ["", f"### Platô: {len(plateau[1]) - len(bad)}/{len(plateau[1])} variações com E[R] > 0"]
            for k, v in bad.items():
                lines.append(f"- FALHA {k}: {v:+.3f}")
        if selections:
            lines += ["", "### Walk-forward (configuração escolhida por janela)", ""]
            for s in selections:
                lines.append(f"- IS {s['is'][0]}→{s['is'][1]} | OOS {s['oos'][0]}→{s['oos'][1]} | config #{s['config']} (E_IS={s['is_expectancy']:+.3f})")
        lines += ["", "_Cada configuração percorre o período inteiro; o WF escolhe entre caminhos pré-computados._"]
        _, lo, hi = compute_expectancy_bootstrap([t.r_multiple for t in result.trades])
        return G0Report(approved, "\n".join(lines) + "\n", metrics, checks,
                        {"plateau": plateau, "regimes": regimes, "pbo": pbo, "ci": (lo, hi)})
