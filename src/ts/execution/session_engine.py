"""Session engine: one trading session of the book — shared by backtest and paper (PRD principle 1).

For a closed session ``d`` it:

1. Executes the orders planned the evening before (opening auction, buy-stops valid for one session,
   exits decided at the previous close) against the RAW bar of ``d`` with slippage and B3 costs.
2. Manages open positions with the bar of ``d`` (stops/partials/targets intrabar, trailing updates,
   time/stagnation exits queued for the next open).
3. Marks to market, updates the circuit breaker (with the shadow curve) and heat.
4. Evaluates signals with the closed bar of ``d`` and plans orders for the next session, recording the
   disposition of every signal (accepted, rejected and why, or shadow).

The backtest calls :meth:`SessionEngine.run_session` for every session; the paper EOD routine calls it once
per day with the state persisted in SQLite. Same code, same numbers.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ts.core.calendar import B3Calendar
from ts.core.config import CostsConfig, RegimeTier, StrategyConfig
from ts.core.models import ExitReason, Signal, TradeClass
from ts.risk.exits import ExitDecision, ExitParams, PositionRecord, evaluate_position_exit, exit_params_for
from ts.risk.portfolio import PortfolioRiskManager, compute_betas
from ts.risk.protections import AssetCooldownTracker, CircuitBreaker, SetupStatisticalMonitor
from ts.risk.sizing import SizingResult, calculate_position_size
from ts.strategy.regime import regime_tier
from ts.strategy.router import compute_breadth, context_for, route
from ts.strategy.setups import ceil_tick, floor_tick
from ts.strategy.signal_engine import SignalEngine, TickerData

logger = logging.getLogger("ts.execution.session_engine")


# ----------------------------------------------------------------------------------------------- data
@dataclass
class CompletedTrade:
    position_id: str
    ticker: str
    setup: str
    trade_class: TradeClass
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float  # quantity-weighted average of all exit legs
    qty: int
    risk_r: float  # risk per share at entry
    r_multiple: float  # net P&L / initial risk in BRL
    pnl_brl: float  # gross
    fees_brl: float  # entry + exit
    net_pnl_brl: float
    exit_reason: str
    bars_held: int
    mae_r: float = 0.0
    mfe_r: float = 0.0
    is_shadow: bool = False
    signal_id: str | None = None


@dataclass
class PendingOrder:
    client_order_id: str
    signal_id: str | None
    ticker: str
    side: str  # BUY / SELL
    kind: str  # OPEN_AUCTION, BUY_STOP, EXIT_OPEN
    qty: int
    entry_ref: float
    limit_price: float | None
    stop_price: float
    target_r: float | None
    setup: str
    trade_class: str
    valid_until: date
    is_shadow: bool = False
    reason: str = ""
    exit_reason: str | None = None
    position_id: str | None = None
    risk_budget: float | None = None  # BRL the trade may lose at the stop (sizing target)
    max_fill_price: float | None = None  # risk cap: highest acceptable fill (stop-limit / auction limit)
    target_price: float | None = None  # absolute take-profit sent with the entry (mean-reversion setups)


@dataclass
class FillEvent:
    client_order_id: str
    ticker: str
    side: str
    qty: int
    price: float
    fees: float
    session_date: date
    is_shadow: bool
    reason: str


@dataclass
class SignalRecord:
    signal: Signal
    disposition: str
    reason: str | None = None
    sizing: SizingResult | None = None
    client_order_id: str | None = None


@dataclass
class EquitySnapshot:
    session_date: date
    equity: float
    cash: float
    heat_pct: float
    drawdown_pct: float
    shadow_equity: float
    open_positions: int
    cb_level: int
    regime_percentile: float | None


@dataclass
class SessionResult:
    session_date: date
    fills: list[FillEvent] = field(default_factory=list)
    expired: list[tuple[PendingOrder, str]] = field(default_factory=list)
    closed_trades: list[CompletedTrade] = field(default_factory=list)
    signals: list[SignalRecord] = field(default_factory=list)
    new_orders: list[PendingOrder] = field(default_factory=list)
    equity: EquitySnapshot | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class BookState:
    initial_capital: float
    cash: float
    positions: dict[str, PositionRecord] = field(default_factory=dict)
    shadow_positions: dict[str, PositionRecord] = field(default_factory=dict)
    shadow_realized: float = 0.0
    pending: list[PendingOrder] = field(default_factory=list)
    last_close: dict[str, float] = field(default_factory=dict)
    last_session: date | None = None
    cooldown: AssetCooldownTracker = field(default_factory=AssetCooldownTracker)
    monitor: SetupStatisticalMonitor = field(default_factory=SetupStatisticalMonitor)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    # Probation book of suspended setups (key "setup:ticker"): virtual, 1 lot, never touches cash or the
    # circuit-breaker shadow curve; only feeds SetupStatisticalMonitor.record_probation.
    probation: dict[str, PositionRecord] = field(default_factory=dict)
    probation_pending: list[PendingOrder] = field(default_factory=list)

    @classmethod
    def new(cls, capital: float, cfg: StrategyConfig, calendar: B3Calendar | None = None,
            reference_r: dict[str, list[float]] | None = None) -> BookState:
        p = cfg.protections
        sessions_fn = _sessions_counter(calendar)
        return cls(
            initial_capital=capital,
            cash=capital,
            cooldown=AssetCooldownTracker(p.cooldown_stops_count, p.cooldown_lookback_days, p.cooldown_pause_days,
                                          sessions_between=sessions_fn),
            monitor=SetupStatisticalMonitor(p.setup_monitor_window, p.setup_bootstrap_quantile, reference_r,
                                            p.cusum_k_sigma, p.cusum_h_sigma, p.setup_resume_trades),
            breaker=CircuitBreaker(p.circuit_breaker_dd_level1, p.circuit_breaker_dd_level2, p.shadow_sma_period,
                                   capital, p.circuit_breaker_level1_risk_multiplier,
                                   p.quarantine_consecutive_losses),
        )

    # ---------------------------------------------------------------- (de)serialization for paper mode
    def to_dict(self) -> dict[str, Any]:
        def pos(p: PositionRecord) -> dict[str, Any]:
            d = asdict(p)
            d["trade_class"] = p.trade_class.value
            d["entry_date"] = p.entry_date.isoformat()
            d["pending_exit"] = p.pending_exit.value if p.pending_exit else None
            return d

        def order(o: PendingOrder) -> dict[str, Any]:
            d = asdict(o)
            d["valid_until"] = o.valid_until.isoformat()
            return d

        b = self.breaker
        return {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "positions": [pos(p) for p in self.positions.values()],
            "shadow_positions": [pos(p) for p in self.shadow_positions.values()],
            "shadow_realized": self.shadow_realized,
            "pending": [order(o) for o in self.pending],
            "last_close": self.last_close,
            "last_session": self.last_session.isoformat() if self.last_session else None,
            "cooldown": {
                "stops": {k: [d.isoformat() for d in v] for k, v in self.cooldown.stop_dates_by_ticker.items()},
                "paused_from": {k: v.isoformat() for k, v in self.cooldown.paused_from_by_ticker.items()},
            },
            "monitor": {
                "trades": self.monitor.trades_r_by_setup,
                "suspended": self.monitor.suspended_setups,
                "cusum": self.monitor._cusum,
                "probation_r": self.monitor.probation_r,
            },
            "probation": [pos(p) for p in self.probation.values()],
            "probation_pending": [order(o) for o in self.probation_pending],
            "breaker": {
                "peak": b.peak_equity, "current": b.current_equity, "shadow": b.shadow_equity_curve[-400:],
                "halted": b.is_halted, "sessions_halted": b.sessions_halted, "dd": b.drawdown,
                "loss_streak": b.loss_streak, "halt_reason": b.halt_reason,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], cfg: StrategyConfig, calendar: B3Calendar | None = None,
                  reference_r: dict[str, list[float]] | None = None) -> BookState:
        st = cls.new(float(data["initial_capital"]), cfg, calendar, reference_r)
        st.cash = float(data["cash"])

        def pos(d: dict[str, Any]) -> PositionRecord:
            d = dict(d)
            d["trade_class"] = TradeClass(d["trade_class"])
            d["entry_date"] = date.fromisoformat(d["entry_date"])
            d["pending_exit"] = ExitReason(d["pending_exit"]) if d.get("pending_exit") else None
            return PositionRecord(**d)

        st.positions = {p["ticker"]: pos(p) for p in data.get("positions", [])}
        st.shadow_positions = {p["ticker"]: pos(p) for p in data.get("shadow_positions", [])}
        st.shadow_realized = float(data.get("shadow_realized", 0.0))
        st.pending = [
            PendingOrder(**{**o, "valid_until": date.fromisoformat(o["valid_until"])}) for o in data.get("pending", [])
        ]
        st.last_close = {k: float(v) for k, v in data.get("last_close", {}).items()}
        st.last_session = date.fromisoformat(data["last_session"]) if data.get("last_session") else None
        cd = data.get("cooldown", {})
        st.cooldown.stop_dates_by_ticker = {k: [date.fromisoformat(x) for x in v] for k, v in cd.get("stops", {}).items()}
        st.cooldown.paused_from_by_ticker = {k: date.fromisoformat(v) for k, v in cd.get("paused_from", {}).items()}
        mo = data.get("monitor", {})
        st.monitor.trades_r_by_setup = {k: list(v) for k, v in mo.get("trades", {}).items()}
        st.monitor.suspended_setups = dict(mo.get("suspended", {}))
        st.monitor._cusum = dict(mo.get("cusum", {}))
        st.monitor.probation_r = {k: list(v) for k, v in mo.get("probation_r", {}).items()}
        st.probation = {f"{p['setup']}:{p['ticker']}": pos(p) for p in data.get("probation", [])}
        st.probation_pending = [PendingOrder(**{**o, "valid_until": date.fromisoformat(o["valid_until"])})
                                for o in data.get("probation_pending", [])]
        br = data.get("breaker", {})
        if br:
            b = st.breaker
            b.peak_equity, b.current_equity = float(br["peak"]), float(br["current"])
            b.shadow_equity_curve = [float(x) for x in br["shadow"]] or [b.current_equity]
            b.is_halted, b.sessions_halted, b.drawdown = bool(br["halted"]), int(br["sessions_halted"]), float(br["dd"])
            b.loss_streak = int(br.get("loss_streak", 0))
            b.halt_reason = br.get("halt_reason")
        return st


def _sessions_counter(calendar: B3Calendar | None) -> Callable[[date, date], int] | None:
    if calendar is None:
        return None

    def count(a: date, b: date) -> int:
        if b <= a:
            return 0
        return len(calendar.sessions_between(a, b)) - (1 if calendar.is_session(a) else 0)

    return count


def client_order_id(prefix: str, d: date, ticker: str, tag: str) -> str:
    """Short deterministic id that fits MT5's 31-char ``comment`` (idempotency key, PRD 1.3.1)."""
    return f"{prefix}{d.strftime('%y%m%d')}{ticker}{tag}"[:31]


