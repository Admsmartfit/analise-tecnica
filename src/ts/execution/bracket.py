"""Pre-open order package (PRD v2.1, adjusted): everything the market needs for the day, sent before the open.

From the paper/live book state it builds:

* **Entries** — BUY with mandatory server-side stop (``sl``); absolute take-profit (``tp``) only for the
  mean-reversion setups (B, E); auction entries as LIMIT at the risk cap, breakouts as STOP-LIMIT capped at
  the same price (a gap above it simply does not fill).
* **Stop updates** — trailing / break-even computed at the previous EOD, applied with a SLTP request.
* **Partials** — SELL LIMIT at entry + 1.5R for mean-reversion positions that have not taken it yet
  (re-placed every pre-open, valid for the day).
* **Exits at the open** — time stop / stagnation decided at the close: SELL LIMIT with a protection price
  (market orders are rejected in the opening auction).

The session engine simulates exactly these orders, so paper and live stay in parity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ts.core.config import StrategyConfig
from ts.core.models import OrderIntent, TradeClass
from ts.execution.session_engine import BookState, client_order_id
from ts.risk.exits import exit_params_for
from ts.strategy.setups import ceil_tick, floor_tick


@dataclass(frozen=True)
class StopUpdate:
    ticker: str
    stop: float
    reason: str


@dataclass
class PreopenPackage:
    entries: list[OrderIntent]
    exits: list[OrderIntent]
    partials: list[OrderIntent]
    stop_updates: list[StopUpdate]

    @property
    def all_orders(self) -> list[OrderIntent]:
        return [*self.exits, *self.partials, *self.entries]


def _d(x: float | None) -> Decimal | None:
    return None if x is None else Decimal(str(round(x, 8)))


def build_preopen_package(state: BookState, today: date, cfg: StrategyConfig, prefix: str = "TS",
                          fractional: bool = False) -> PreopenPackage:
    tick = cfg.risk.tick_size
    prot = cfg.risk.auction_limit_protection_pct
    entries: list[OrderIntent] = []
    exits: list[OrderIntent] = []
    for o in state.pending:
        if o.is_shadow or o.valid_until < today:
            continue
        if o.kind == "EXIT_OPEN":
            last = state.last_close.get(o.ticker)
            if last is None:
                continue
            exits.append(OrderIntent(
                o.client_order_id, o.signal_id, o.ticker, "SELL", o.qty, "LIMIT",
                _d(floor_tick(last * (1.0 - prot), tick)), None, today, o.reason, o.position_id, "live",
            ))
            continue
        if o.kind == "OPEN_AUCTION":
            entries.append(OrderIntent(
                o.client_order_id, o.signal_id, o.ticker, "BUY", o.qty, "LIMIT", _d(o.limit_price),
                _d(o.stop_price), today, o.reason, None, "live", take_profit=_d(o.target_price),
                risk_budget=_d(o.risk_budget),
            ))
        elif o.kind == "BUY_STOP":
            entries.append(OrderIntent(
                o.client_order_id, o.signal_id, o.ticker, "BUY", o.qty, "STOP_LIMIT", _d(o.entry_ref),
                _d(o.stop_price), today, o.reason, None, "live", take_profit=_d(o.target_price),
                limit_price=_d(o.max_fill_price), risk_budget=_d(o.risk_budget),
            ))

    partials: list[OrderIntent] = []
    stops: list[StopUpdate] = []
    exiting = {o.ticker for o in state.pending if o.kind == "EXIT_OPEN" and not o.is_shadow}
    for t, p in state.positions.items():
        stops.append(StopUpdate(t, p.current_stop, "trailing / break-even" if p.current_stop > p.initial_stop else "stop inicial"))
        if t in exiting or p.trade_class != TradeClass.MEAN_REVERSION or p.is_partial_taken:
            continue
        params = exit_params_for(p.setup, cfg, fractional=fractional)
        if params.partial_target_r is None:
            continue
        q = int(p.qty_open * params.partial_pct) // params.lot * params.lot
        if 0 < q < p.qty_open:
            price = ceil_tick(p.entry_price + params.partial_target_r * p.risk_r, tick)
            partials.append(OrderIntent(
                client_order_id(prefix, today, t, "P"), p.signal_id, t, "SELL", q, "LIMIT", _d(price), None, today,
                f"parcial {params.partial_target_r}R", p.position_id, "live",
            ))
    return PreopenPackage(entries, exits, partials, stops)
