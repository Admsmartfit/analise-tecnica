"""Quarantine re-optimization (PRD v2.1, adjusted): search a candidate configuration WITHIN the trader's guardrails
and validate it with the same G0 criteria used before the paper. It never applies anything: the trader approves.

Why the full history and not "the last 6 months": the strategy makes ~20 trades/year; six months are ~10
trades, which cannot distinguish skill from luck, and choosing parameters on the very period of the drawdown is
hindsight. Every grid point is backtested over the whole history with the production engine; PBO (CSCV) and the
Deflated Sharpe account for the number of configurations tried; the chosen point must also survive the ±20%
plateau test.
"""
from __future__ import annotations

import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ts.backtest.engine import BacktestResult
from ts.backtest.metrics import compute_pbo_cscv, compute_performance_metrics
from ts.backtest.walkforward import WalkForwardEngine, _get_path, _set_path
from ts.core.config import CostsConfig, StrategyConfig, strategy_config_hash
from ts.core.guardrails import Limits, check_limits, diff_params

logger = logging.getLogger("ts.backtest.reoptimize")


@dataclass
class Trial:
    params: dict[str, Any]
    cfg: StrategyConfig
    result: BacktestResult
    metrics: Any
    eligible: bool
    why_not: list[str] = field(default_factory=list)


@dataclass
class ReoptResult:
    candidate: StrategyConfig | None
    changes: dict[str, tuple[Any, Any]]
    trials: list[Trial]
    pbo: float
    plateau: dict[str, float]
    report_markdown: str
    verdict: str  # PENDING (candidate found) | NONE_FOUND


def _grid_configs(base: StrategyConfig, limits: Limits) -> list[tuple[dict[str, Any], StrategyConfig]]:
    keys = list(limits.grid)
    out: list[tuple[dict[str, Any], StrategyConfig]] = []
    seen: set[str] = set()
    combos = itertools.product(*[limits.grid[k] for k in keys]) if keys else [()]
    for values in [None, *combos]:  # None = the current configuration, always evaluated
        cfg = base.model_copy(deep=True)
        params: dict[str, Any] = {}
        if values is not None:
            for k, v in zip(keys, values, strict=True):
                _set_path(cfg, k, v)
                params[k] = _get_path(cfg, k)
        if check_limits(cfg.model_dump(mode="json"), limits):
            continue  # grid point outside the guardrails: never tested
        h = strategy_config_hash(cfg)
        if h in seen:
            continue
        seen.add(h)
        out.append((params or {k: _get_path(cfg, k) for k in keys}, cfg))
    return out


def reoptimize(
    bars: dict[str, pd.DataFrame],
    index_bars: pd.DataFrame,
    base: StrategyConfig,
    limits: Limits,
    costs: CostsConfig | None = None,
    capital: float = 100000.0,
    universe_fn: Callable[[date], list[str]] | None = None,
    trigger_reason: str = "",
    plateau_variation: float = 0.20,
) -> ReoptResult:
    wf = WalkForwardEngine(base, costs, capital)
    configs = _grid_configs(base, limits)
    trials: list[Trial] = []
    for params, cfg in configs:
        res = wf.run_config(cfg, bars, index_bars, universe_fn)
        trials.append(Trial(params, cfg, res, None, False))

    rets = pd.DataFrame({i: t.result.daily_returns for i, t in enumerate(trials)}).fillna(0.0)
    pbo = compute_pbo_cscv(rets) if rets.shape[1] >= 2 else float("nan")
    sr = rets.mean() / rets.std(ddof=1).replace(0, np.nan)
    sr_var = float(np.nanvar(sr.to_numpy(), ddof=1)) if len(sr) > 1 else None

    for t in trials:
        m = compute_performance_metrics(t.result.trades, t.result.equity_curve, len(trials), sr_var, pbo)
        t.metrics = m
        why: list[str] = []
        if m.total_trades < limits.min_trades:
            why.append(f"{m.total_trades} operações < {limits.min_trades}")
        if m.expectancy_r <= 0:
            why.append(f"esperança {m.expectancy_r:+.3f}R ≤ 0")
        if m.bootstrap_ci_lower_r <= 0:
            why.append(f"IC inferior {m.bootstrap_ci_lower_r:+.3f}R ≤ 0")
        if m.deflated_sharpe_prob < 0.95:
            why.append(f"DSR {m.deflated_sharpe_prob:.2f} < 0,95")
        if sum(x.net_pnl_brl for x in t.result.trades) <= 0 or m.max_single_trade_profit_pct > 0.20:
            why.append("lucro concentrado ou negativo")
        if not (np.isfinite(pbo) and pbo < 0.20):
            why.append(f"PBO da grade {pbo:.2f} ≥ 0,20")
        t.eligible, t.why_not = not why, why

    current_hash = strategy_config_hash(base)
    eligible = sorted((t for t in trials if t.eligible), key=lambda t: -t.metrics.deflated_sharpe_prob)
    candidate: Trial | None = None
    plateau: dict[str, float] = {}
    for t in eligible:
        if strategy_config_hash(t.cfg) == current_hash:
            continue  # the current configuration is not a "new" candidate
        ok, plateau = _plateau(wf, t.cfg, list(limits.grid), limits, bars, index_bars, universe_fn, plateau_variation)
        if ok:
            candidate = t
            break
        t.why_not.append("falhou no platô ±20%")

    changes = diff_params(base.model_dump(mode="json"), candidate.cfg.model_dump(mode="json")) if candidate else {}
    verdict = "PENDING" if candidate else "NONE_FOUND"
    report = _report(trials, candidate, changes, pbo, plateau, trigger_reason, current_hash)
    return ReoptResult(candidate.cfg if candidate else None, changes, trials, pbo, plateau, report, verdict)