# --------------------------------------------------------------------------------------------- engine
class SessionEngine:
    def __init__(
        self,
        strategy_config: StrategyConfig,
        costs_config: CostsConfig,
        config_hash: str,
        calendar: B3Calendar | None = None,
        fractional: bool = False,
        order_prefix: str = "TS",
    ) -> None:
        self.cfg = strategy_config
        self.costs = costs_config.costs
        self.config_hash = config_hash
        self.calendar = calendar
        self.fractional = fractional
        self.prefix = order_prefix
        self.signal_engine = SignalEngine(strategy_config, config_hash)
        self.portfolio = PortfolioRiskManager(strategy_config.portfolio)
        # break-even for the remainder after the partial = entry + round-trip costs (PRD 1.2.2)
        self.breakeven_cost_pct = 2.0 * (self.costs.b3_emoluments_pct + self.costs.slippage_pct)
        self._params: dict[str, ExitParams] = {}
        self._breadth_cache: tuple[tuple[int, int], pd.DataFrame] | None = None
        # External veto on NEW buys (TradingAgents "Cérebro"): (ticker, session) -> None to allow, or the reason
        # to block. Set only by the paper/live routines; backtests never use it (no historical decisions).
        self.entry_gate: Callable[[str, date], str | None] | None = None

    # ------------------------------------------------------------------ helpers
    def exit_params(self, setup: str) -> ExitParams:
        if setup not in self._params:
            self._params[setup] = exit_params_for(setup, self.cfg, self.breakeven_cost_pct, self.fractional)
        return self._params[setup]

    def _fees(self, value: float) -> float:
        return value * self.costs.b3_emoluments_pct + self.costs.brokerage_per_order

    def _next_session(self, d: date) -> date:
        if self.calendar is not None:
            return self.calendar.next_session(d)
        nd = pd.Timestamp(d) + pd.offsets.BDay(1)
        return nd.date()

    @staticmethod
    def _bar(td: TickerData, d: date) -> tuple[int, dict[str, float]] | None:
        k = td.index_of(d)
        if k is None:
            return None
        cols = td.view._cols
        return k, {
            "open": float(cols["raw_open"][k]),
            "high": float(cols["raw_high"][k]),
            "low": float(cols["raw_low"][k]),
            "close": float(cols["raw_close"][k]),
            "factor": float(cols["adj_factor"][k]),
        }

    # ------------------------------------------------------------------ main entry
    def run_session(
        self,
        state: BookState,
        d: date,
        prepared: Mapping[str, TickerData],
        index_bars: pd.DataFrame | None = None,
        market_ok: bool | None = None,
        regime_percentile: float | None = None,
        index_returns: pd.Series | None = None,
        universe: set[str] | None = None,
        plan: bool = True,
        market_bear: bool | None = None,
    ) -> SessionResult:
        res = SessionResult(session_date=d)
        self._market_bear = market_bear  # read by _plan (setup F only trades in a bear market)
        state.breaker.just_halted = False
        bars_today: dict[str, tuple[int, dict[str, float]]] = {}
        for t, td in prepared.items():
            b = self._bar(td, d)
            if b is not None:
                bars_today[t] = b
                state.last_close[t] = b[1]["close"]

        opened_today = self._execute_orders(state, d, bars_today, res)
        self._manage_positions(state, d, prepared, bars_today, opened_today, res)
        if state.probation or state.probation_pending:
            self._run_probation(state, d, prepared, bars_today, res)
        snap = self._mark_to_market(state, d, regime_percentile)
        res.equity = snap
        state.last_session = d

        if plan:
            self._plan(state, d, prepared, index_bars, market_ok, regime_percentile, index_returns, universe, res)
        return res

    # ------------------------------------------------------------------ 1. orders
    def _execute_orders(
        self, state: BookState, d: date, bars: dict[str, tuple[int, dict[str, float]]], res: SessionResult
    ) -> set[str]:
        slip = self.costs.slippage_pct
        opened: set[str] = set()
        keep: list[PendingOrder] = []
        for o in state.pending:
            if o.valid_until < d:
                res.expired.append((o, "expired before session"))
                continue
            bar = bars.get(o.ticker)
            if bar is None:
                if o.valid_until > d:
                    keep.append(o)
                else:
                    res.expired.append((o, "no bar for the session"))
                continue
            k, b = bar
            book = state.shadow_positions if o.is_shadow else state.positions

            if o.kind == "EXIT_OPEN":
                pos = book.get(o.ticker)
                if pos is None:
                    continue
                price = b["open"] * (1.0 - slip)
                self._close(state, pos, d, min(o.qty, pos.qty_open), price, ExitReason(o.exit_reason or "TIME"), res)
                continue

            if o.ticker in book:
                res.expired.append((o, "position already open"))
                continue
            if self.entry_gate is not None and not o.is_shadow and o.side == "BUY":
                veto = self.entry_gate(o.ticker, d)
                if veto:
                    res.expired.append((o, veto))
                    continue
            fill: float | None = None
            if o.kind == "OPEN_AUCTION":
                if o.limit_price is not None and b["open"] > o.limit_price:
                    res.expired.append((o, f"abertura {b['open']:.2f} acima do preço máximo {o.limit_price:.2f} "
                                           "(proteção do leilão / teto de risco)"))
                    continue
                if b["open"] * (1.0 + slip) <= o.stop_price:
                    # Gap down through the technical stop: the setup is invalid before entering (a buy with SL above
                    # the price is rejected by the broker). Filling here produced risk ~0 and absurd R multiples.
                    res.expired.append((o, f"abertura {b['open']:.2f} no/abaixo do stop {o.stop_price:.2f} "
                                           "(setup invalidado pelo gap)"))
                    continue
                fill = b["open"] * (1.0 + slip)
            elif o.kind == "BUY_STOP":
                if b["high"] >= o.entry_ref:
                    base = max(b["open"], o.entry_ref)
                    if o.max_fill_price is not None and base > o.max_fill_price:
                        # stop-limit: gap above the risk cap -> no fill
                        res.expired.append((o, f"gap: abertura {b['open']:.2f} acima do teto de risco "
                                               f"{o.max_fill_price:.2f}"))
                        continue
                    fill = base * (1.0 + slip)
            if fill is None:
                if o.valid_until > d:
                    keep.append(o)
                else:
                    res.expired.append((o, "buy-stop not triggered"))
                continue

            value = o.qty * fill
            fees = self._fees(value)
            if not o.is_shadow and state.cash < value + fees:
                res.expired.append((o, "insufficient cash"))
                continue
            if not o.is_shadow:
                state.cash -= value + fees
            else:
                state.shadow_realized -= fees
            risk_ps = fill - o.stop_price
            target = o.target_price  # absolute TP sent with the entry (planned the night before)
            if target is None and o.target_r is not None and risk_ps > 0:  # snapshots from older versions
                target = ceil_tick(fill + o.target_r * risk_ps, self.cfg.risk.tick_size)
            pos = PositionRecord(
                position_id=o.client_order_id,
                ticker=o.ticker,
                setup=o.setup,
                trade_class=TradeClass(o.trade_class),
                entry_date=d,
                entry_price=fill,
                initial_stop=o.stop_price,
                current_stop=o.stop_price,
                target_price=target,
                qty_initial=o.qty,
                qty_open=o.qty,
                risk_r=max(risk_ps, 1e-9),
                highest_price_seen=fill,
                lowest_price_seen=fill,
                entry_fees=fees,
                is_shadow=o.is_shadow,
                signal_id=o.signal_id,
            )
            pos.extra["entry_kind_stop"] = 1.0 if o.kind == "BUY_STOP" else 0.0
            book[o.ticker] = pos
            opened.add(("S:" if o.is_shadow else "") + o.ticker)
            res.fills.append(FillEvent(o.client_order_id, o.ticker, "BUY", o.qty, fill, fees, d, o.is_shadow, o.kind))
        state.pending = keep
        return opened

    # ------------------------------------------------------------------ 2. exits
    def _manage_positions(
        self,
        state: BookState,
        d: date,
        prepared: Mapping[str, TickerData],
        bars: dict[str, tuple[int, dict[str, float]]],
        opened_today: set[str],
        res: SessionResult,
    ) -> None:
        for book, shadow in ((state.positions, False), (state.shadow_positions, True)):
            for ticker in list(book.keys()):
                pos = book[ticker]
                if pos.pending_exit is not None:
                    continue
                bar = bars.get(ticker)
                if bar is None:
                    continue
                k, b = bar
                td = prepared[ticker]
                factor = b["factor"] or 1.0
                atr22 = float(td.view._cols["atr22"][k]) / factor
                lows = td.view._cols["raw_low"][max(0, k - 9): k + 1]
                donch = float(np.min(lows)) if len(lows) == 10 else None
                params = self.exit_params(pos.setup)
                sma_exit = float(td.view._cols["sma_f"][k]) / factor if params.exit_above_sma else None
                high = b["high"]
                key = ("S:" if shadow else "") + ticker
                if key in opened_today and params.partial_target_r is not None:
                    # Live: the partial is a SELL LIMIT placed at the next pre-open (the fill price is only known
                    # after the auction). Stop and final target ride with the entry order from day one.
                    params = replace(params, partial_target_r=None)
                if key in opened_today and pos.extra.get("entry_kind_stop"):
                    # Buy-stop filled intraday: the bar's high before the fill is unknowable -> conservative.
                    high = max(pos.entry_price, b["close"])
                dec = evaluate_position_exit(
                    pos, d, b["open"], high, b["low"], b["close"],
                    atr22=atr22 if math.isfinite(atr22) else None,
                    donchian10_lower=donch, params=params, sma_exit=sma_exit,
                )
                self._apply_exit(state, pos, d, dec, res)

    def _apply_exit(self, state: BookState, pos: PositionRecord, d: date, dec: ExitDecision, res: SessionResult) -> None:
        if not dec.should_exit or dec.exit_reason is None:
            return
        if dec.at_next_open:
            pos.pending_exit = dec.exit_reason
            nd = self._next_session(d)
            o = PendingOrder(
                client_order_id=client_order_id(self.prefix, nd, pos.ticker, "X" + ("S" if pos.is_shadow else "")),
                signal_id=pos.signal_id, ticker=pos.ticker, side="SELL", kind="EXIT_OPEN", qty=pos.qty_open,
                entry_ref=0.0, limit_price=None, stop_price=0.0, target_r=None, setup=pos.setup,
                trade_class=pos.trade_class.value, valid_until=date.max, is_shadow=pos.is_shadow,
                reason=f"{dec.exit_reason.value} decided at close of {d}", exit_reason=dec.exit_reason.value,
                position_id=pos.position_id,
            )
            state.pending.append(o)
            res.new_orders.append(o)
            return
        slip = self.costs.slippage_pct
        is_stop = dec.exit_reason in (ExitReason.STOP, ExitReason.TRAILING, ExitReason.BREAKEVEN)
        price = dec.exit_price * (1.0 - slip) if is_stop else dec.exit_price
        if dec.is_partial:
            self._partial(state, pos, d, dec.exit_qty, price, res)
        else:
            self._close(state, pos, d, dec.exit_qty, price, dec.exit_reason, res)

    def _partial(self, state: BookState, pos: PositionRecord, d: date, qty: int, price: float, res: SessionResult) -> None:
        value = qty * price
        fees = self._fees(value)
        pos.realized_pnl += (price - pos.entry_price) * qty
        pos.exit_fees += fees
        pos.qty_open -= qty
        if pos.is_shadow:
            state.shadow_realized += (price - pos.entry_price) * qty - fees
        else:
            state.cash += value - fees
        res.fills.append(FillEvent(pos.position_id + "P", pos.ticker, "SELL", qty, price, fees, d, pos.is_shadow, "PARTIAL"))

    def _close(self, state: BookState, pos: PositionRecord, d: date, qty: int, price: float,
               reason: ExitReason, res: SessionResult) -> None:
        value = qty * price
        fees = self._fees(value)
        gross_leg = (price - pos.entry_price) * qty
        if pos.is_shadow:
            state.shadow_realized += gross_leg - fees
        else:
            state.cash += value - fees
        pos.realized_pnl += gross_leg
        pos.exit_fees += fees
        pos.qty_open -= qty
        res.fills.append(FillEvent(pos.position_id + "X", pos.ticker, "SELL", qty, price, fees, d, pos.is_shadow, reason.value))
        if pos.qty_open > 0:
            return
        book = state.shadow_positions if pos.is_shadow else state.positions
        book.pop(pos.ticker, None)
        pos.status = "CLOSED"
        total_fees = pos.entry_fees + pos.exit_fees
        net = pos.realized_pnl - total_fees
        risk_brl = pos.risk_r * pos.qty_initial
        exit_avg = pos.entry_price + pos.realized_pnl / pos.qty_initial
        trade = CompletedTrade(
            position_id=pos.position_id, ticker=pos.ticker, setup=pos.setup, trade_class=pos.trade_class,
            entry_date=pos.entry_date, exit_date=d, entry_price=pos.entry_price, exit_price=exit_avg,
            qty=pos.qty_initial, risk_r=pos.risk_r, r_multiple=net / risk_brl if risk_brl > 0 else 0.0,
            pnl_brl=pos.realized_pnl, fees_brl=total_fees, net_pnl_brl=net, exit_reason=reason.value,
            bars_held=pos.bars_held,
            mae_r=(pos.entry_price - pos.lowest_price_seen) / pos.risk_r if pos.risk_r > 0 else 0.0,
            mfe_r=(pos.highest_price_seen - pos.entry_price) / pos.risk_r if pos.risk_r > 0 else 0.0,
            is_shadow=pos.is_shadow, signal_id=pos.signal_id,
        )
        res.closed_trades.append(trade)
        if not pos.is_shadow:
            if reason == ExitReason.STOP:
                state.cooldown.record_stop(pos.ticker, d)
            state.breaker.record_trade(trade.r_multiple)
            if state.monitor.record_trade(pos.setup, trade.r_multiple):
                res.notes.append(f"SETUP_SUSPENDED {pos.setup}: {state.monitor.suspended_setups.get(pos.setup)}")

    # ------------------------------------------------------------------ 3. MTM
    def _mark_to_market(self, state: BookState, d: date, regime_percentile: float | None) -> EquitySnapshot:
        mv, heat = 0.0, 0.0
        for t, p in state.positions.items():
            px = state.last_close.get(t, p.entry_price)
            mv += p.qty_open * px
            heat += max(0.0, (px - p.current_stop) * p.qty_open)
        equity = state.cash + mv
        shadow_unreal = sum(
            (state.last_close.get(t, p.entry_price) - p.entry_price) * p.qty_open
            for t, p in state.shadow_positions.items()
        )
        shadow_equity = equity + state.shadow_realized + shadow_unreal
        state.breaker.update(equity, shadow_equity)
        return EquitySnapshot(
            session_date=d, equity=equity, cash=state.cash, heat_pct=heat / equity if equity > 0 else 0.0,
            drawdown_pct=state.breaker.drawdown, shadow_equity=shadow_equity, open_positions=len(state.positions),
            cb_level=state.breaker.level, regime_percentile=regime_percentile,
        )

    # ------------------------------------------------------------------ 4. planning
    def _plan(
        self,
        state: BookState,
        d: date,
        prepared: Mapping[str, TickerData],
        index_bars: pd.DataFrame | None,
        market_ok: bool | None,
        regime_percentile: float | None,
        index_returns: pd.Series | None,
        universe: set[str] | None,
        res: SessionResult,
    ) -> None:
        candidates = {t: td for t, td in prepared.items() if universe is None or t in universe}
        signals = self.signal_engine.evaluate_prepared(
            d, candidates, index_bars if index_bars is not None else pd.DataFrame(), market_ok=market_ok,
            market_bear=getattr(self, "_market_bear", None),
        )
        if not signals:
            return
        tier: RegimeTier = regime_tier(float("nan") if regime_percentile is None else regime_percentile, self.cfg.regime)
        nd = self._next_session(d)
        equity = res.equity.equity if res.equity else state.cash
        halted = state.breaker.is_halted
        risk_pct = self.cfg.risk.risk_per_trade_pct * tier.risk_multiplier * state.breaker.risk_multiplier()
        pending_tickers = {o.ticker for o in state.pending if not o.is_shadow}

        sized: list[tuple[Signal, SizingResult]] = []
        breadth = self._breadth(prepared)
        router_cfg = self.cfg.regime_router
        for sig in signals:
            rec = SignalRecord(sig, "")
            res.signals.append(rec)
            td = prepared[sig.ticker]
            k = td.index_of(d)
            ctx = context_for(td, k, d, breadth) if k is not None else None
            if ctx is not None:
                sig.features.update(ctx.as_features())  # regime tag on every signal (router on or off)
            if router_cfg.enabled and ctx is not None and not halted:
                why = route(sig, ctx, router_cfg)
                if why:
                    rec.disposition, rec.reason = "REJECTED_REGIME", why
                    continue
            if halted:
                self._shadow_order(state, sig, d, nd, equity, rec)
                continue
            if tier.risk_multiplier <= 0:
                rec.disposition, rec.reason = "REJECTED_REGIME", f"vol percentile {regime_percentile:.0f} blocks new entries"
                continue
            if sig.ticker in state.positions or sig.ticker in pending_tickers:
                rec.disposition, rec.reason = "REJECTED_DUPLICATE", "position or order already open"
                continue
            if state.cooldown.is_paused(sig.ticker, d):
                rec.disposition, rec.reason = "REJECTED_COOLDOWN", "asset in cooldown"
                continue
            if state.monitor.is_suspended(sig.setup.value):
                rec.disposition, rec.reason = "REJECTED_SETUP", state.monitor.suspended_setups.get(sig.setup.value)
                if state.monitor.resume_trades > 0:
                    self._probation_order(state, sig, nd, equity)
                    rec.reason = f"{rec.reason} — acompanhado na sombra para poder voltar"
                continue
            sz = calculate_position_size(
                sig.entry_ref, sig.stop_price, sig.features.get("atr14_raw", float("nan")), equity,
                self.cfg.risk, risk_pct_override=risk_pct, gap_p01=sig.features.get("gap_p01"),
                fractional=self.fractional,
            )
            rec.sizing = sz
            if not sz.accepted:
                rec.disposition, rec.reason = "REJECTED_SIZE", sz.rejection_reason
                continue
            if sz.note:
                res.notes.append(f"{sig.signal_id}: {sz.note}")
            sized.append((sig, sz))

        if not sized:
            return
        rets, betas, ibov_window = self._returns_context(state, d, prepared, [s.ticker for s, _ in sized], index_returns)
        open_pos = [
            {"ticker": t, "risk_brl": max(0.0, (state.last_close.get(t, p.entry_price) - p.current_stop) * p.qty_open),
             "capital_allocated": state.last_close.get(t, p.entry_price) * p.qty_open}
            for t, p in state.positions.items()
        ]
        decisions = self.portfolio.evaluate_candidates(
            sized, equity, open_pos, rets, ibov_window, betas, max_heat_pct=tier.heat_max_pct
        )
        by_id = {r.signal.signal_id: r for r in res.signals}
        for dec in decisions:
            rec = by_id[dec.signal.signal_id]
            rec.disposition, rec.reason = dec.disposition, dec.reason
            if dec.disposition == "ACCEPTED":
                rec.client_order_id = self._queue_entry(state, dec.signal, dec.sizing, d, nd, False, res)

    def _breadth(self, prepared: Mapping[str, TickerData]) -> pd.DataFrame:
        """Universe breadth, computed once per prepared set (whole history; causal per row)."""
        key = (id(prepared), len(prepared))
        if self._breadth_cache is None or self._breadth_cache[0] != key:
            self._breadth_cache = (key, compute_breadth(prepared))
        return self._breadth_cache[1]

    # ------------------------------------------------------------------ probation of suspended setups
    def _probation_order(self, state: BookState, sig: Signal, nd: date, equity: float) -> None:
        key = f"{sig.setup.value}:{sig.ticker}"
        if key in state.probation or any(f"{o.setup}:{o.ticker}" == key for o in state.probation_pending):
            return
        # same stop rules as a real entry (1–3 ATR); the size itself is irrelevant (R is per share)
        sz = calculate_position_size(sig.entry_ref, sig.stop_price, sig.features.get("atr14_raw", float("nan")),
                                     max(equity, 1.0), self.cfg.risk, gap_p01=sig.features.get("gap_p01"))
        if not sz.accepted:
            return
        prot = self.cfg.risk.auction_limit_protection_pct
        state.probation_pending.append(PendingOrder(
            client_order_id=f"PB{key}", signal_id=sig.signal_id, ticker=sig.ticker, side="BUY", kind=sig.entry_type,
            qty=1, entry_ref=sig.entry_ref,
            limit_price=sig.entry_ref * (1 + prot) if sig.entry_type == "OPEN_AUCTION" else None,
            stop_price=sz.stop_price, target_r=None, setup=sig.setup.value, trade_class=sig.trade_class.value,
            valid_until=nd, reason="probation", target_price=sig.target_price,
        ))

    def _run_probation(self, state: BookState, d: date, prepared: Mapping[str, TickerData],
                       bars: dict[str, tuple[int, dict[str, float]]], res: SessionResult) -> None:
        slip, emol = self.costs.slippage_pct, self.costs.b3_emoluments_pct

        def close(key: str, pos: PositionRecord, price: float) -> None:
            r = ((price - pos.entry_price) - (price + pos.entry_price) * emol) / pos.risk_r
            del state.probation[key]
            if state.monitor.record_probation(pos.setup, r):
                res.notes.append(f"SETUP_RESUMED {pos.setup}: média positiva nas últimas "
                                 f"{state.monitor.resume_trades} operações-sombra")

        for key, pos in list(state.probation.items()):  # exits decided at yesterday's close
            if pos.pending_exit is not None and pos.ticker in bars:
                close(key, pos, bars[pos.ticker][1]["open"] * (1 - slip))
        keep = []
        for o in state.probation_pending:
            bar = bars.get(o.ticker)
            if bar is None:
                if o.valid_until > d:
                    keep.append(o)
                continue
            b = bar[1]
            if o.kind == "OPEN_AUCTION":
                if (o.limit_price is not None and b["open"] > o.limit_price) or b["open"] * (1 + slip) <= o.stop_price:
                    continue
                fill = b["open"] * (1 + slip)
            elif b["high"] >= o.entry_ref:
                fill = max(b["open"], o.entry_ref) * (1 + slip)
            else:
                continue
            state.probation[f"{o.setup}:{o.ticker}"] = PositionRecord(
                position_id=o.client_order_id, ticker=o.ticker, setup=o.setup, trade_class=TradeClass(o.trade_class),
                entry_date=d, entry_price=fill, initial_stop=o.stop_price, current_stop=o.stop_price,
                target_price=o.target_price, qty_initial=1, qty_open=1, risk_r=max(fill - o.stop_price, 1e-9),
                highest_price_seen=fill, lowest_price_seen=fill,
                extra={"entry_kind_stop": 1.0 if o.kind == "BUY_STOP" else 0.0})
        state.probation_pending = keep
        for key, pos in list(state.probation.items()):
            if pos.pending_exit is not None or pos.ticker not in bars:
                continue
            k, b = bars[pos.ticker]
            atr22, donch, sma_exit = self._exit_inputs(prepared[pos.ticker], k, b["factor"] or 1.0)
            params = replace(self.exit_params(pos.setup), partial_target_r=None)  # whole position, simpler R
            # same conservative rule as the real book: a buy-stop filled today cannot use the bar's high
            high = max(pos.entry_price, b["close"]) if pos.entry_date == d and pos.extra.get("entry_kind_stop") \
                else b["high"]
            dec = evaluate_position_exit(pos, d, b["open"], high, b["low"], b["close"], atr22=atr22,
                                         donchian10_lower=donch, params=params,
                                         sma_exit=sma_exit if params.exit_above_sma else None)
            if not dec.should_exit:
                continue
            if dec.at_next_open:
                pos.pending_exit = dec.exit_reason
            else:
                stop_like = dec.exit_reason in (ExitReason.STOP, ExitReason.TRAILING, ExitReason.BREAKEVEN)
                close(key, pos, dec.exit_price * (1 - slip) if stop_like else dec.exit_price)

    @staticmethod
    def _exit_inputs(td: TickerData, k: int, factor: float) -> tuple[float | None, float | None, float | None]:
        atr22 = float(td.view._cols["atr22"][k]) / factor
        lows = td.view._cols["raw_low"][max(0, k - 9): k + 1]
        donch = float(np.min(lows)) if len(lows) == 10 else None
        sma = float(td.view._cols["sma_f"][k]) / factor if "sma_f" in td.view._cols else None
        return (atr22 if math.isfinite(atr22) else None), donch, sma

    def _shadow_order(self, state: BookState, sig: Signal, d: date, nd: date, equity: float, rec: SignalRecord) -> None:
        rec.disposition, rec.reason = "SHADOW", "circuit breaker level 2: tracked in shadow book"
        if sig.ticker in state.shadow_positions or any(o.ticker == sig.ticker and o.is_shadow for o in state.pending):
            return
        sz = calculate_position_size(
            sig.entry_ref, sig.stop_price, sig.features.get("atr14_raw", float("nan")), equity, self.cfg.risk,
            gap_p01=sig.features.get("gap_p01"), fractional=self.fractional,
        )
        rec.sizing = sz
        if sz.accepted:
            rec.client_order_id = self._queue_entry(state, sig, sz, d, nd, True, None)

    def _queue_entry(self, state: BookState, sig: Signal, sz: SizingResult, d: date, nd: date,
                     shadow: bool, res: SessionResult | None) -> str:
        tag = sig.setup.value + ("S" if shadow else "")
        cid = client_order_id(self.prefix, nd, sig.ticker, tag)
        setup_cfg = getattr(self.cfg.setups, sig.setup.value)
        target_r = getattr(setup_cfg, "final_target_r", None) if sig.trade_class == TradeClass.MEAN_REVERSION else None
        tick = self.cfg.risk.tick_size
        # RF-02 (risk audit) built into the order itself: highest fill price whose loss at the stop still fits the
        # risk budget. The auction order is a LIMIT at min(protection, risk cap); the breakout order is a STOP-LIMIT
        # capped at the same price. A gap beyond it simply does not fill — no intraday logic, same in paper and live.
        budget = sz.target_risk_brl * (1.0 + self.cfg.risk.max_entry_risk_overshoot_pct)
        risk_cap = floor_tick(sz.stop_price + budget / sz.qty, tick)
        limit = None
        if sig.entry_type == "OPEN_AUCTION":
            limit = min(ceil_tick(sig.entry_ref * (1.0 + self.cfg.risk.auction_limit_protection_pct), tick), risk_cap)
        target_price = None
        if target_r is not None:
            target_price = ceil_tick(sig.entry_ref + target_r * (sig.entry_ref - sz.stop_price), tick)
        o = PendingOrder(
            client_order_id=cid, signal_id=sig.signal_id, ticker=sig.ticker, side="BUY", kind=sig.entry_type,
            qty=sz.qty, entry_ref=sig.entry_ref, limit_price=limit, stop_price=sz.stop_price, target_r=target_r,
            setup=sig.setup.value, trade_class=sig.trade_class.value, valid_until=nd, is_shadow=shadow,
            reason=f"Setup {sig.setup.value} decided {d}",
            risk_budget=round(budget, 2), max_fill_price=risk_cap, target_price=target_price,
        )
        state.pending.append(o)
        if res is not None:
            res.new_orders.append(o)
        return cid

    def _returns_context(
        self, state: BookState, d: date, prepared: Mapping[str, TickerData], tickers: list[str],
        index_returns: pd.Series | None,
    ) -> tuple[pd.DataFrame, dict[str, float] | None, pd.Series | None]:
        pc = self.cfg.portfolio
        names = list(dict.fromkeys([*state.positions.keys(), *tickers]))
        lb = max(pc.correlation_lookback_days, pc.beta_lookback_days)
        cols: dict[str, pd.Series] = {}
        for t in names:
            td = prepared.get(t)
            if td is None:
                continue
            k = td.last_index_upto(d)
            lo = max(0, k - lb + 1)
            cols[t] = pd.Series(td.view._cols["ret1"][lo : k + 1], index=td.dates[lo : k + 1], dtype=float)
        frame = pd.DataFrame(cols)
        corr_window = frame.tail(pc.correlation_lookback_days)
        betas = None
        ibov_window = None
        if index_returns is not None and len(index_returns):
            ir = index_returns[index_returns.index <= d]
            ibov_window = ir.tail(pc.relative_strength_days)
            beta_frame = frame.tail(pc.beta_lookback_days)
            betas = compute_betas(beta_frame, ir.reindex(beta_frame.index))
        return corr_window, betas, ibov_window
