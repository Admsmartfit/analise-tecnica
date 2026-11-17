"""Regime laboratory: does the regime router add an edge out of sample? (``python -m ts backtest --regimes``)

1. Baseline backtest (router off) and theory-router backtest (rules from the reference books) — both with the
   full production engine, so the comparison includes portfolio effects (heat, clusters, freed capital).
2. Every trade is tagged with the regime at its decision: Diamond phase, Kaufman ER bucket, breadth bucket.
3. Learned map, walk-forward: on each 3-year in-sample window, keep the (setup, phase, ER bucket) cells with
   enough trades and positive expectancy; apply the map to the next, unseen year. This is a trade-level filter
   (an approximation: it ignores the capital a skipped trade would have freed), reported as such.

The report recommends enabling the router only when it improves out-of-sample results — otherwise it says so.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ts.backtest.engine import BacktestResult
from ts.backtest.metrics import compute_expectancy_bootstrap, compute_performance_metrics
from ts.backtest.walkforward import WalkForwardEngine
from ts.core.config import CostsConfig, StrategyConfig
from ts.strategy.router import PHASE_NAMES


@dataclass
class TaggedTrade:
    setup: str
    entry: date
    r: float
    phase: int | None
    er_bucket: str
    breadth_bucket: str


def tag_trades(res: BacktestResult, er_trend_min: float) -> list[TaggedTrade]:
    feats = {rec.signal.signal_id: rec.signal.features for rec in res.signals}
    out: list[TaggedTrade] = []
    for t in res.trades:
        f = feats.get(t.signal_id or "", {})
        ph = f.get("phase", float("nan"))
        er = f.get("er", float("nan"))
        bp = f.get("breadth_pct", float("nan"))
        out.append(TaggedTrade(
            t.setup, t.entry_date, t.r_multiple,
            int(ph) if isinstance(ph, float) and math.isfinite(ph) else None,
            "tendência" if math.isfinite(er) and er >= er_trend_min else ("lateral" if math.isfinite(er) else "n/d"),
            "ampla" if math.isfinite(bp) and bp >= 0.5 else ("estreita" if math.isfinite(bp) else "n/d"),
        ))
    return out


def _stats(rs: list[float]) -> tuple[int, float, float, float]:
    if not rs:
        return 0, float("nan"), float("nan"), float("nan")
    m, lo, _ = compute_expectancy_bootstrap(rs)
    return len(rs), m, lo, float(np.mean([x > 0 for x in rs]))


def _table(trades: list[TaggedTrade], key: Callable[[TaggedTrade], str], title: str) -> list[str]:
    groups: dict[tuple[str, str], list[float]] = {}
    for t in trades:
        groups.setdefault((t.setup, key(t)), []).append(t.r)
    lines = [f"### {title}", "", "| Setup | Regime | Operações | E[R] | IC 95% inf. | Acerto |", "| --- | --- | --- | --- | --- | --- |"]
    for (s, k), rs in sorted(groups.items()):
        n, m, lo, wr = _stats(rs)
        lines.append(f"| {s} | {k} | {n} | {m:+.3f} | {lo:+.3f} | {wr:.0%} |")
    return lines + [""]


def learned_map_walkforward(trades: list[TaggedTrade], sessions: list[date], train: int = 756, test: int = 252,
                            min_cell: int = 15) -> dict[str, Any]:
    def cell(t: TaggedTrade) -> tuple[str, int | None, str]:
        return (t.setup, t.phase, t.er_bucket)

    kept: list[float] = []
    all_oos: list[float] = []
    folds: list[dict[str, Any]] = []
    start = train
    while start < len(sessions):
        is_from, is_to = sessions[start - train], sessions[start - 1]
        oos_from, oos_to = sessions[start], sessions[min(start + test, len(sessions)) - 1]
        cells: dict[tuple[str, int | None, str], list[float]] = {}
        for t in trades:
            if is_from <= t.entry <= is_to:
                cells.setdefault(cell(t), []).append(t.r)
        allowed = {c for c, rs in cells.items() if len(rs) >= min_cell and np.mean(rs) > 0}
        oos = [t for t in trades if oos_from <= t.entry <= oos_to]
        k = [t.r for t in oos if cell(t) in allowed]
        kept += k
        all_oos += [t.r for t in oos]
        folds.append({"oos": (oos_from, oos_to), "allowed": sorted(allowed, key=str), "oos_trades": len(oos), "kept": len(k)})
        start += test
    return {"kept": kept, "all": all_oos, "folds": folds}


def final_learned_map(trades: list[TaggedTrade], min_cell: int = 15) -> dict[str, list[tuple[int | None, str, int, float]]]:
    cells: dict[tuple[str, int | None, str], list[float]] = {}
    for t in trades:
        cells.setdefault((t.setup, t.phase, t.er_bucket), []).append(t.r)
    out: dict[str, list[tuple[int | None, str, int, float]]] = {}
    for (s, ph, erb), rs in sorted(cells.items(), key=str):
        if len(rs) >= min_cell and np.mean(rs) > 0:
            out.setdefault(s, []).append((ph, erb, len(rs), float(np.mean(rs))))
    return out


def regime_report(
    bars: dict[str, pd.DataFrame],
    index_bars: pd.DataFrame,
    base: StrategyConfig,
    costs: CostsConfig | None = None,
    capital: float = 100000.0,
    universe_fn: Callable[[date], list[str]] | None = None,
    train_sessions: int = 756,
    test_sessions: int = 252,
    min_cell_trades: int = 15,
) -> tuple[str, dict[str, Any]]:
    wf = WalkForwardEngine(base, costs, capital)
    off = base.model_copy(deep=True)
    off.regime_router.enabled = False
    on = base.model_copy(deep=True)
    on.regime_router.enabled = True
    res_off = wf.run_config(off, bars, index_bars, universe_fn)
    res_on = wf.run_config(on, bars, index_bars, universe_fn)
    m_off = compute_performance_metrics(res_off.trades, res_off.equity_curve, 2)
    m_on = compute_performance_metrics(res_on.trades, res_on.equity_curve, 2)

    thr = base.regime_router.er_trend_min
    tagged = tag_trades(res_off, thr)
    sessions = list(res_off.equity_curve["session_date"]) if not res_off.equity_curve.empty else []
    lw = learned_map_walkforward(tagged, sessions, train_sessions, test_sessions, min_cell_trades)
    n_all, e_all, lo_all, _ = _stats(lw["all"])
    n_kept, e_kept, lo_kept, _ = _stats(lw["kept"])
    learned = final_learned_map(tagged, min_cell_trades)

    theory_better = m_on.expectancy_r > m_off.expectancy_r and m_on.bootstrap_ci_lower_r >= m_off.bootstrap_ci_lower_r
    learned_better = n_kept >= 30 and np.isfinite(e_kept) and e_kept > e_all and lo_kept > lo_all
    if theory_better and learned_better:
        verdict = ("**Recomendação: ligar o roteador (regras do tratado).** Melhora o backtest completo e o mapa aprendido "
                   "confirma fora da amostra. Ligar muda parâmetros: a contagem do paper reinicia.")
    elif theory_better:
        verdict = ("**Inconclusivo.** As regras do tratado melhoram o backtest completo, mas o mapa aprendido fora da amostra "
                   "não confirma. Manter desligado e reavaliar com mais dados.")
    elif learned_better:
        verdict = ("**Inconclusivo.** O mapa aprendido melhora fora da amostra, mas as regras fixas do tratado não. "
                   "Candidato a regras diferentes das do livro (ver mapa abaixo); manter desligado.")
    else:
        verdict = "**Recomendação: manter o roteador desligado.** Não há ganho fora da amostra com os dados atuais."

    def row(name: str, m: Any) -> str:
        return (f"| {name} | {m.total_trades} | {m.expectancy_r:+.3f} | {m.bootstrap_ci_lower_r:+.3f} | {m.win_rate:.0%} | "
                f"{m.max_drawdown_pct:.1%} | {m.profit_factor} |")

    lines = [
        "# Laboratório de regime — o roteador ajuda?",
        "",
        f"**Gerado em:** {date.today().isoformat()}  ",
        f"**Eficiência (Kaufman) para 'tendência':** ≥ {thr:.2f} · **Mínimo por célula aprendida:** {min_cell_trades} operações",
        "",
        verdict,
        "",
        "## 1. Backtest completo (motor de produção)",
        "",
        "| Configuração | Operações | E[R] | IC 95% inf. | Acerto | DD máx. | Profit factor |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        row("Sem roteador (atual)", m_off),
        row("Roteador com regras do tratado", m_on),
        "",
        "## 2. Mapa aprendido por walk-forward (3 anos aprendendo → 1 ano fora da amostra)",
        "",
        "| Fora da amostra | Operações | E[R] | IC 95% inf. |",
        "| --- | --- | --- | --- |",
        f"| Todas as operações | {n_all} | {e_all:+.3f} | {lo_all:+.3f} |",
        f"| Só as células aprovadas no período anterior | {n_kept} | {e_kept:+.3f} | {lo_kept:+.3f} |",
        "",
        "_Filtro aplicado operação a operação: ignora o capital que uma operação recusada liberaria._",
        "",
        "## 3. Cada setup por regime (backtest sem roteador)",
        "",
        *_table(tagged, lambda t: f"{t.phase} {PHASE_NAMES.get(t.phase, '?')}" if t.phase else "n/d", "Por fase do Diamante"),
        *_table(tagged, lambda t: t.er_bucket, "Por eficiência (tendência × lateral)"),
        *_table(tagged, lambda t: t.breadth_bucket, "Por amplitude do mercado (≥ 50% das ações acima da MM50 = ampla)"),
        "## 4. Mapa aprendido com todo o histórico (referência, não validado)",
        "",
    ]
    if learned:
        for s, cells in learned.items():
            desc = "; ".join(f"fase {ph} {PHASE_NAMES.get(ph, '?') if ph else ''} / {erb} ({n} op., {m:+.2f}R)"
                             for ph, erb, n, m in cells)
            lines.append(f"- **{s}:** {desc}")
    else:
        lines.append("- Nenhuma célula com operações suficientes e esperança positiva.")
    lines += ["", "## 5. Walk-forward do mapa", ""]
    for f in lw["folds"]:
        lines.append(f"- OOS {f['oos'][0]}→{f['oos'][1]}: {f['kept']}/{f['oos_trades']} operações mantidas; "
                     f"células: {', '.join(f'{c[0]}/fase {c[1]}/{c[2]}' for c in f['allowed']) or 'nenhuma'}")
    data = {"off": m_off, "on": m_on, "learned_oos": (n_kept, e_kept, lo_kept), "all_oos": (n_all, e_all, lo_all),
            "theory_better": theory_better, "learned_better": learned_better}
    return "\n".join(lines) + "\n", data
