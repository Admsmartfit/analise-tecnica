"""MetaTrader 5 live adapter (PRD 1.3.1). Disabled unless ``execution.mode == 'live'``.

Write-side safety rules:
* Every intent carries a short ``client_order_id`` stored in the MT5 ``comment`` (<= 31 chars) + fixed magic.
* Before any (re)send the broker is reconciled by that id across pending orders, open positions and
  deals — a filled market order is no longer in ``orders_get``, so checking only pending orders (as the
  previous version did) would duplicate a real order after a lost response.
* Lost response / IPC error: no blind resend. Reconcile at 2 s, 5 s, 15 s; still unknown -> UNKNOWN,
  ticker blocked, alert (OrderStateUnknownError).
* Retcode policy table (PRD 1.3.1) — only requote/price-changed/no-quote are resent (max 2, after
  reconciliation, with a fresh price); 10024 waits 5 s and retries once; everything else is final.
"""
from __future__ import annotations

import logging
import time as time_mod
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import ts.data.fake_mt5 as C  # noqa: N812 - MT5 constant namespace
from ts.core.config import Settings
from ts.core.errors import (
    ConfigError,
    OrderRejectedError,
    OrderStateUnknownError,
    TradingDisabledError,
)
from ts.core.models import BrokerOrderState, BrokerPosition, Fill, OrderIntent, OrderState, SubmitResult

logger = logging.getLogger("ts.execution.mt5_broker")

RETRYABLE_PRICE = {C.TRADE_RETCODE_REQUOTE, C.TRADE_RETCODE_PRICE_CHANGED, C.TRADE_RETCODE_PRICE_OFF}
BUSINESS_REJECT = {C.TRADE_RETCODE_REJECT, C.TRADE_RETCODE_INVALID_VOLUME, C.TRADE_RETCODE_INVALID_PRICE,
                   C.TRADE_RETCODE_INVALID_STOPS, C.TRADE_RETCODE_NO_MONEY}
CONFIG_REJECT = {C.TRADE_RETCODE_MARKET_CLOSED, C.TRADE_RETCODE_TRADE_DISABLED, C.TRADE_RETCODE_INVALID_FILL}
RECONCILE_DELAYS_S = (2.0, 5.0, 15.0)


