"""Position exits (PRD 1.2.2): stop, partial at 1.5R, target 3R, time stop, Chandelier/Donchian trailing,
stagnation. Same function is used by the backtest and the paper engine.

Execution timing (decision with closed bar, execution at next auction — PRD 1.1.1):
* Stop, partial and target are resting orders: they execute *intrabar* on the bar being evaluated, the
  stop at ``min(open, stop)`` to reflect gaps (PRD 1.2.2 "Execução do stop").
* Time stop and stagnation are only known after the close, so they are returned with
  ``at_next_open=True`` and executed in the next session's opening auction.
* Trailing updates computed with bar t only protect from bar t+1 on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from ts.core.config import StrategyConfig
from ts.core.models import ExitReason, TradeClass


@dataclass(frozen=True, slots=True)
class ExitParams:
    partial_target_r: float | None = None
    partial_pct: float = 0.5
    final_target_r: float | None = None
    time_stop_bars: int | None = None
    trailing: Literal["CHANDELIER", "DONCHIAN", "NONE"] = "NONE"
    trailing_activation_r: float = 1.0
    chandelier_k: float = 3.0
    stagnation_bars: int | None = None
    stagnation_mfe_r: float = 1.0
    lot: int = 100
    breakeven_cost_pct: float = 0.001
    breakeven_after_r: float | None = None  # trend trades: floor the stop at entry + costs once MFE >= this


def exit_params_for(setup: str, cfg: StrategyConfig, breakeven_cost_pct: float = 0.001, fractional: bool = False) -> ExitParams:
    lot = 1 if fractional else cfg.risk.round_lot
    s = cfg.setups
    if setup == "A":
        return ExitParams(trailing="CHANDELIER", chandelier_k=s.A.trailing_chandelier_k,
                          stagnation_bars=s.A.stagnation_bars, stagnation_mfe_r=s.A.stagnation_mfe_r, lot=lot,
                          breakeven_after_r=s.A.breakeven_after_r or None, breakeven_cost_pct=breakeven_cost_pct)
    if setup == "C":
        # Donchian exit (10-day low) is the setup's own trailing and is always active.
        return ExitParams(trailing="DONCHIAN", trailing_activation_r=0.0,
                          stagnation_bars=s.C.stagnation_bars, stagnation_mfe_r=s.C.stagnation_mfe_r, lot=lot)
    if setup == "D":
        return ExitParams(trailing="CHANDELIER", chandelier_k=s.D.trailing_chandelier_k,
                          stagnation_bars=s.D.stagnation_bars, stagnation_mfe_r=s.D.stagnation_mfe_r, lot=lot)
    mr = s.B if setup == "B" else s.E
    return ExitParams(partial_target_r=mr.partial_target_r, partial_pct=mr.partial_pct,
                      final_target_r=mr.final_target_r, time_stop_bars=mr.time_stop_bars,
                      lot=lot, breakeven_cost_pct=breakeven_cost_pct)


@dataclass
class PositionRecord:
    position_id: str
    ticker: str
    setup: str
    trade_class: TradeClass
    entry_date: date
    entry_price: float
    initial_stop: float
    current_stop: float
    target_price: float | None
    qty_initial: int
    qty_open: int
    risk_r: float  # risk per share = entry fill - initial stop
    bars_held: int = 0
    highest_price_seen: float = 0.0
    lowest_price_seen: float = 0.0
    status: Literal["OPEN", "PARTIAL", "CLOSED"] = "OPEN"
    is_partial_taken: bool = False
    entry_fees: float = 0.0
    realized_pnl: float = 0.0  # gross, from partial exits
    exit_fees: float = 0.0
    is_shadow: bool = False
    signal_id: str | None = None
    pending_exit: ExitReason | None = None
    extra: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExitDecision:
    should_exit: bool
    exit_reason: ExitReason | None
    exit_qty: int
    exit_price: float
    new_stop: float | None = None
    is_partial: bool = False
    at_next_open: bool = False


_NO_EXIT = ExitDecision(False, None, 0, 0.0)


def evaluate_position_exit(
    pos: PositionRecord,
    bar_date: date,
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    atr22: float | None = None,
    donchian10_lower: float | None = None,
    cost_buffer_pct: float | None = None,
    params: ExitParams | None = None,
) -> ExitDecision:
    """Evaluate one closed bar for an open position (mutates bars_held, extremes, stop, partial flag)."""
    if params is None:
        params = exit_params_for(pos.setup, StrategyConfig())
    be_cost = params.breakeven_cost_pct if cost_buffer_pct is None else cost_buffer_pct

    pos.bars_held += 1
    pos.highest_price_seen = max(pos.highest_price_seen or pos.entry_price, high_p)
    pos.lowest_price_seen = min(pos.lowest_price_seen or pos.entry_price, low_p)
    mfe_r = (pos.highest_price_seen - pos.entry_price) / pos.risk_r if pos.risk_r > 0 else 0.0

    # 1. Stop (all classes) — checked first, conservative when stop and target share the same bar.
    if low_p <= pos.current_stop:
        exec_price = min(open_p, pos.current_stop)
        if pos.is_partial_taken:
            reason = ExitReason.BREAKEVEN
        elif pos.current_stop > pos.initial_stop + 1e-9:
            reason = ExitReason.TRAILING
        else:
            reason = ExitReason.STOP
        return ExitDecision(True, reason, pos.qty_open, exec_price)

    # 2. Mean reversion (B, E)
    if pos.trade_class == TradeClass.MEAN_REVERSION:
        if not pos.is_partial_taken and params.partial_target_r is not None:
            partial_price = pos.entry_price + params.partial_target_r * pos.risk_r
            if high_p >= partial_price:
                q = int(pos.qty_open * params.partial_pct) // params.lot * params.lot
                new_stop = round(pos.entry_price * (1.0 + be_cost), 2)
                pos.is_partial_taken = True
                pos.current_stop = max(pos.current_stop, new_stop)
                if q > 0 and q < pos.qty_open:
                    pos.status = "PARTIAL"
                    return ExitDecision(True, ExitReason.TARGET, q, max(open_p, partial_price),
                                        new_stop=pos.current_stop, is_partial=True)
                # Lot too small to split: keep the whole position with the stop moved to break-even.
        if pos.target_price is not None and high_p >= pos.target_price:
            return ExitDecision(True, ExitReason.TARGET, pos.qty_open, max(open_p, pos.target_price))
        if params.time_stop_bars is not None and pos.bars_held >= params.time_stop_bars:
            return ExitDecision(True, ExitReason.TIME, pos.qty_open, close_p, at_next_open=True)

    # 3. Trend (A, C, D)
    if pos.trade_class == TradeClass.TREND:
        if params.stagnation_bars is not None and pos.bars_held >= params.stagnation_bars and mfe_r < params.stagnation_mfe_r:
            return ExitDecision(True, ExitReason.STAGNATION, pos.qty_open, close_p, at_next_open=True)
        if params.breakeven_after_r is not None and mfe_r >= params.breakeven_after_r:
            # H1: a trade that reached +X R may not turn into a loss (stop >= entry + round-trip costs)
            be = round(pos.entry_price * (1.0 + be_cost), 2)
            if be > pos.current_stop:
                pos.current_stop = be
        if mfe_r >= params.trailing_activation_r:
            if params.trailing == "DONCHIAN" and donchian10_lower is not None and donchian10_lower == donchian10_lower:
                cand = round(donchian10_lower, 2)
                if cand > pos.current_stop:
                    pos.current_stop = cand
            elif params.trailing == "CHANDELIER" and atr22 is not None and atr22 == atr22:
                cand = round(pos.highest_price_seen - params.chandelier_k * atr22, 2)
                if cand > pos.current_stop:
                    pos.current_stop = cand

    return ExitDecision(False, None, 0, 0.0, new_stop=pos.current_stop)
