"""Raio-X checklist: every condition of the active setups for one ticker on the last closed bar.

Read-only explanation for the terminal ("why did / didn't it trigger?"). It reuses the strategy's own feature
frame, pivot replay and config, so the values shown are the ones the signal engine sees. It does NOT decide
anything: the signal engine stays the single source of truth (a test checks both agree on "fired").
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ts.core.config import StrategyConfig
from ts.strategy.regime import is_market_filter_bullish
from ts.strategy.router import PHASE_NAMES, RegimeContext, route
from ts.strategy.setups import SETUP_EVALUATORS
from ts.strategy.signal_engine import prepare_ticker

SETUP_NAMES = {"A": "Pivô de alta", "B": "Pullback", "C": "Donchian 20/10", "D": "Bollinger squeeze", "E": "Divergência",
               "F": "Reversão na baixa"}


def _f(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 4)


def _cond(label: str, ok: bool | None, value: str, limit: str, hint: str = "") -> dict[str, Any]:
    return {"label": label, "ok": ok, "value": value, "limit": limit, "hint": hint}


def _n(x: float, d: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _router(cfg: StrategyConfig, setup: str, phase: float, er: float) -> list[dict[str, Any]]:
    rc = cfg.regime_router
    if not rc.enabled or setup not in rc.rules:
        return []
    rule = rc.rules[setup]
    ph = int(phase) if phase == phase else None
    allowed = ", ".join(f"{p} {PHASE_NAMES[p]}" for p in rule.phases)
    out = [_cond("Roteador: fase do Diamante permitida", ph in rule.phases if ph else False,
                 f"{ph} {PHASE_NAMES.get(ph, '')}" if ph else "indefinida", allowed,
                 "Cada setup só opera nas fases em que tem vantagem")]
    if rule.trend_only:
        out.append(_cond("Roteador: mercado em tendência (eficiência de Kaufman)", er == er and er >= rc.er_trend_min,
                         _n(er), f"≥ {_n(rc.er_trend_min)}", "Abaixo do limite o preço anda de lado"))
    return out


def build_checklist(ticker: str, bars: pd.DataFrame, ibov: pd.DataFrame, cfg: StrategyConfig) -> dict[str, Any]:
    td = prepare_ticker(ticker, bars, cfg)
    k = len(td.dates) - 1
    asof: date = td.dates[k]
    fv = td.view.at_k(k)
    pivots = td.pivots.asof(asof)
    known = [p for p in pivots if p.confirmed_date <= asof]
    ib = ibov[ibov["session_date"] <= asof]
    c = fv.v("close")
    phase, er = fv.v("phase"), fv.v("er")

    ic = ib["close"].astype(float)
    ib_s200 = float(ic.rolling(cfg.market_filter.ibov_sma_long).mean().iloc[-1]) if len(ic) else float("nan")
    ib_s50 = float(ic.rolling(cfg.market_filter.ibov_sma_short).mean().iloc[-1]) if len(ic) else float("nan")
    market_ok = (not cfg.market_filter.require_bull_regime) or is_market_filter_bullish(ib, cfg.market_filter)
    common = [
        _cond("Mercado: IBOV acima da MM200 e MM50 > MM200", market_ok,
              f"IBOV {_n(float(ic.iloc[-1]) if len(ic) else float('nan'), 0)} · MM50 {_n(ib_s50, 0)} · MM200 {_n(ib_s200, 0)}",
              "maré primária de alta", "Com o IBOV em baixa nenhum setup compra"),
        _cond("Histórico suficiente", k + 1 >= cfg.min_history_bars, f"{k + 1} pregões", f"≥ {cfg.min_history_bars}"),
        _cond(f"Tendência do ativo: acima da MM{cfg.asset_trend_sma}", c > fv.v("sma_trend") if fv.v("sma_trend") == fv.v("sma_trend") else False,
              f"{_n(c)} vs {_n(fv.v('sma_trend'))}", "fechamento > média"),
    ]

    setups: list[dict[str, Any]] = []
    s = cfg.setups
    # ---------------------------------------------------------------- A
    if s.A.enabled:
        ca = s.A
        conds = []
        if len(known) >= 3:
            p1, p2, p3 = known[-3], known[-2], known[-1]
            shape = p1.kind == "LOW" and p2.kind == "HIGH" and p3.kind == "LOW"
            lab = {"LOW": "fundo", "HIGH": "topo"}
            conds.append(_cond("Estrutura: fundo → topo → fundo mais alto (pivôs confirmados)",
                               shape and p3.price > p1.price,
                               " → ".join(f"{lab[p.kind]} {_n(p.price)}" for p in (p1, p2, p3)),
                               "2º fundo acima do 1º", "Os três últimos pivôs confirmados do ativo"))
            if shape:
                conds.append(_cond("Rompimento: fechamento acima do topo", c > p2.price, _n(c), f"> {_n(p2.price)}",
                                   f"faltam {_n((p2.price / c - 1) * 100, 1)}%" if c <= p2.price else ""))
            else:
                conds.append(_cond("Rompimento: fechamento acima do topo", None, "—", "—",
                                   "Só se aplica quando a estrutura fundo → topo → fundo existe"))
        else:
            conds.append(_cond("Estrutura: fundo → topo → fundo mais alto", False, f"{len(known)} pivôs", "≥ 3 pivôs"))
        rv, ad = fv.v("rvol"), fv.v("adx")
        h_t, h_p = fv.v("macd_hist"), fv.v("macd_hist", -1)
        conds += [
            _cond("Volume relativo (RVOL)", rv == rv and rv >= ca.rvol_min, _n(rv), f"≥ {_n(ca.rvol_min)}", "Volume do dia ÷ média de 20 dias"),
            _cond("Força da tendência (ADX 14)", ad == ad and ad >= ca.adx_threshold, _n(ad, 1), f"≥ {_n(ca.adx_threshold, 0)}"),
            _cond("Momento: histograma do MACD positivo e subindo", h_t == h_t and h_p == h_p and h_t > 0 and h_t > h_p,
                  f"{_n(h_t, 3)} (ontem {_n(h_p, 3)})", "> 0 e > ontem"),
        ]
        conds += _router(cfg, "A", phase, er)
        setups.append({"id": "A", "name": SETUP_NAMES["A"], "enabled": True, "conditions": conds})
    # ---------------------------------------------------------------- B
    if s.B.enabled:
        cb = s.B
        sma_t, ema_t, low_t = fv.v("sma_b"), fv.v("ema_b"), fv.v("low")
        n_lower = 0
        for i in range(cb.pullback_bars_max + 1):
            if fv.v("high", -i) < fv.v("high", -i - 1):
                n_lower += 1
            else:
                break
        rsi = fv.v("rsi_b")
        recent = fv.window("adx", -cb.adx_recent_bars + 1, 1)
        adx_max = float(np.nanmax(recent)) if len(recent) and np.isfinite(recent).any() else float("nan")
        entry = fv.v("high") + cfg.risk.tick_size
        risk = entry - (low_t - cfg.risk.tick_size)
        above = [p.price for p in known if p.kind == "HIGH" and p.price > entry]
        rr = (min(above) - entry) / risk if above and risk > 0 else float("inf")
        conds = [
            _cond(f"Tendência: preço e MME{cfg.setups.B.ema_short} acima da MM{cb.sma_medium}",
                  c > sma_t and ema_t > sma_t, f"{_n(c)} · MME {_n(ema_t)} · MM {_n(sma_t)}", "ambos acima"),
            _cond("Correção: máximas descendentes seguidas", cb.pullback_bars_min <= n_lower <= cb.pullback_bars_max,
                  f"{n_lower} pregões", f"{cb.pullback_bars_min} a {cb.pullback_bars_max}"),
            _cond("Mínima do dia entre as duas médias", min(sma_t, ema_t) <= low_t <= max(sma_t, ema_t),
                  _n(low_t), f"{_n(min(sma_t, ema_t))} a {_n(max(sma_t, ema_t))}"),
            _cond(f"IFR {cb.rsi_period} sobrevendido no pullback", rsi == rsi and rsi <= cb.rsi_max, _n(rsi, 1), f"≤ {_n(cb.rsi_max, 0)}"),
            _cond(f"ADX ≥ {_n(cb.adx_threshold, 0)} em algum dos últimos {cb.adx_recent_bars} pregões",
                  adx_max == adx_max and adx_max >= cb.adx_threshold, f"máx. {_n(adx_max, 1)}", f"≥ {_n(cb.adx_threshold, 0)}"),
            _cond("Espaço até o próximo topo (retorno/risco)", rr >= cb.min_rr_filter,
                  "sem topo acima" if rr == float("inf") else f"{_n(rr, 1)} R", f"≥ {_n(cb.min_rr_filter, 0)} R"),
        ]
        conds += _router(cfg, "B", phase, er)
        setups.append({"id": "B", "name": SETUP_NAMES["B"], "enabled": True, "conditions": conds})
    for sid, sc in (("C", s.C), ("D", s.D), ("E", s.E), ("F", s.F)):
        setups.append({"id": sid, "name": SETUP_NAMES[sid], "enabled": bool(sc.enabled), "conditions": []})

    # Ground truth: what the signal engine itself says for this bar (the checklist only explains it).
    fired = set()
    if market_ok and k + 1 >= cfg.min_history_bars and c > fv.v("sma_trend"):
        for ev in SETUP_EVALUATORS:
            sig = ev(ticker, asof, fv, pivots, cfg, "raio-x")
            if sig is None:
                continue
            ctx = RegimeContext(phase, er, float("nan"), float("nan"))
            if cfg.regime_router.enabled and route(sig, ctx, cfg.regime_router):
                continue  # the engine would reject it in planning (REJECTED_REGIME)
            fired.add(sig.setup.value)
    for st in setups:
        conds = st["conditions"]
        st["passed"] = sum(1 for x in conds if x["ok"])
        st["total"] = len(conds)
        st["fired"] = st["id"] in fired
    return {"ticker": ticker, "date": asof.isoformat(), "close": _f(c), "common": common,
            "common_ok": all(x["ok"] for x in common), "setups": setups,
            "phase": None if phase != phase else int(phase), "er": _f(er)}


def checklist_for(root: Path, store: Any, corp_path: Path, ticker: str, ibov_symbol: str, cfg: StrategyConfig) -> dict[str, Any]:
    from ts.data.corporate_actions import CorporateActionsStore
    from ts.data.loader import load_signal_bars

    bars = load_signal_bars(store, [ticker, ibov_symbol], CorporateActionsStore(corp_path))
    if ticker not in bars or ibov_symbol not in bars:
        raise KeyError(ticker)
    return build_checklist(ticker, bars[ticker], bars[ibov_symbol], cfg)