class MT5Broker:
    def __init__(self, mt5_module: Any = None, settings: Settings | None = None,
                 sleep: Callable[[float], None] = time_mod.sleep) -> None:
        self.mt5 = mt5_module
        self.settings = settings or Settings()
        self.mode: Literal["live"] = "live"
        self._sleep = sleep
        self.blocked_tickers: set[str] = set()

    def _api(self) -> Any:
        if self.mt5 is None:
            try:
                import MetaTrader5

                self.mt5 = MetaTrader5
            except ImportError as e:
                raise OrderRejectedError("MetaTrader5 module not available") from e
        return self.mt5

    def _c(self, name: str) -> Any:
        return getattr(self.mt5, name, getattr(C, name))

    # ------------------------------------------------------------------ reconciliation
    def find_by_client_id(self, client_order_id: str) -> BrokerOrderState | None:
        mt5 = self._api()
        cid = client_order_id[:31]
        magic = self.settings.execution.magic_number

        def mine(o: Any) -> bool:
            return getattr(o, "comment", "") == cid and getattr(o, "magic", magic) == magic

        for o in mt5.orders_get() or ():
            if mine(o):
                return BrokerOrderState(client_order_id, str(getattr(o, "order", getattr(o, "ticket", ""))),
                                        getattr(o, "symbol", ""), OrderState.ACCEPTED,
                                        int(getattr(o, "volume_initial", getattr(o, "volume", 0))),
                                        int(getattr(o, "volume_initial", 0) - getattr(o, "volume_current", 0)),
                                        Decimal(str(getattr(o, "price_open", 0.0))), Decimal(str(getattr(o, "sl", 0.0))),
                                        comment=cid)
        deals = mt5.history_deals_get(datetime.now(UTC) - timedelta(days=3), datetime.now(UTC) + timedelta(days=1)) or ()
        filled = [d for d in deals if mine(d)]
        if filled:
            qty = int(sum(getattr(d, "volume", 0) for d in filled))
            px = sum(getattr(d, "price", 0.0) * getattr(d, "volume", 0) for d in filled) / max(qty, 1)
            return BrokerOrderState(client_order_id, str(getattr(filled[0], "order", "")), getattr(filled[0], "symbol", ""),
                                    OrderState.FILLED, qty, qty, Decimal(str(round(px, 4))), None, comment=cid)
        return None

    def _reconcile_after_loss(self, intent: OrderIntent) -> BrokerOrderState | None:
        for delay in RECONCILE_DELAYS_S:
            self._sleep(delay)
            st = self.find_by_client_id(intent.client_order_id)
            if st is not None:
                return st
        return None

    # ------------------------------------------------------------------ submit
    def _request(self, intent: OrderIntent, price: float) -> dict[str, Any]:
        mt5 = self.mt5
        side_buy = intent.side == "BUY"
        if intent.order_type == "MARKET":
            action, otype = self._c("TRADE_ACTION_DEAL"), self._c("ORDER_TYPE_BUY" if side_buy else "ORDER_TYPE_SELL")
        elif intent.order_type == "LIMIT":
            action, otype = self._c("TRADE_ACTION_PENDING"), self._c("ORDER_TYPE_BUY_LIMIT" if side_buy else "ORDER_TYPE_SELL_LIMIT")
        elif intent.order_type == "STOP":
            action, otype = self._c("TRADE_ACTION_PENDING"), self._c("ORDER_TYPE_BUY_STOP" if side_buy else "ORDER_TYPE_SELL_STOP")
        elif intent.order_type == "STOP_LIMIT":
            action = self._c("TRADE_ACTION_PENDING")
            otype = self._c("ORDER_TYPE_BUY_STOP_LIMIT" if side_buy else "ORDER_TYPE_SELL_STOP_LIMIT")
        else:
            raise ConfigError(f"Unsupported order type {intent.order_type}")
        sym = mt5.symbol_info(intent.ticker)
        filling = self._filling_for(sym)
        req = {
            "action": action, "symbol": intent.ticker, "volume": float(intent.qty), "type": otype, "price": price,
            "sl": float(intent.stop_loss) if intent.stop_loss else 0.0,
            "tp": float(intent.take_profit) if intent.take_profit else 0.0, "deviation": 20,
            "magic": self.settings.execution.magic_number, "comment": intent.client_order_id[:31],
            # pending orders valid only for the session (buy-stop T+1, auction protection limit)
            "type_time": self._c("ORDER_TIME_DAY") if action == self._c("TRADE_ACTION_PENDING") else self._c("ORDER_TIME_GTC"),
            "type_filling": filling,
        }
        if intent.order_type == "STOP_LIMIT":
            req["stoplimit"] = float(intent.limit_price) if intent.limit_price else price
        return req

    @staticmethod
    def audit_entry(intent: OrderIntent) -> None:
        """RF-02: every BUY carries a server-side stop and its worst-case loss fits the risk budget.

        Worst case = the highest price the order can fill at (LIMIT price, or the STOP-LIMIT cap) minus the stop,
        times the quantity. Raises OrderRejectedError (logged by the caller) — never sends a naked entry.
        """
        if intent.side != "BUY":
            return
        if intent.stop_loss is None or intent.stop_loss <= 0:
            raise OrderRejectedError(f"{intent.client_order_id}: compra sem stop loss não é enviada")
        if intent.order_type in ("MARKET", "STOP"):
            raise OrderRejectedError(f"{intent.client_order_id}: compra precisa de preço máximo (LIMIT ou STOP_LIMIT)")
        worst = intent.limit_price if intent.order_type == "STOP_LIMIT" else intent.price
        if worst is None or worst <= intent.stop_loss:
            raise OrderRejectedError(f"{intent.client_order_id}: preço máximo ausente ou abaixo do stop")
        if intent.take_profit is not None and intent.take_profit <= worst:
            raise OrderRejectedError(f"{intent.client_order_id}: alvo abaixo do preço de entrada")
        if intent.risk_budget is not None:
            worst_loss = (worst - intent.stop_loss) * intent.qty
            if worst_loss > intent.risk_budget + Decimal("0.01"):
                raise OrderRejectedError(
                    f"{intent.client_order_id}: risco no pior preço R$ {worst_loss:.2f} > limite R$ {intent.risk_budget:.2f}"
                )

    def _filling_for(self, sym: Any) -> int:
        """Derive type_filling from the symbol's filling_mode flags (PRD 1.3.1, retcode 10030)."""
        flags = int(getattr(sym, "filling_mode", 0) or 0)
        if flags & 1:  # SYMBOL_FILLING_FOK
            return int(self._c("ORDER_FILLING_FOK"))
        if flags & 2:  # SYMBOL_FILLING_IOC
            return int(self._c("ORDER_FILLING_IOC"))
        return int(self._c("ORDER_FILLING_RETURN"))  # exchange execution (B3) default

    def submit(self, intent: OrderIntent) -> SubmitResult:
        if self.settings.execution.mode != "live":
            raise OrderRejectedError(
                f"Cannot execute live order when execution.mode='{self.settings.execution.mode}'. "
                "Mode must be formally changed to 'live' after the Go report is approved."
            )
        mt5 = self._api()
        if intent.ticker in self.blocked_tickers:
            raise OrderStateUnknownError(f"{intent.ticker} blocked: an order is in UNKNOWN state")
        term = mt5.terminal_info()
        if term is None or not getattr(term, "trade_allowed", False):
            raise TradingDisabledError("AutoTrading is disabled on the MT5 terminal")

        self.audit_entry(intent)
        existing = self.find_by_client_id(intent.client_order_id)
        if existing is not None:
            return SubmitResult(True, intent.client_order_id, existing.broker_order_id, existing.state,
                                C.TRADE_RETCODE_PLACED, "Already at broker (idempotent)")

        price = float(intent.price) if intent.price is not None else self._market_price(intent)
        resends = 0
        busy_retry_done = False
        while True:
            request = self._request(intent, price)
            chk = mt5.order_check(request)
            if chk is None or getattr(chk, "retcode", -1) not in (0, C.TRADE_RETCODE_DONE, C.TRADE_RETCODE_PLACED):
                raise OrderRejectedError(
                    f"order_check failed for {intent.client_order_id}: "
                    f"{getattr(chk, 'retcode', -1)} - {getattr(chk, 'comment', 'no response')}"
                )
            res = mt5.order_send(request)
            if res is None:
                st = self._reconcile_after_loss(intent)
                if st is not None:
                    return SubmitResult(True, intent.client_order_id, st.broker_order_id, st.state, -1,
                                        "Response lost; found at broker after reconciliation")
                self.blocked_tickers.add(intent.ticker)
                raise OrderStateUnknownError(
                    f"Response lost for {intent.client_order_id} and not found after reconciliation: UNKNOWN"
                )
            rc = int(getattr(res, "retcode", -1))
            oid = str(getattr(res, "order", "") or "")
            if rc in (C.TRADE_RETCODE_DONE, C.TRADE_RETCODE_PLACED):
                state = OrderState.FILLED if rc == C.TRADE_RETCODE_DONE else OrderState.ACCEPTED
                return SubmitResult(True, intent.client_order_id, oid, state, rc, getattr(res, "comment", "OK"))
            if rc == C.TRADE_RETCODE_DONE_PARTIAL:
                return SubmitResult(True, intent.client_order_id, oid, OrderState.PARTIALLY_FILLED, rc, "Partial fill")
            if rc == C.TRADE_RETCODE_TRADE_DISABLED:
                raise TradingDisabledError("AutoTrading is disabled on the MT5 terminal (10027)")
            if rc in RETRYABLE_PRICE and resends < 2:
                if self.find_by_client_id(intent.client_order_id) is not None:
                    st2 = self.find_by_client_id(intent.client_order_id)
                    assert st2 is not None
                    return SubmitResult(True, intent.client_order_id, st2.broker_order_id, st2.state, rc, "Found after requote")
                resends += 1
                if intent.order_type == "MARKET":
                    price = self._market_price(intent)
                continue
            if rc == C.TRADE_RETCODE_TOO_MANY_REQUESTS and not busy_retry_done:
                busy_retry_done = True
                self._sleep(5.0)
                if self.find_by_client_id(intent.client_order_id) is None:
                    continue
            if rc in CONFIG_REJECT:
                raise ConfigError(f"Order {intent.client_order_id} rejected by configuration error {rc}: {getattr(res, 'comment', '')}")
            raise OrderRejectedError(f"Order {intent.client_order_id} rejected with code {rc}: {getattr(res, 'comment', 'Rejected')}")

    def _market_price(self, intent: OrderIntent) -> float:
        tick = self.mt5.symbol_info_tick(intent.ticker)
        if tick is None:
            raise OrderRejectedError(f"No quote for {intent.ticker}")
        return float(tick.ask if intent.side == "BUY" else tick.bid)

    def set_stop(self, ticker: str, position_ticket: int, stop: float) -> None:
        """Move the server-side stop of a position (trailing / break-even). Netting account: one per symbol."""
        mt5 = self._api()
        req = {"action": self._c("TRADE_ACTION_SLTP"), "symbol": ticker, "position": position_ticket, "sl": float(stop), "tp": 0.0,
               "magic": self.settings.execution.magic_number}
        res = mt5.order_send(req)
        if res is None or int(getattr(res, "retcode", -1)) not in (C.TRADE_RETCODE_DONE, C.TRADE_RETCODE_PLACED):
            raise OrderRejectedError(f"SLTP update failed for {ticker}: {getattr(res, 'retcode', None)}")

    def cancel(self, client_order_id: str) -> None:
        mt5 = self._api()
        cid = client_order_id[:31]
        for o in mt5.orders_get() or ():
            if getattr(o, "comment", "") == cid:
                ticket = getattr(o, "order", getattr(o, "ticket", None))
                if ticket:
                    mt5.order_send({"action": self._c("TRADE_ACTION_REMOVE"), "order": ticket})

    def open_positions(self) -> Sequence[BrokerPosition]:
        mt5 = self._api()
        out: list[BrokerPosition] = []
        for p in mt5.positions_get() or ():
            out.append(BrokerPosition(
                position_id=str(getattr(p, "ticket", getattr(p, "order", ""))), ticker=getattr(p, "symbol", ""),
                qty=int(getattr(p, "volume", 0)), entry_price=Decimal(str(getattr(p, "price_open", 0.0))),
                current_price=Decimal(str(getattr(p, "price_current", getattr(p, "price_open", 0.0)))),
                current_stop=Decimal(str(getattr(p, "sl", 0.0))), target_price=Decimal(str(getattr(p, "tp", 0.0))),
                ts_utc=datetime.now(UTC),
            ))
        return out

    def fills_since(self, ts_utc: datetime) -> Sequence[Fill]:
        mt5 = self._api()
        out: list[Fill] = []
        magic = self.settings.execution.magic_number
        for d in mt5.history_deals_get(ts_utc, datetime.now(UTC) + timedelta(days=1)) or ():
            if getattr(d, "magic", magic) != magic:
                continue
            out.append(Fill(
                client_order_id=getattr(d, "comment", ""), broker_deal_id=str(getattr(d, "deal", getattr(d, "ticket", ""))),
                ts_utc=datetime.fromtimestamp(int(getattr(d, "time", 0)), UTC), qty=int(getattr(d, "volume", 0)),
                price=Decimal(str(getattr(d, "price", 0.0))),
                fees=Decimal(str(abs(getattr(d, "fee", 0.0)) + abs(getattr(d, "commission", 0.0)))),
            ))
        return out
