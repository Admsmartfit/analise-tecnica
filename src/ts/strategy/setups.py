"""Setups A–E (PRD 1.1.3), evaluated on the decision bar t with closed bars only.

Each setup reads a :class:`FeatureView` positioned at t (adjusted prices) and the pivot *state as of t*.
Prices in the returned :class:`Signal` (entry_ref, stop, target) are converted to RAW prices of bar t
(raw = adjusted / adj_factor[t]) because they become order prices; the adjusted values used for the
decision are kept in ``features``.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np

from ts.core.config import StrategyConfig
from ts.core.models import PivotEvent, SetupId, Signal, TradeClass
from ts.strategy.features import FeatureView, ensure_view


def floor_tick(price: float, tick: float) -> float:
    return round(math.floor(round(price / tick, 6)) * tick, 8)


def ceil_tick(price: float, tick: float) -> float:
    return round(math.ceil(round(price / tick, 6)) * tick, 8)


def signal_id(setup: SetupId, ticker: str, asof: date, config_hash: str) -> str:
    """Deterministic id: the same decision always gets the same id (paridade / replays / UNIQUE in DB)."""
    return f"{setup.value}-{ticker}-{asof.strftime('%Y%m%d')}-{config_hash[:6]}"


def _nan(x: float) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _known(pivots: Sequence[PivotEvent], asof: date) -> list[PivotEvent]:
    return [p for p in pivots if p.confirmed_date <= asof]


def _make_signal(
    fv: FeatureView,
    setup: SetupId,
    trade_class: TradeClass,
    ticker: str,
    asof: date,
    entry_type: str,
    entry_adj: float,
    stop_adj: float,
    target_adj: float | None,
    features: dict[str, float],
    cfg: StrategyConfig,
    config_hash: str,
) -> Signal:
    tick = cfg.risk.tick_size
    factor = fv.v("adj_factor") if fv.has("adj_factor") else 1.0
    if _nan(factor) or factor <= 0:
        factor = 1.0
    entry_raw = entry_adj / factor
    entry_raw = ceil_tick(entry_raw, tick) if entry_type == "BUY_STOP" else round(entry_raw, 8)
    stop_raw = floor_tick(stop_adj / factor, tick)
    target_raw = ceil_tick(target_adj / factor, tick) if target_adj is not None else None
    feats = {k: float(v) for k, v in features.items()}
    feats["adj_factor"] = float(factor)
    feats["atr14_raw"] = fv.v("atr14") / factor
    if fv.has("gap_p01"):
        feats["gap_p01"] = fv.v("gap_p01")
    if fv.has("roc_rs"):
        feats["roc_rs"] = fv.v("roc_rs")
    return Signal(
        signal_id=signal_id(setup, ticker, asof, config_hash),
        ticker=ticker,
        setup=setup,
        trade_class=trade_class,
        decision_date=asof,
        entry_type=entry_type,  # type: ignore[arg-type]
        entry_ref=entry_raw,
        stop_price=stop_raw,
        target_price=target_raw,
        features=feats,
        config_hash=config_hash,
    )


def _rr_filter_ok(pivots: Sequence[PivotEvent], asof: date, entry: float, risk: float, min_rr: float) -> bool:
    """PRD 1.2.2: discard if the nearest confirmed pivot top above the entry is closer than ``min_rr`` R."""
    if min_rr <= 0:
        return True
    above = [p.price for p in _known(pivots, asof) if p.kind == "HIGH" and p.price > entry]
    if not above:
        return True
    return (min(above) - entry) / risk >= min_rr


# --------------------------------------------------------------------------------------------------
# Setup A — Pivô de Alta (Lemos): valley P1, peak P2, higher valley P3, all confirmed; C[t] > H[P2]
# --------------------------------------------------------------------------------------------------
def evaluate_setup_a(
    ticker: str, asof: date, data: Any, pivots: Sequence[PivotEvent], cfg: StrategyConfig, config_hash: str
) -> Signal | None:
    ca = cfg.setups.A
    fv = ensure_view(data, cfg)
    if not ca.enabled or fv.n_bars < 50:
        return None
    known = _known(pivots, asof)
    if len(known) < 3:
        return None
    p1, p2, p3 = known[-3], known[-2], known[-1]
    if not (p1.kind == "LOW" and p2.kind == "HIGH" and p3.kind == "LOW") or p3.price <= p1.price:
        return None

    c_t = fv.v("close")
    if c_t <= p2.price:
        return None
    rv, ad = fv.v("rvol"), fv.v("adx")
    h_t, h_p = fv.v("macd_hist"), fv.v("macd_hist", -1)
    if _nan(rv) or rv < ca.rvol_min or _nan(ad) or ad < ca.adx_threshold:
        return None
    if _nan(h_t) or _nan(h_p) or h_t <= 0 or h_t <= h_p:
        return None

    stop = p3.price - cfg.risk.tick_size
    return _make_signal(
        fv, SetupId.A_PIVOT, TradeClass.TREND, ticker, asof, "OPEN_AUCTION", c_t, stop, None,
        {"p1_price": p1.price, "p2_price": p2.price, "p3_price": p3.price, "rvol": rv, "adx": ad, "macd_hist": h_t},
        cfg, config_hash,
    )


# --------------------------------------------------------------------------------------------------
# Setup B — Pullback
# --------------------------------------------------------------------------------------------------
def evaluate_setup_b(
    ticker: str, asof: date, data: Any, pivots: Sequence[PivotEvent], cfg: StrategyConfig, config_hash: str
) -> Signal | None:
    cb = cfg.setups.B
    fv = ensure_view(data, cfg)
    if not cb.enabled or fv.n_bars < max(60, cb.sma_medium + cb.pullback_bars_max + 2):
        return None

    c_t, sma_t, ema_t = fv.v("close"), fv.v("sma_b"), fv.v("ema_b")
    if _nan(sma_t) or _nan(ema_t) or not (c_t > sma_t and ema_t > sma_t):
        return None

    # Correction: consecutive lower highs ending at t. Counted up to max+1 so a correction longer than
    # the maximum is rejected instead of being capped (previous version accepted 6+ bars as 5).
    n_lower = 0
    for i in range(cb.pullback_bars_max + 1):
        if fv.v("high", -i) < fv.v("high", -i - 1):
            n_lower += 1
        else:
            break
    if not (cb.pullback_bars_min <= n_lower <= cb.pullback_bars_max):
        return None

    low_t = fv.v("low")
    if not (min(sma_t, ema_t) <= low_t <= max(sma_t, ema_t)):
        return None

    rsi_t = fv.v("rsi_b")
    if _nan(rsi_t) or rsi_t > cb.rsi_max:
        return None

    # ADX >= threshold at some point of the last N sessions. NaN never counts as a pass.
    recent_adx = fv.window("adx", -cb.adx_recent_bars + 1, 1)
    if not np.any(np.nan_to_num(recent_adx, nan=-1.0) >= cb.adx_threshold):
        return None

    tick = cfg.risk.tick_size
    entry = fv.v("high") + tick
    stop = low_t - tick
    risk = entry - stop
    if risk <= 0:
        return None
    if not _rr_filter_ok(pivots, asof, entry, risk, cb.min_rr_filter):
        return None
    target = entry + cb.final_target_r * risk
    return _make_signal(
        fv, SetupId.B_PULLBACK, TradeClass.MEAN_REVERSION, ticker, asof, "BUY_STOP", entry, stop, target,
        {"correction_bars": n_lower, "rsi": rsi_t, "sma50": sma_t, "ema20": ema_t},
        cfg, config_hash,
    )


# --------------------------------------------------------------------------------------------------
# Setup C — Donchian 20/10
# --------------------------------------------------------------------------------------------------
def evaluate_setup_c(
    ticker: str, asof: date, data: Any, pivots: Sequence[PivotEvent], cfg: StrategyConfig, config_hash: str
) -> Signal | None:
    cc = cfg.setups.C
    fv = ensure_view(data, cfg)
    if not cc.enabled or fv.n_bars < cc.donchian_entry_period + 10:
        return None
    up, c_t = fv.v("donch_entry_up"), fv.v("close")
    if _nan(up) or c_t <= up * (1.0 + cc.breakout_min_pct):  # H2: penetration filter (0 = classic Donchian)
        return None
    ad, rv = fv.v("adx"), fv.v("rvol")
    if _nan(ad) or ad < cc.adx_threshold or _nan(rv) or rv < cc.rvol_min:
        return None
    exit_low = fv.v("donch_exit_low")
    if _nan(exit_low):
        return None
    return _make_signal(
        fv, SetupId.C_DONCHIAN, TradeClass.TREND, ticker, asof, "OPEN_AUCTION", c_t,
        exit_low - cfg.risk.tick_size, None,
        {"donchian_upper": up, "adx": ad, "rvol": rv},
        cfg, config_hash,
    )


# --------------------------------------------------------------------------------------------------
# Setup D — Bollinger Squeeze (squeeze at t-1, breakout at t)
# --------------------------------------------------------------------------------------------------
def evaluate_setup_d(
    ticker: str, asof: date, data: Any, pivots: Sequence[PivotEvent], cfg: StrategyConfig, config_hash: str
) -> Signal | None:
    cd = cfg.setups.D
    fv = ensure_view(data, cfg)
    lb = cd.squeeze_lookback
    if not cd.enabled or fv.n_bars < lb + cd.bollinger_period + 2:
        return None
    hist = fv.window("bb_bw", -lb, 0)  # BW[t-lb .. t-1]
    if len(hist) < lb or np.isnan(hist).any():
        return None
    q = float(np.percentile(hist, cd.squeeze_percentile_q, method="lower"))
    bw_prev = fv.v("bb_bw", -1)
    if bw_prev > q:
        return None
    c_t, up_t = fv.v("close"), fv.v("bb_upper")
    if _nan(up_t) or c_t <= up_t:
        return None
    rv = fv.v("rvol")
    if _nan(rv) or rv < cd.rvol_min:
        return None
    return _make_signal(
        fv, SetupId.D_SQUEEZE, TradeClass.TREND, ticker, asof, "OPEN_AUCTION", c_t,
        fv.v("bb_mid") - cfg.risk.tick_size, None,
        {"bw_prev": bw_prev, "bw_q": q, "upper_band": up_t, "rvol": rv},
        cfg, config_hash,
    )


# --------------------------------------------------------------------------------------------------
# Setup E — Bullish divergence between two confirmed pivot valleys
# --------------------------------------------------------------------------------------------------
def evaluate_setup_e(
    ticker: str, asof: date, data: Any, pivots: Sequence[PivotEvent], cfg: StrategyConfig, config_hash: str
) -> Signal | None:
    ce = cfg.setups.E
    fv = ensure_view(data, cfg)
    if not ce.enabled or fv.n_bars < 50:
        return None
    valleys = [p for p in _known(pivots, asof) if p.kind == "LOW"]
    if len(valleys) < 2:
        return None
    pa, pb = valleys[-2], valleys[-1]
    if pa.pivot_idx < 0 or pb.pivot_idx < 0 or pb.confirmed_idx < 0 or pb.confirmed_idx > fv.k:
        return None
    bars_between = pb.pivot_idx - pa.pivot_idx
    if not (ce.bars_between_pivots_min <= bars_between <= ce.bars_between_pivots_max):
        return None
    if pb.price > pa.price * ce.price_tolerance:
        return None
    rsi_pa, rsi_pb = fv.at("rsi_e", pa.pivot_idx), fv.at("rsi_e", pb.pivot_idx)
    if _nan(rsi_pa) or _nan(rsi_pb) or rsi_pb < rsi_pa + ce.rsi_delta_min:
        return None
    h_t, h_p = fv.v("macd_hist"), fv.v("macd_hist", -1)
    if _nan(h_t) or _nan(h_p) or h_t <= h_p:
        return None

    tick = cfg.risk.tick_size
    entry = fv.at("high", pb.confirmed_idx) + tick
    # Stale setup: if price already traded through the trigger after confirmation, the buy-stop would
    # simply fill at the next open far above the planned level. Only fresh triggers are valid.
    if fv.k > pb.confirmed_idx:
        after = fv.window("high", pb.confirmed_idx - fv.k + 1, 1)
        if len(after) and float(np.max(after)) >= entry:
            return None
    stop = pb.price - tick
    risk = entry - stop
    if risk <= 0 or not _rr_filter_ok(pivots, asof, entry, risk, ce.min_rr_filter):
        return None
    target = entry + ce.final_target_r * risk
    return _make_signal(
        fv, SetupId.E_DIVERGENCE, TradeClass.MEAN_REVERSION, ticker, asof, "BUY_STOP", entry, stop, target,
        {"pa_price": pa.price, "pb_price": pb.price, "rsi_pa": rsi_pa, "rsi_pb": rsi_pb, "bars_between": bars_between},
        cfg, config_hash,
    )


# --------------------------------------------------------------------------------------------------
# Setup F — Reversão em mercado de baixa (IFR2 de Connors, condicionado ao regime do IBOV)
# Lab 2026-11-17: só tem vantagem com o IBOV em BAIXA; em ALTA/LATERAL a mesma regra perde. Por isso o
# signal engine só chama este avaliador quando o mercado está em baixa (BEAR_EVALUATORS).
# --------------------------------------------------------------------------------------------------
def evaluate_setup_f(
    ticker: str, asof: date, data: Any, pivots: Sequence[PivotEvent], cfg: StrategyConfig, config_hash: str
) -> Signal | None:
    sf = cfg.setups.F
    fv = ensure_view(data, cfg)
    if not sf.enabled or fv.n_bars < 60:
        return None
    r, c_t, a = fv.v("rsi_f"), fv.v("close"), fv.v("atr14")
    if _nan(r) or r >= sf.rsi_max or _nan(a) or a <= 0:
        return None
    stop = c_t - sf.stop_atr_k * a
    if stop <= 0:
        return None
    return _make_signal(
        fv, SetupId.F_BEAR_REVERSION, TradeClass.MEAN_REVERSION, ticker, asof, "OPEN_AUCTION", c_t, stop, None,
        {"rsi2": r, "sma_exit": fv.v("sma_f")}, cfg, config_hash,
    )


# --------------------------------------------------------------------------------------------------
# Setup G — Força mensal + virada do MACD diário (lab multi-tempo 2026-11-17, "M2")
# --------------------------------------------------------------------------------------------------
def evaluate_setup_g(
    ticker: str, asof: date, data: Any, pivots: Sequence[PivotEvent], cfg: StrategyConfig, config_hash: str
) -> Signal | None:
    sg = cfg.setups.G
    fv = ensure_view(data, cfg)
    if not sg.enabled or fv.n_bars < 60:
        return None
    rm, h_t, h_p = fv.v("rsi_month"), fv.v("macd_hist"), fv.v("macd_hist", -1)
    if _nan(rm) or rm < sg.rsi_month_min or _nan(h_t) or _nan(h_p) or not (h_t > 0 >= h_p):
        return None
    c_t, a = fv.v("close"), fv.v("atr14")
    if _nan(a) or a <= 0 or c_t - sg.stop_atr_k * a <= 0:
        return None
    return _make_signal(
        fv, SetupId.G_MONTHLY_MACD, TradeClass.TREND, ticker, asof, "OPEN_AUCTION", c_t, c_t - sg.stop_atr_k * a,
        None, {"rsi_month": rm, "macd_hist": h_t}, cfg, config_hash,
    )


SETUP_EVALUATORS = (
    evaluate_setup_a,
    evaluate_setup_b,
    evaluate_setup_c,
    evaluate_setup_d,
    evaluate_setup_e,
    evaluate_setup_g,  # skipped here when G.all_regimes (then evaluated in every regime by the signal engine)
)
BEAR_EVALUATORS = (evaluate_setup_f,)  # only when the IBOV is in a bear regime (no asset-trend filter)
