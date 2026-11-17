"""Portfolio limits (PRD 1.2.3): heat, correlation-adjusted risk, clusters, beta, relative-strength ranking."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ts.core.config import PortfolioConfig
from ts.core.models import Signal
from ts.risk.sizing import SizingResult


def ledoit_wolf_correlation(returns: pd.DataFrame) -> pd.DataFrame:
    """Ledoit-Wolf (2004) shrinkage of the correlation matrix towards the identity.

    Works on standardized returns, so the sample covariance *is* the sample correlation; the optimal
    intensity delta = min(1, b2/d2) follows the closed form of Ledoit & Wolf, "A well-conditioned estimator
    for large-dimensional covariance matrices" with target mu*I (mu = 1 for standardized data).
    """
    x = returns.dropna(how="any")
    cols = list(returns.columns)
    p = len(cols)
    if p == 0:
        return pd.DataFrame()
    if len(x) < 3 or p == 1:
        return pd.DataFrame(np.eye(p), index=cols, columns=cols)
    std = x.std(ddof=0).replace(0, np.nan)
    z = ((x - x.mean()) / std).fillna(0.0).to_numpy()
    n = z.shape[0]
    s = z.T @ z / n
    mu = float(np.trace(s) / p)
    target = mu * np.eye(p)
    d2 = float(np.sum((s - target) ** 2)) / p
    b2_bar = sum(float(np.sum((np.outer(z[k], z[k]) - s) ** 2)) for k in range(n)) / (n * n) / p
    b2 = min(b2_bar, d2)
    delta = b2 / d2 if d2 > 0 else 1.0
    shrunk = delta * target + (1.0 - delta) * s
    d = np.sqrt(np.clip(np.diag(shrunk), 1e-12, None))
    corr = shrunk / np.outer(d, d)
    np.fill_diagonal(corr, 1.0)
    return pd.DataFrame(corr, index=cols, columns=cols)


def ledoit_wolf_correlation_shrinkage(corr_matrix: np.ndarray, shrinkage_weight: float = 0.20) -> np.ndarray:
    """Fixed-intensity shrinkage (kept for backward compatibility; prefer :func:`ledoit_wolf_correlation`)."""
    p = corr_matrix.shape[0]
    return ((1.0 - shrinkage_weight) * corr_matrix) + (shrinkage_weight * np.eye(p))


def cluster_tickers(returns_df: pd.DataFrame, threshold: float = 0.70) -> dict[str, int]:
    """Single-linkage hierarchical clustering cut at correlation ``threshold`` (connected components)."""
    tickers = list(returns_df.columns)
    n = len(tickers)
    if n <= 1:
        return {t: i for i, t in enumerate(tickers)}
    corr = returns_df.corr().to_numpy()
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if np.isfinite(corr[i, j]) and corr[i, j] >= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    roots = sorted({find(i) for i in range(n)})
    root_map = {r: k for k, r in enumerate(roots)}
    return {tickers[i]: root_map[find(i)] for i in range(n)}


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    signal: Signal
    sizing: SizingResult
    disposition: str  # ACCEPTED, REJECTED_CLUSTER, REJECTED_HEAT, REJECTED_CORR, REJECTED_BETA, REJECTED_DUPLICATE
    reason: str | None = None


class PortfolioRiskManager:
    """Applies heat, correlation-adjusted risk, cluster and beta limits to sized candidates."""

    def __init__(self, config: PortfolioConfig | None = None) -> None:
        self.config = config or PortfolioConfig()

    def evaluate_candidates(
        self,
        signals: list[tuple[Signal, SizingResult]],
        equity: float,
        open_positions: Sequence[dict[str, float | str]],
        returns_history: pd.DataFrame,
        ibov_returns: pd.Series | None = None,
        asset_betas: dict[str, float] | None = None,
        max_heat_pct: float | None = None,
    ) -> list[CandidateDecision]:
        """Rank by relative strength and accept candidates while every limit holds.

        ``open_positions`` items: ``ticker``, ``risk_brl`` (current: (price - stop) * qty, floored at 0)
        and ``capital_allocated`` (market value).
        """
        if not signals:
            return []
        cfg = self.config
        heat_cap = equity * (cfg.max_heat_pct if max_heat_pct is None else max_heat_pct)
        ibov_perf = float((1 + ibov_returns.fillna(0)).prod() - 1) if ibov_returns is not None and len(ibov_returns) else 0.0

        def rs(sig: Signal) -> float:
            if "roc_rs" in sig.features and np.isfinite(sig.features["roc_rs"]):
                return sig.features["roc_rs"] - ibov_perf
            if sig.ticker in returns_history.columns:
                return float((1 + returns_history[sig.ticker].fillna(0)).prod() - 1) - ibov_perf
            return -np.inf

        # Desempate: maior força relativa; ties broken by ticker/setup for determinism.
        ranked = sorted(signals, key=lambda p: (-rs(p[0]), p[0].ticker, p[0].setup.value))

        open_tickers = [str(p.get("ticker", "")) for p in open_positions]
        universe_cols = list(dict.fromkeys([*open_tickers, *[s.ticker for s, _ in ranked]]))
        rets = returns_history.reindex(columns=[c for c in universe_cols if c in returns_history.columns])
        clusters = cluster_tickers(rets.dropna(how="all"), cfg.cluster_correlation_threshold) if not rets.empty else {}
        next_id = (max(clusters.values()) + 1) if clusters else 0
        for t in universe_cols:  # tickers without return history get their own singleton cluster
            if t not in clusters:
                clusters[t] = next_id
                next_id += 1

        cluster_counts: dict[int, int] = {}
        for t in open_tickers:
            cluster_counts[clusters[t]] = cluster_counts.get(clusters[t], 0) + 1

        risks: dict[str, float] = {}
        alloc: dict[str, float] = {}
        for p in open_positions:
            t = str(p.get("ticker", ""))
            risks[t] = risks.get(t, 0.0) + max(0.0, float(p.get("risk_brl", 0.0)))
            alloc[t] = alloc.get(t, 0.0) + float(p.get("capital_allocated", 0.0))
        heat = sum(risks.values())

        corr = ledoit_wolf_correlation(rets) if rets.shape[1] > 0 else pd.DataFrame()

        def corr_risk(r: dict[str, float]) -> float:
            names = list(r)
            vec = np.array([r[n] for n in names])
            m = np.eye(len(names))
            for i, a in enumerate(names):
                for j, b in enumerate(names):
                    if i != j and a in corr.index and b in corr.index:
                        m[i, j] = corr.at[a, b]
            return float(np.sqrt(max(0.0, vec @ m @ vec)))

        decisions: list[CandidateDecision] = []
        taken_tickers: set[str] = set(open_tickers)
        for sig, sizing in ranked:
            t = sig.ticker
            if t in taken_tickers:
                decisions.append(CandidateDecision(sig, sizing, "REJECTED_DUPLICATE", "ticker already held or chosen today"))
                continue
            c_id = clusters[t]
            if cluster_counts.get(c_id, 0) >= cfg.max_positions_per_cluster:
                decisions.append(CandidateDecision(sig, sizing, "REJECTED_CLUSTER", f"cluster {c_id} full"))
                continue
            if heat + sizing.risk_brl > heat_cap + 1e-9:
                decisions.append(CandidateDecision(sig, sizing, "REJECTED_HEAT",
                                                   f"heat {(heat + sizing.risk_brl) / equity:.2%} > {heat_cap / equity:.2%}"))
                continue
            trial = dict(risks)
            trial[t] = trial.get(t, 0.0) + sizing.risk_brl
            cr = corr_risk(trial)
            if cr > equity * cfg.max_correlated_risk_pct + 1e-9:
                decisions.append(CandidateDecision(sig, sizing, "REJECTED_CORR",
                                                   f"correlated risk {cr / equity:.2%} > {cfg.max_correlated_risk_pct:.2%}"))
                continue
            if asset_betas is not None and equity > 0:
                trial_alloc = dict(alloc)
                trial_alloc[t] = trial_alloc.get(t, 0.0) + sizing.capital_allocated_brl
                beta = sum(v / equity * asset_betas.get(k, 1.0) for k, v in trial_alloc.items())
                if beta > cfg.max_beta + 1e-9:
                    decisions.append(CandidateDecision(sig, sizing, "REJECTED_BETA", f"portfolio beta {beta:.2f} > {cfg.max_beta:.2f}"))
                    continue
            decisions.append(CandidateDecision(sig, sizing, "ACCEPTED"))
            taken_tickers.add(t)
            risks = trial
            alloc[t] = alloc.get(t, 0.0) + sizing.capital_allocated_brl
            heat += sizing.risk_brl
            cluster_counts[c_id] = cluster_counts.get(c_id, 0) + 1
        return decisions

    def filter_and_rank_candidates(
        self,
        signals: list[tuple[Signal, SizingResult]],
        equity: float,
        open_positions: Sequence[dict[str, float | str]],
        returns_history_60d: pd.DataFrame,
        ibov_returns_60d: pd.Series | None = None,
        asset_betas_120d: dict[str, float] | None = None,
        max_heat_pct: float | None = None,
    ) -> list[tuple[Signal, SizingResult]]:
        """Backward-compatible wrapper returning only the accepted pairs."""
        decisions = self.evaluate_candidates(
            signals, equity, open_positions, returns_history_60d, ibov_returns_60d, asset_betas_120d, max_heat_pct
        )
        return [(d.signal, d.sizing) for d in decisions if d.disposition == "ACCEPTED"]


def compute_betas(returns: pd.DataFrame, ibov_returns: pd.Series) -> dict[str, float]:
    """OLS beta of each column against the IBOV over the given (already windowed) returns."""
    out: dict[str, float] = {}
    var = float(ibov_returns.var(ddof=0)) if len(ibov_returns) > 2 else 0.0
    for c in returns.columns:
        joined = pd.concat([returns[c], ibov_returns], axis=1).dropna()
        if len(joined) < 20 or var <= 0:
            out[c] = 1.0
            continue
        cov = float(np.cov(joined.iloc[:, 0], joined.iloc[:, 1], ddof=0)[0, 1])
        v = float(joined.iloc[:, 1].var(ddof=0))
        out[c] = cov / v if v > 0 else 1.0
    return out
