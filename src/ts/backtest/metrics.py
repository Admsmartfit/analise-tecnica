"""Performance metrics, bootstrap CIs, Deflated Sharpe Ratio, PBO (CSCV) and the G0 gate (PRD 1.4)."""
from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ts.execution.session_engine import CompletedTrade

EULER_GAMMA = 0.5772156649015329


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |err| < 1.2e-9)."""
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    p_low = 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= 1.0 - p_low:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)


def calc_skew(series: Sequence[float] | pd.Series) -> float:
    x = np.asarray(series, dtype=float)
    if len(x) < 3:
        return 0.0
    m2 = np.mean((x - x.mean()) ** 2)
    return float(np.mean((x - x.mean()) ** 3) / m2**1.5) if m2 > 0 else 0.0


def calc_kurtosis(series: Sequence[float] | pd.Series) -> float:
    """Pearson kurtosis (normal = 3)."""
    x = np.asarray(series, dtype=float)
    if len(x) < 4:
        return 3.0
    m2 = np.mean((x - x.mean()) ** 2)
    return float(np.mean((x - x.mean()) ** 4) / m2**2) if m2 > 0 else 3.0


def compute_expectancy_bootstrap(r_multiples: Sequence[float], n_boot: int = 5000,
                                 confidence: float = 0.95, seed: int = 42) -> tuple[float, float, float]:
    """Mean R and percentile-bootstrap CI (deterministic seed)."""
    arr = np.asarray(list(r_multiples), dtype=float)
    if len(arr) == 0:
        return 0.0, 0.0, 0.0
    mean_r = float(arr.mean())
    if len(arr) < 5:
        return mean_r, mean_r, mean_r
    rng = np.random.default_rng(seed)
    means = rng.choice(arr, size=(n_boot, len(arr)), replace=True).mean(axis=1)
    alpha = (1.0 - confidence) / 2.0
    return mean_r, float(np.quantile(means, alpha)), float(np.quantile(means, 1.0 - alpha))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] of ``n_trials`` independent zero-skill strategies (Bailey & López de Prado 2014)."""
    if n_trials <= 1:
        return 0.0
    z = (1.0 - EULER_GAMMA) * norm_ppf(1.0 - 1.0 / n_trials) + EULER_GAMMA * norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(max(sr_variance, 0.0)) * z


def compute_deflated_sharpe_ratio(
    sharpe: float,
    n_trials: int,
    t_observations: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    sr_variance_across_trials: float | None = None,
) -> float:
    """Probability that the true (per-period) Sharpe exceeds the best Sharpe expected by luck.

    ``sharpe`` must be the *per-observation* (non-annualized) Sharpe of the same frequency as
    ``t_observations``. Without the cross-trial variance, the null variance 1/(T-1) is used.
    """
    if t_observations <= 2:
        return 0.0
    var_trials = sr_variance_across_trials if sr_variance_across_trials and sr_variance_across_trials > 0 else 1.0 / (t_observations - 1)
    sr0 = expected_max_sharpe(n_trials, var_trials)
    denom = 1.0 - skewness * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe**2
    if denom <= 0:
        return 0.0
    z = (sharpe - sr0) * math.sqrt(t_observations - 1) / math.sqrt(denom)
    return norm_cdf(z)


