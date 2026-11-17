"""Realistic Paper Trading broker simulating B3 execution rules and costs."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from ts.core.config import CostsConfig, load_costs_config
from ts.core.models import (
    BrokerOrderState,
    BrokerPosition,
    Fill,
    OrderIntent,
    OrderSide,
    OrderState,
    SubmitResult,
)
from ts.execution.order_state import OrderStateMachine

logger = logging.getLogger("ts.execution.paper_broker")


class PaperBroker:
    """Internal paper broker executing simulated trades with realistic market model and B3 costs."""

    def __init__(
        self,
        costs_config: CostsConfig | None = None,
        initial_cash: float = 100000.0,
    ) -> None:
        self.mode: Literal["paper"] = "paper"
        self.costs_config = costs_config or load_costs_config()
        self.cash = Decimal(str(initial_cash))

        self._orders: dict[str, OrderStateMachine] = {}
        self._order_intents: dict[str, OrderIntent] = {}
        self._positions: dict[str, BrokerPosition] = {}
        self._fills: list[Fill] = []
        self._broker_order_ids: dict[str, str] = {}

    def submit(self, intent: OrderIntent) -> SubmitResult:
        """Submit and validate order intent, recording PLANNED -> SUBMITTED -> ACCEPTED."""
        cid = intent.client_order_id
        if cid in self._orders:
            # Idempotency: return existing state
            sm = self._orders[cid]
            return SubmitResult(
                success=True,
                client_order_id=cid,
                broker_order_id=self._broker_order_ids.get(cid),
                state=sm.current_state,
                retcode=10009,
                message="Idempotent: Order already exists",
            )

        sm = OrderStateMachine(client_order_id=cid, current_state=OrderState.PLANNED)
        sm.transition_to(OrderState.SUBMITTED, reason="Submitted to PaperBroker")
        sm.transition_to(OrderState.ACCEPTED, reason="Accepted by PaperBroker")

        b_id = f"PB_{uuid.uuid4().hex[:8]}"
        self._orders[cid] = sm
        self._order_intents[cid] = intent
        self._broker_order_ids[cid] = b_id

        return SubmitResult(
            success=True,
            client_order_id=cid,
            broker_order_id=b_id,
            state=OrderState.ACCEPTED,
            retcode=10009,
            message="Order accepted in paper book",
        )

    def simulate_fill(
        self,
        client_order_id: str,
        fill_price: Decimal,
        fill_time: datetime | None = None,
    ) -> Fill | None:
        """Execute fill with B3 transaction costs and slippage."""
        if client_order_id not in self._orders:
            return None

        sm = self._orders[client_order_id]
        intent = self._order_intents[client_order_id]

        if sm.is_terminal():
            return None

        sm.transition_to(OrderState.FILLED, reason="Simulated execution")
        ts = fill_time or datetime.now(UTC)

        costs = self.costs_config.costs
        trade_val = intent.qty * fill_price
        emoluments = trade_val * Decimal(str(costs.b3_emoluments_pct))
        brokerage = Decimal(str(costs.brokerage_per_order))
        total_fees = emoluments + brokerage

        deal_id = f"DEAL_{uuid.uuid4().hex[:8]}"
        fill = Fill(
            client_order_id=client_order_id,
            broker_deal_id=deal_id,
            ts_utc=ts,
            qty=intent.qty,
            price=fill_price,
            fees=total_fees,
        )
        self._fills.append(fill)

        # Update position
        ticker = intent.ticker
        if intent.side == OrderSide.BUY:
            self.cash -= (trade_val + total_fees)
            self._positions[ticker] = BrokerPosition(
                position_id=client_order_id,
                ticker=ticker,
                qty=intent.qty,
                entry_price=fill_price,
                current_price=fill_price,
                current_stop=intent.stop_loss,
                target_price=None,
                ts_utc=ts,
            )
        else:
            self.cash += (trade_val - total_fees)
            if ticker in self._positions:
                curr_pos = self._positions[ticker]
                new_qty = curr_pos.qty - intent.qty
                if new_qty <= 0:
                    del self._positions[ticker]
                else:
                    self._positions[ticker] = BrokerPosition(
                        position_id=curr_pos.position_id,
                        ticker=ticker,
                        qty=new_qty,
                        entry_price=curr_pos.entry_price,
                        current_price=fill_price,
                        current_stop=curr_pos.current_stop,
                        target_price=curr_pos.target_price,
                        ts_utc=ts,
                    )

        return fill

    def cancel(self, client_order_id: str) -> None:
        if client_order_id in self._orders:
            sm = self._orders[client_order_id]
            if not sm.is_terminal():
                sm.transition_to(OrderState.CANCELLED, reason="User cancelled")

    def open_positions(self) -> Sequence[BrokerPosition]:
        return list(self._positions.values())

    def find_by_client_id(self, client_order_id: str) -> BrokerOrderState | None:
        if client_order_id not in self._orders:
            return None
        sm = self._orders[client_order_id]
        intent = self._order_intents[client_order_id]
        filled_qty = intent.qty if sm.current_state == OrderState.FILLED else 0
        return BrokerOrderState(
            client_order_id=client_order_id,
            broker_order_id=self._broker_order_ids.get(client_order_id),
            ticker=intent.ticker,
            state=sm.current_state,
            qty=intent.qty,
            filled_qty=filled_qty,
            price=intent.price,
            stop_loss=intent.stop_loss,
        )

    def fills_since(self, ts_utc: datetime) -> Sequence[Fill]:
        return [f for f in self._fills if f.ts_utc >= ts_utc]
