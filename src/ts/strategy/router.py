"""Regime router: each setup only trades in the market regimes where it is supposed to have an edge.

Regime context of a decision (all causal, computed with bars <= t):

* **Diamond phase** of the asset (Lemos, 6 phases from close vs SMA50/SMA200);
* **Kaufman efficiency ratio** of the asset (trend vs chop);
* **Market breadth** of the tradable universe: share of stocks above their SMA50 and the McClellan oscillator
  (EMA19 - EMA39 of advancers minus decliners), computed from the stocks we already store — no new data source.

The context is attached to every signal (features ``phase``, ``er``, ``breadth_pct``, ``mcclellan``) whether or
not the router is enabled, so the regime report can measure each setup by regime before anything changes.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ts.core.config import RegimeRouterConfig
from ts.core.models import Signal

PHASE_NAMES = {1: "Recuperação", 2: "Acumulação", 3: "Altista", 4: "Aviso", 5: "Distribuição", 6: "Baixista"}


@dataclass(frozen=True)
class RegimeContext:
    phase: float
    er: float
    breadth_pct: float
    mcclellan: float

    def as_features(self) -> dict[str, float]:
        return {"phase": self.phase, "er": self.er, "breadth_pct": self.breadth_pct, "mcclellan": self.mcclellan}


def compute_breadth(prepared: Mapping[str, Any]) -> pd.DataFrame:
    """Daily breadth of the prepared universe: pct above SMA50, advancers, decliners, McClellan."""
    above: dict[date, list[int]] = {}
    adv: dict[date, int] = {}
    dec: dict[date, int] = {}
    for td in prepared.values():
        cols = td.view._cols
        if "sma50" not in cols:
            continue
        close, s50, r1 = cols["close"], cols["sma50"], cols["ret1"]
        for i, d in enumerate(td.dates):
            if np.isfinite(s50[i]):
                above.setdefault(d, [0, 0])
                above[d][0] += int(close[i] > s50[i])
                above[d][1] += 1
            if np.isfinite(r1[i]):
                if r1[i] > 0:
                    adv[d] = adv.get(d, 0) + 1
                elif r1[i] < 0:
                    dec[d] = dec.get(d, 0) + 1
    days = sorted(set(above) | set(adv) | set(dec))
    df = pd.DataFrame(index=pd.Index(days, name="session_date"))
    df["pct_above_sma50"] = [above[d][0] / above[d][1] if d in above and above[d][1] else np.nan for d in days]
    df["adv"] = [adv.get(d, 0) for d in days]
    df["dec"] = [dec.get(d, 0) for d in days]
    net = (df["adv"] - df["dec"]).astype(float)
    df["mcclellan"] = net.ewm(span=19, adjust=False).mean() - net.ewm(span=39, adjust=False).mean()
    return df


def context_for(td: Any, k: int, d: date, breadth: pd.DataFrame | None) -> RegimeContext:
    cols = td.view._cols
    bp = mc = float("nan")
    if breadth is not None and d in breadth.index:
        bp = float(breadth.at[d, "pct_above_sma50"])
        mc = float(breadth.at[d, "mcclellan"])
    return RegimeContext(float(cols["phase"][k]), float(cols["er"][k]), bp, mc)


def route(sig: Signal, ctx: RegimeContext, cfg: RegimeRouterConfig) -> str | None:
    """None if the setup may trade in this regime; otherwise the human-readable reason."""
    if cfg.breadth_min_pct_above_sma50 > 0 and math.isfinite(ctx.breadth_pct) \
            and ctx.breadth_pct < cfg.breadth_min_pct_above_sma50:
        return (f"Roteador de regime: amplitude fraca — {ctx.breadth_pct:.0%} das ações acima da MM50 "
                f"(< {cfg.breadth_min_pct_above_sma50:.0%})")
    rule = cfg.rules.get(sig.setup.value)
    if rule is None:
        return None
    if not math.isfinite(ctx.phase) or int(ctx.phase) not in rule.phases:
        name = PHASE_NAMES.get(int(ctx.phase), "indefinida") if math.isfinite(ctx.phase) else "indefinida"
        allowed = ", ".join(f"{p} {PHASE_NAMES[p]}" for p in rule.phases)
        return f"Roteador de regime: setup {sig.setup.value} não opera na fase {name} (permitidas: {allowed})"
    if rule.trend_only and not (math.isfinite(ctx.er) and ctx.er >= cfg.er_trend_min):
        return (f"Roteador de regime: setup {sig.setup.value} exige tendência — eficiência "
                f"{ctx.er:.2f} < {cfg.er_trend_min:.2f} (mercado lateral)")
    return None