def compute_pbo_cscv(returns_matrix: pd.DataFrame | np.ndarray, n_splits: int = 16) -> float:
    """Probability of Backtest Overfitting via Combinatorially Symmetric Cross-Validation.

    ``returns_matrix``: T x N per-period returns, one column per tested configuration. For each of the
    C(S, S/2) splits the best in-sample config (by Sharpe) is ranked out-of-sample; PBO is the fraction of
    splits where its OOS rank falls below the median (logit <= 0). Bailey, Borwein, López de Prado, Zhu (2016).
    """
    m = np.asarray(returns_matrix, dtype=float)
    if m.ndim != 2 or m.shape[1] < 2:
        return float("nan")
    t, n = m.shape
    s = n_splits - (n_splits % 2)
    s = max(2, min(s, (t // 2) * 2))
    blocks = np.array_split(np.arange(t), s)

    def sharpe(x: np.ndarray) -> np.ndarray:
        sd = x.std(axis=0, ddof=1)
        mu = x.mean(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(sd > 0, mu / sd, 0.0)
        return out

    logits = []
    for combo in itertools.combinations(range(s), s // 2):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = np.concatenate([blocks[i] for i in range(s) if i not in combo])
        best = int(np.argmax(sharpe(m[is_idx])))
        oos = sharpe(m[oos_idx])
        rank = (np.sum(oos < oos[best]) + 0.5 * (np.sum(oos == oos[best]) - 1) + 1) / (n + 1)
        rank = min(max(rank, 1e-9), 1 - 1e-9)
        logits.append(math.log(rank / (1 - rank)))
    return float(np.mean(np.asarray(logits) <= 0.0))


def max_consecutive_losses(r: Sequence[float]) -> int:
    best = cur = 0
    for x in r:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    total_trades: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    bootstrap_ci_lower_r: float
    bootstrap_ci_upper_r: float
    max_drawdown_pct: float
    sharpe_ratio: float  # annualized, report only (PRD 1.4.1)
    deflated_sharpe_prob: float
    pbo: float
    max_single_trade_profit_pct: float
    cvar_5pct_r: float
    ulcer_index: float
    calmar_ratio: float
    r_skew: float = 0.0
    r_kurtosis: float = 3.0
    max_win_r: float = 0.0
    max_loss_r: float = 0.0
    max_consecutive_losses: int = 0
    cost_over_gross_profit: float = 0.0
    exposure_pct: float = 0.0
    avg_mae_r: float = 0.0
    avg_mfe_r: float = 0.0
    losses_below_minus_1_5r: int = 0
    by_setup: dict[str, dict[str, float]] = field(default_factory=dict)


def compute_performance_metrics(
    trades: list[CompletedTrade],
    equity_curve: pd.DataFrame,
    n_trials_tested: int = 1,
    sr_variance_across_trials: float | None = None,
    pbo: float | None = None,
) -> PerformanceMetrics:
    if not trades:
        return PerformanceMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0 if pbo is None else pbo,
                                  0.0, 0.0, 0.0, 0.0)

    r_vals = [t.r_multiple for t in trades]
    wins = [t for t in trades if t.net_pnl_brl > 0]
    losses = [t for t in trades if t.net_pnl_brl < 0]
    win_rate = len(wins) / len(trades)
    tot_gain = sum(w.net_pnl_brl for w in wins)
    tot_loss = abs(sum(t.net_pnl_brl for t in losses))
    pf = tot_gain / tot_loss if tot_loss > 0 else (float("inf") if tot_gain > 0 else 0.0)
    mean_r, ci_lo, ci_hi = compute_expectancy_bootstrap(r_vals)

    # Concentration (Pring cap. 34): largest single trade vs TOTAL net profit.
    total_net = sum(t.net_pnl_brl for t in trades)
    max_gain = max((t.net_pnl_brl for t in trades), default=0.0)
    max_single_pct = (max_gain / total_net) if total_net > 0 else 1.0

    # CVaR 5%: mean of the worst 5% of trades (not the 5th percentile, which is VaR).
    sorted_r = np.sort(np.asarray(r_vals))
    k = max(1, int(math.ceil(0.05 * len(sorted_r))))
    cvar_5 = float(sorted_r[:k].mean())

    max_dd = sharpe_ann = ulcer = calmar = exposure = 0.0
    dsr = 0.0
    if not equity_curve.empty and "equity" in equity_curve.columns:
        eq = equity_curve["equity"].astype(float)
        dd = equity_curve["drawdown_pct"] if "drawdown_pct" in equity_curve.columns else (eq.cummax() - eq) / eq.cummax()
        max_dd = float(dd.max())
        ulcer = math.sqrt(float((dd**2).mean()))
        rets = eq.pct_change().dropna()
        if len(rets) > 5 and rets.std() > 0:
            sr = float(rets.mean() / rets.std())
            sharpe_ann = sr * math.sqrt(252)
            dsr = compute_deflated_sharpe_ratio(sr, max(1, n_trials_tested), len(rets), calc_skew(rets),
                                                calc_kurtosis(rets), sr_variance_across_trials)
        years = max(len(eq) / 252.0, 1e-9)
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if eq.iloc[0] > 0 and eq.iloc[-1] > 0 else -1.0
        calmar = cagr / max_dd if max_dd > 0 else 0.0
        if "open_positions" in equity_curve.columns:
            exposure = float((equity_curve["open_positions"] > 0).mean())

    gross_profit = sum(t.pnl_brl for t in trades if t.pnl_brl > 0)
    costs = sum(t.fees_brl for t in trades)

    by_setup: dict[str, dict[str, float]] = {}
    for s in sorted({t.setup for t in trades}):
        rs = [t.r_multiple for t in trades if t.setup == s]
        m, lo, hi = compute_expectancy_bootstrap(rs)
        by_setup[s] = {"trades": float(len(rs)), "expectancy_r": m, "ci_lower": lo, "ci_upper": hi,
                       "win_rate": float(np.mean([x > 0 for x in rs]))}

    return PerformanceMetrics(
        total_trades=len(trades),
        win_rate=round(win_rate, 4),
        profit_factor=round(pf, 2) if math.isfinite(pf) else pf,
        expectancy_r=round(mean_r, 4),
        bootstrap_ci_lower_r=round(ci_lo, 4),
        bootstrap_ci_upper_r=round(ci_hi, 4),
        max_drawdown_pct=round(max_dd, 4),
        sharpe_ratio=round(sharpe_ann, 2),
        deflated_sharpe_prob=round(dsr, 4),
        pbo=float("nan") if pbo is None else round(pbo, 4),
        max_single_trade_profit_pct=round(max_single_pct, 4),
        cvar_5pct_r=round(cvar_5, 3),
        ulcer_index=round(ulcer, 4),
        calmar_ratio=round(calmar, 2),
        r_skew=round(calc_skew(r_vals), 3),
        r_kurtosis=round(calc_kurtosis(r_vals), 3),
        max_win_r=round(max(r_vals), 3),
        max_loss_r=round(min(r_vals), 3),
        max_consecutive_losses=max_consecutive_losses(r_vals),
        cost_over_gross_profit=round(costs / gross_profit, 4) if gross_profit > 0 else float("inf"),
        exposure_pct=round(exposure, 4),
        avg_mae_r=round(float(np.mean([t.mae_r for t in trades])), 3),
        avg_mfe_r=round(float(np.mean([t.mfe_r for t in trades])), 3),
        losses_below_minus_1_5r=sum(1 for x in r_vals if x < -1.5),
        by_setup=by_setup,
    )


def validate_gate_g0(
    metrics: PerformanceMetrics,
    plateau_passed: bool | None = None,
    regimes_passed: bool | None = None,
) -> dict[str, Any]:
    """Every PRD 1.4.2 G0 criterion is mandatory. A criterion not evaluated (None) fails the gate."""
    checks: dict[str, Any] = {
        "expectancy_positive": metrics.expectancy_r > 0.0,
        "bootstrap_ci_lower_positive": metrics.bootstrap_ci_lower_r > 0.0,
        "dsr_passed": metrics.deflated_sharpe_prob >= 0.95,
        "pbo_passed": bool(np.isfinite(metrics.pbo) and metrics.pbo < 0.20),
        "concentration_passed": metrics.max_single_trade_profit_pct <= 0.20,
        "plateau_passed": bool(plateau_passed),
        "regimes_passed": bool(regimes_passed),
    }
    checks["overall_g0_approved"] = all(checks.values())
    return checks
