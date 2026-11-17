"""Broker interface protocol."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from ts.core.models import (
    BrokerOrderState,
    BrokerPosition,
    Fill,
    OrderIntent,
    SubmitResult,
)


class Broker(Protocol):
    """Abstract trading broker adapter."""

    mode: Literal["paper", "live"]

    def submit(self, intent: OrderIntent) -> SubmitResult:
        """Submit an order intent to the market/simulator."""
        ...

    def cancel(self, client_order_id: str) -> None:
        """Cancel an active order."""
        ...

    def open_positions(self) -> Sequence[BrokerPosition]:
        """Fetch all currently open positions."""
        ...

    def find_by_client_id(self, client_order_id: str) -> BrokerOrderState | None:
        """Locate order state by client idempotency id."""
        ...

    def fills_since(self, ts_utc: datetime) -> Sequence[Fill]:
        """Retrieve fills executed since timestamp."""
        ...