def _plateau(wf: WalkForwardEngine, cfg: StrategyConfig, params: list[str], limits: Limits,
             bars: dict[str, pd.DataFrame], index_bars: pd.DataFrame,
             universe_fn: Callable[[date], list[str]] | None, var: float) -> tuple[bool, dict[str, float]]:
    out: dict[str, float] = {}
    for path in params:
        v0 = _get_path(cfg, path)
        lo, hi = limits.bounds.get(path, (None, None))
        for mult in (1 - var, 1 + var):
            c = cfg.model_copy(deep=True)
            v = v0 * mult
            if lo is not None:
                v = max(v, lo)
            if hi is not None:
                v = min(v, hi)
            _set_path(c, path, v)
            res = wf.run_config(c, bars, index_bars, universe_fn)
            out[f"{path}@{mult:.2f}"] = float(np.mean([x.r_multiple for x in res.trades])) if res.trades else float("nan")
    return all(np.isfinite(x) and x > 0 for x in out.values()), out


def _report(trials: list[Trial], cand: Trial | None, changes: dict[str, tuple[Any, Any]], pbo: float,
            plateau: dict[str, float], reason: str, current_hash: str) -> str:
    lines = [
        "# Reotimização da quarentena",
        "",
        f"**Gerado em:** {date.today().isoformat()}  ",
        f"**Motivo da quarentena:** {reason or '—'}  ",
        f"**Configuração atual:** `{current_hash}`  ",
        f"**Configurações testadas:** {len(trials)} (todas dentro de `strategy_default.yaml`)  ",
        f"**PBO da grade (CSCV):** {pbo:.3f}",
        "",
        f"## Resultado: {'CANDIDATA ENCONTRADA — aguarda sua aprovação' if cand else 'NENHUMA candidata passou nos critérios'}",
        "",
    ]
    if cand:
        m = cand.metrics
        lines += [
            f"Esperança **{m.expectancy_r:+.3f}R** (IC 95% [{m.bootstrap_ci_lower_r:+.3f}, {m.bootstrap_ci_upper_r:+.3f}]), "
            f"{m.total_trades} operações, DSR {m.deflated_sharpe_prob:.2f}, drawdown máx. {m.max_drawdown_pct:.1%}.",
            "",
            "| Parâmetro | Atual | Candidata |",
            "| --- | --- | --- |",
            *[f"| `{k}` | {a} | {b} |" for k, (a, b) in changes.items()],
            "",
            "Ao aprovar, a contagem do paper reinicia com a nova configuração (PRD 1.4.4).",
        ]
    else:
        lines += ["O sistema continua em quarentena: novas entradas seguem só na carteira sombra e a volta acontece "
                  "quando a curva sombra se recuperar (sem mudar parâmetros).", ""]
    lines += ["", "## Todas as configurações", "", "| # | Parâmetros | Operações | E[R] | IC inf. | DSR | Elegível | Motivo |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, t in enumerate(trials):
        m = t.metrics
        p = ", ".join(f"{k.split('.')[-2]}.{k.split('.')[-1]}={v}" for k, v in t.params.items())
        lines.append(f"| {i} | {p} | {m.total_trades} | {m.expectancy_r:+.3f} | {m.bootstrap_ci_lower_r:+.3f} | "
                     f"{m.deflated_sharpe_prob:.2f} | {'sim' if t.eligible else 'não'} | {'; '.join(t.why_not)} |")
    if plateau:
        lines += ["", "## Platô ±20% da candidata", ""]
        lines += [f"- {k}: E[R] {v:+.3f}" for k, v in plateau.items()]
    return "\n".join(lines) + "\n"
