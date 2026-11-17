"""Position sizing and stop distance (PRD 1.2.1).

D = clip(P_e - S_tec, k_min*ATR14, k_max*ATR14), discarding the trade when the technical stop is farther
than k_max*ATR (never shortening it to a meaningless level). Q = floor(R / D) rounded down to the lot,
then capped by capital (Q*P <= 20% PL) and gap tail risk (Q*P*|gap_p01| <= 2% PL).

Invariants (property-tested): Q*D_effective <= R target; Q*P <= capital cap; Q*P*|gap| <= gap cap;
Q is a multiple of the lot when fractional trading is off.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from ts.core.config import RiskConfig


@dataclass(frozen=True, slots=True)
class SizingResult:
    accepted: bool
    qty: int
    entry_price: float
    stop_price: float
    distance_r: float
    risk_brl: float
    capital_allocated_brl: float
    disposition: Literal["ACCEPTED", "REJECTED_SIZE", "REJECTED_HEAT"]
    rejection_reason: str | None = None
    target_risk_brl: float = 0.0
    risk_efficiency: float = 0.0  # actual risk / target risk
    note: str | None = None  # e.g. rounding loss above the PRD threshold


def _reject(entry: float, stop: float, dist: float, reason: str) -> SizingResult:
    return SizingResult(False, 0, entry, stop, dist, 0.0, 0.0, "REJECTED_SIZE", reason)


def calculate_position_size(
    entry_price: float,
    technical_stop: float,
    current_atr14: float,
    equity: float,
    risk_cfg: RiskConfig,
    risk_pct_override: float | None = None,
    gap_p01: float | None = None,
    fractional: bool = False,
) -> SizingResult:
    """Deterministic position size and stop for a long entry."""
    tick = risk_cfg.tick_size
    risk_pct = risk_pct_override if risk_pct_override is not None else risk_cfg.risk_per_trade_pct
    gap = abs(gap_p01) if gap_p01 is not None and math.isfinite(gap_p01) and gap_p01 != 0 else risk_cfg.default_gap_p01

    if not (math.isfinite(entry_price) and entry_price > 0):
        return _reject(entry_price, technical_stop, 0.0, "Invalid entry price")
    if not (math.isfinite(current_atr14) and current_atr14 > 0):
        return _reject(entry_price, technical_stop, 0.0, "ATR14 unavailable (warm-up or bad data)")
    if risk_pct <= 0 or equity <= 0:
        return _reject(entry_price, technical_stop, 0.0, "Risk budget is zero (regime / circuit breaker)")

    raw_dist = entry_price - technical_stop
    if raw_dist <= 0:
        return _reject(entry_price, technical_stop, 0.0, "Technical stop is equal to or higher than entry price")

    max_dist = risk_cfg.stop_atr_max_k * current_atr14
    if raw_dist > max_dist + 1e-12:
        return _reject(
            entry_price, technical_stop, raw_dist,
            f"Technical stop distance ({raw_dist:.2f}) exceeds {risk_cfg.stop_atr_max_k:.1f}x ATR ({max_dist:.2f})",
        )

    dist = max(raw_dist, risk_cfg.stop_atr_min_k * current_atr14)
    # Stop on the tick grid, rounded DOWN (farther), then use the *effective* distance for Q so the
    # realized risk never exceeds the target because of rounding.
    stop = math.floor(round((entry_price - dist) / tick, 6)) * tick
    stop = round(stop, 8)
    eff_dist = entry_price - stop
    if stop <= 0 or eff_dist <= 0:
        return _reject(entry_price, stop, eff_dist, "Stop below zero")

    r_target = equity * risk_pct
    lot = 1 if fractional else risk_cfg.round_lot

    def to_lot(q: float) -> int:
        return int(math.floor(q / lot)) * lot

    q = to_lot(r_target / eff_dist)
    if q <= 0:
        return _reject(entry_price, stop, eff_dist, f"Target risk R${r_target:.2f} below one lot at distance R${eff_dist:.2f}")

    cap_q = to_lot(equity * risk_cfg.capital_cap_pct / entry_price)
    if q > cap_q:
        q = cap_q
    if q <= 0:
        return _reject(entry_price, stop, eff_dist, f"Position exceeds capital cap ({risk_cfg.capital_cap_pct:.0%} equity)")

    gap_q = to_lot(equity * risk_cfg.gap_cap_pct / (entry_price * gap))
    if q > gap_q:
        q = gap_q
    if q <= 0:
        return _reject(entry_price, stop, eff_dist, f"Position exceeds opening-gap tail cap ({risk_cfg.gap_cap_pct:.0%} equity)")

    actual_risk = q * eff_dist
    efficiency = actual_risk / r_target if r_target > 0 else 0.0
    note = None
    if efficiency < risk_cfg.min_risk_efficiency:
        note = f"Rounding/caps reduced risk to {efficiency:.0%} of target"
    return SizingResult(
        accepted=True,
        qty=q,
        entry_price=entry_price,
        stop_price=stop,
        distance_r=eff_dist,
        risk_brl=round(actual_risk, 2),
        capital_allocated_brl=round(q * entry_price, 2),
        disposition="ACCEPTED",
        target_risk_brl=round(r_target, 2),
        risk_efficiency=round(efficiency, 4),
        note=note,
    )
