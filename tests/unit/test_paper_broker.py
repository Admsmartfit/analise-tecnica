"""Unit tests for PaperBroker — submit, fill, idempotency, partial sell, cost calculation."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from ts.core.models import OrderIntent, OrderState
from ts.execution.paper_broker import PaperBroker


def _make_intent(
    cid: str = "TEST-001",
    ticker: str = "PETR4",
    side: str = "BUY",
    qty: int = 100,
    price: float = 30.0,
    stop: float = 28.0,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid,
        signal_id=None,
        ticker=ticker,
        side=side,
        qty=qty,
        order_type="MARKET",
        price=Decimal(str(price)),
        stop_loss=Decimal(str(stop)),
        valid_until=date(2026, 12, 31),
        reason="unit test",
    )


class TestPaperBrokerSubmit:
    def test_submit_returns_accepted_state(self) -> None:
        broker = PaperBroker(initial_cash=200_000.0)
        intent = _make_intent()
        result = broker.submit(intent)
        assert result.success is True
        assert result.state == OrderState.ACCEPTED
        assert result.broker_order_id is not None

    def test_submit_idempotent_on_duplicate(self) -> None:
        broker = PaperBroker(initial_cash=200_000.0)
        intent = _make_intent()
        r1 = broker.submit(intent)
        r2 = broker.submit(intent)
        assert r1.broker_order_id == r2.broker_order_id
        assert r2.message is not None and "Idempotent" in r2.message

    def test_find_by_client_id_after_submit(self) -> None:
        broker = PaperBroker(initial_cash=200_000.0)
        intent = _make_intent("ORD-XYZ")
        broker.submit(intent)
        state = broker.find_by_client_id("ORD-XYZ")
        assert state is not None
        assert state.ticker == "PETR4"
        assert state.state == OrderState.ACCEPTED


class TestPaperBrokerFill:
    def test_simulate_fill_updates_cash_and_position(self) -> None:
        broker = PaperBroker(initial_cash=200_000.0)
        intent = _make_intent(qty=100, price=30.0)
        broker.submit(intent)

        fill = broker.simulate_fill("TEST-001", Decimal("30.00"))
        assert fill is not None
        assert fill.qty == 100
        # Cash should decrease by trade value + fees
        assert float(broker.cash) < 200_000.0 - (100 * 30.0 - 1.0)  # at minimum trade cost

        positions = broker.open_positions()
        assert len(positions) == 1
        assert positions[0].ticker == "PETR4"
        assert positions[0].qty == 100

    def test_simulate_fill_calculates_b3_costs(self) -> None:
        broker = PaperBroker(initial_cash=500_000.0)
        intent = _make_intent(qty=1000, price=50.0)  # R$ 50,000 trade
        broker.submit(intent)

        fill = broker.simulate_fill("TEST-001", Decimal("50.00"))
        assert fill is not None
        # B3 emoluments ~0.03% = R$ 15 on a R$ 50k trade
        assert float(fill.fees) > 10.0  # at least emoluments + brokerage

    def test_partial_sell_reduces_position(self) -> None:
        broker = PaperBroker(initial_cash=200_000.0)
        buy = _make_intent("BUY-001", qty=200, price=30.0)
        broker.submit(buy)
        broker.simulate_fill("BUY-001", Decimal("30.00"))

        sell_partial = _make_intent("SELL-001", side="SELL", qty=100, price=31.0)
        broker.submit(sell_partial)
        broker.simulate_fill("SELL-001", Decimal("31.00"))

        positions = broker.open_positions()
        assert len(positions) == 1
        assert positions[0].qty == 100  # 200 - 100

    def test_full_sell_closes_position(self) -> None:
        broker = PaperBroker(initial_cash=200_000.0)
        buy = _make_intent("BUY-002", qty=100, price=30.0)
        broker.submit(buy)
        broker.simulate_fill("BUY-002", Decimal("30.00"))

        sell = _make_intent("SELL-002", side="SELL", qty=100, price=33.0)
        broker.submit(sell)
        broker.simulate_fill("SELL-002", Decimal("33.00"))

        positions = broker.open_positions()
        assert len(positions) == 0

    def test_cancel_prevents_fill(self) -> None:
        broker = PaperBroker(initial_cash=200_000.0)
        intent = _make_intent("ORD-CANCEL")
        broker.submit(intent)
        broker.cancel("ORD-CANCEL")

        # Cancelled order: simulate_fill should return None
        fill = broker.simulate_fill("ORD-CANCEL", Decimal("30.00"))
        assert fill is None

    def test_fills_since_filters_by_time(self) -> None:
        broker = PaperBroker(initial_cash=500_000.0)
        intent = _make_intent("FIL-001")
        broker.submit(intent)
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        broker.simulate_fill("FIL-001", Decimal("30.00"))

        fills = broker.fills_since(old_ts)
        assert len(fills) == 1

        future_ts = datetime(2030, 1, 1, tzinfo=UTC)
        fills_future = broker.fills_since(future_ts)
        assert len(fills_future) == 0
