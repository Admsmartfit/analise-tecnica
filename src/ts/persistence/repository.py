"""Persistence of paper-engine sessions: snapshot of the book + audit tables (signals, orders, positions, equity)."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from ts.core.calendar import B3Calendar
from ts.core.config import StrategyConfig
from ts.execution.session_engine import BookState, CompletedTrade, PendingOrder, SessionResult


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PaperRepository:
    def __init__(self, conn: sqlite3.Connection, mode: str = "paper") -> None:
        self.conn = conn
        self.mode = mode

    # ------------------------------------------------------------------ book snapshot
    def last_snapshot_date(self) -> date | None:
        row = self.conn.execute(
            "SELECT MAX(session_date) FROM book_snapshots WHERE mode=?", (self.mode,)
        ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def load_state(self, cfg: StrategyConfig, initial_capital: float, calendar: B3Calendar | None,
                   before: date | None = None, reference_r: dict[str, list[float]] | None = None) -> BookState:
        q = "SELECT state_json FROM book_snapshots WHERE mode=?"
        params: list[Any] = [self.mode]
        if before is not None:
            q += " AND session_date < ?"
            params.append(before.isoformat())
        row = self.conn.execute(q + " ORDER BY session_date DESC LIMIT 1", params).fetchone()
        if row is None:
            return BookState.new(initial_capital, cfg, calendar, reference_r)
        return BookState.from_dict(json.loads(row[0]), cfg, calendar, reference_r)

    def save_snapshot(self, d: date, state: BookState, config_hash: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO book_snapshots (session_date, mode, config_hash, state_json, created_at) VALUES (?,?,?,?,?)",
            (d.isoformat(), self.mode, config_hash, json.dumps(state.to_dict(), sort_keys=True), _now()),
        )

    # ------------------------------------------------------------------ audit
    def _order_event(self, cid: str, state: str, detail: str, qty: int | None = None, price: float | None = None,
                     fees: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO order_events (client_order_id, ts_utc, state, filled_qty, avg_price, fees, detail) VALUES (?,?,?,?,?,?,?)",
            (cid, _now(), state, qty, price, fees, detail),
        )

    def _ensure_intent(self, o: PendingOrder, config_hash: str, signal_known: bool) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO order_intents (client_order_id, signal_id, position_id, ticker, side, qty, order_type,
                   price, stop_loss, valid_until, reason, mode, created_at, setup, entry_kind, is_shadow, config_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                o.client_order_id, o.signal_id if signal_known else None, o.position_id, o.ticker, o.side, o.qty,
                "MARKET" if o.kind == "EXIT_OPEN" else ("STOP" if o.kind == "BUY_STOP" else "LIMIT"),
                o.limit_price if o.kind == "OPEN_AUCTION" else (o.entry_ref or None), o.stop_price or None,
                o.valid_until.isoformat(), o.reason, self.mode, _now(), o.setup, o.kind, int(o.is_shadow), config_hash,
            ),
        )

    def record_session(self, run_id: str, res: SessionResult, state: BookState, config_hash: str) -> None:
        d = res.session_date
        known_signals: set[str] = set()
        for rec in res.signals:
            s = rec.signal
            self.conn.execute(
                """INSERT OR REPLACE INTO signals (signal_id, run_id, decision_date, ticker, setup, trade_class, entry_type,
                       entry_ref, stop_price, target_price, features_json, disposition, reason, config_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    s.signal_id, run_id, s.decision_date.isoformat(), s.ticker, s.setup.value, s.trade_class.value,
                    s.entry_type, s.entry_ref, s.stop_price, s.target_price, json.dumps(s.features, sort_keys=True),
                    rec.disposition, rec.reason, config_hash,
                ),
            )
            known_signals.add(s.signal_id)
        for o in res.new_orders:
            sig_ok = o.signal_id in known_signals or self.conn.execute(
                "SELECT 1 FROM signals WHERE signal_id=?", (o.signal_id,)).fetchone() is not None
            self._ensure_intent(o, config_hash, sig_ok)
            self._order_event(o.client_order_id, "PLANNED", o.reason)
        for o, why in res.expired:
            self._ensure_intent(o, config_hash, False)
            self._order_event(o.client_order_id, "EXPIRED", why)
        for f in res.fills:
            exists = self.conn.execute("SELECT 1 FROM order_intents WHERE client_order_id=?", (f.client_order_id,)).fetchone()
            if exists:
                self._order_event(f.client_order_id, "FILLED", f.reason, f.qty, f.price, f.fees)
        self._upsert_positions(d, state, res.closed_trades)
        e = res.equity
        if e is not None:
            self.conn.execute(
                "INSERT OR REPLACE INTO equity_daily (session_date, curve, equity, cash, heat_pct, drawdown_pct) VALUES (?,?,?,?,?,?)",
                (d.isoformat(), "PAPER" if self.mode == "paper" else "REAL", e.equity, e.cash, e.heat_pct, e.drawdown_pct),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO equity_daily (session_date, curve, equity, cash, heat_pct, drawdown_pct) VALUES (?,?,?,?,?,?)",
                (d.isoformat(), "SHADOW", e.shadow_equity, e.cash, e.heat_pct, 0.0),
            )
        self._protections(d, state)
        self.save_snapshot(d, state, config_hash)

    def _upsert_positions(self, d: date, state: BookState, closed: list[CompletedTrade]) -> None:
        for book in (state.positions, state.shadow_positions):
            for p in book.values():
                self.conn.execute(
                    """INSERT INTO positions (position_id, ticker, setup, trade_class, status, opened_on, entry_price,
                           initial_stop, current_stop, target_price, qty_initial, qty_open, risk_brl, bars_held,
                           mae_r, mfe_r, highest_price, lowest_price, fees_brl, risk_per_share, is_shadow, signal_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(position_id) DO UPDATE SET status=excluded.status, current_stop=excluded.current_stop,
                           qty_open=excluded.qty_open, bars_held=excluded.bars_held, mae_r=excluded.mae_r,
                           mfe_r=excluded.mfe_r, highest_price=excluded.highest_price, lowest_price=excluded.lowest_price,
                           fees_brl=excluded.fees_brl, target_price=excluded.target_price""",
                    (
                        p.position_id, p.ticker, p.setup, p.trade_class.value, p.status, p.entry_date.isoformat(),
                        p.entry_price, p.initial_stop, p.current_stop, p.target_price, p.qty_initial, p.qty_open,
                        p.risk_r * p.qty_initial, p.bars_held,
                        (p.entry_price - p.lowest_price_seen) / p.risk_r if p.risk_r > 0 else 0.0,
                        (p.highest_price_seen - p.entry_price) / p.risk_r if p.risk_r > 0 else 0.0,
                        p.highest_price_seen, p.lowest_price_seen, p.entry_fees + p.exit_fees, p.risk_r,
                        int(p.is_shadow), p.signal_id,
                    ),
                )
        for t in closed:
            exists = self.conn.execute("SELECT 1 FROM positions WHERE position_id=?", (t.position_id,)).fetchone()
            if not exists:  # opened and closed within the same session
                self.conn.execute(
                    """INSERT INTO positions (position_id, ticker, setup, trade_class, status, opened_on, entry_price,
                           initial_stop, current_stop, qty_initial, qty_open, risk_brl, risk_per_share, is_shadow, signal_id)
                       VALUES (?,?,?,?, 'OPEN', ?,?,?,?,?,?,?,?,?,?)""",
                    (t.position_id, t.ticker, t.setup, t.trade_class.value, t.entry_date.isoformat(), t.entry_price,
                     t.entry_price - t.risk_r, t.entry_price - t.risk_r, t.qty, t.qty, t.risk_r * t.qty, t.risk_r,
                     int(t.is_shadow), t.signal_id),
                )
            self.conn.execute(
                """UPDATE positions SET status='CLOSED', closed_on=?, qty_open=0, exit_reason=?, pnl_brl=?, r_multiple=?,
                       mae_r=?, mfe_r=?, bars_held=?, fees_brl=? WHERE position_id=?""",
                (t.exit_date.isoformat(), t.exit_reason, t.net_pnl_brl, t.r_multiple, t.mae_r, t.mfe_r, t.bars_held,
                 t.fees_brl, t.position_id),
            )

    def _protections(self, d: date, state: BookState) -> None:
        for t, start in state.cooldown.paused_from_by_ticker.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO protections (scope, key, kind, active_from, active_until, reason) VALUES (?,?,?,?,?,?)",
                ("TICKER", t, "COOLDOWN", start.isoformat(), None, "2 stops in 30 sessions"),
            )
        for s, why in state.monitor.suspended_setups.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO protections (scope, key, kind, active_from, active_until, reason) VALUES (?,?,?,?,?,?)",
                ("SETUP", s, "SETUP_SUSPENDED", d.isoformat(), None, why),
            )
        lvl = state.breaker.level
        if lvl > 0:
            self.conn.execute(
                "INSERT OR IGNORE INTO protections (scope, key, kind, active_from, active_until, reason) VALUES (?,?,?,?,?,?)",
                ("PORTFOLIO", "BOOK", "DD_LEVEL2" if lvl == 2 else "DD_LEVEL1", d.isoformat(), None,
                 f"drawdown {state.breaker.drawdown:.2%}"),
            )

    def mark_submitted(self, cid: str, detail: str, broker_order_id: str | None = None, retcode: int | None = None,
                       state: str = "SUBMITTED") -> None:
        self.conn.execute(
            "INSERT INTO order_events (client_order_id, ts_utc, state, broker_order_id, retcode, detail) VALUES (?,?,?,?,?,?)",
            (cid, _now(), state, broker_order_id, retcode, detail),
        )
