"""Order lifecycle state machine enforcing strict transitions and audit trails."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ts.core.errors import OrderStateError
from ts.core.models import OrderState

VALID_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.PLANNED: {OrderState.SUBMITTED, OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.SUBMITTED: {
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
        OrderState.UNKNOWN,
    },
    OrderState.ACCEPTED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
        OrderState.UNKNOWN,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.UNKNOWN,
    },
    # UNKNOWN can only transition out via explicit reconciliation
    OrderState.UNKNOWN: {
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.REJECTED,
    },
    # Terminal states
    OrderState.FILLED: set(),
    OrderState.CANCELLED: set(),
    OrderState.EXPIRED: set(),
    OrderState.REJECTED: set(),
}


@dataclass
class OrderStateMachine:
    client_order_id: str
    current_state: OrderState = OrderState.PLANNED
    history: list[tuple[OrderState, datetime, str | None]] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.history is None:
            now = datetime.now(UTC)
            self.history = [(self.current_state, now, "Initial planned state")]

    def transition_to(self, new_state: OrderState, reason: str | None = None) -> None:
        """Attempt state transition, raising OrderStateError on illegal paths."""
        allowed = VALID_TRANSITIONS.get(self.current_state, set())
        if new_state not in allowed:
            raise OrderStateError(
                f"Illegal order state transition for '{self.client_order_id}': "
                f"from {self.current_state.value} to {new_state.value}"
            )

        self.current_state = new_state
        self.history.append((new_state, datetime.now(UTC), reason))

    def is_terminal(self) -> bool:
        return self.current_state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
        )
