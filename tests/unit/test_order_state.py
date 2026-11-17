"""Unit tests for OrderStateMachine — valid transitions, illegal transitions, terminal detection."""
from __future__ import annotations

import pytest

from ts.core.errors import OrderStateError
from ts.core.models import OrderState
from ts.execution.order_state import VALID_TRANSITIONS, OrderStateMachine


class TestOrderStateMachineBasics:
    def test_initial_state_is_planned(self) -> None:
        sm = OrderStateMachine(client_order_id="ORD-001")
        assert sm.current_state == OrderState.PLANNED

    def test_history_has_initial_entry(self) -> None:
        sm = OrderStateMachine(client_order_id="ORD-001")
        assert len(sm.history) == 1
        state, ts, reason = sm.history[0]
        assert state == OrderState.PLANNED

    def test_valid_planned_to_submitted(self) -> None:
        sm = OrderStateMachine(client_order_id="ORD-001")
        sm.transition_to(OrderState.SUBMITTED, reason="Sent to broker")
        assert sm.current_state == OrderState.SUBMITTED
        assert len(sm.history) == 2

    def test_valid_full_happy_path(self) -> None:
        sm = OrderStateMachine(client_order_id="ORD-002")
        sm.transition_to(OrderState.SUBMITTED)
        sm.transition_to(OrderState.ACCEPTED)
        sm.transition_to(OrderState.FILLED)
        assert sm.current_state == OrderState.FILLED
        assert sm.is_terminal()

    def test_valid_partial_fill_then_filled(self) -> None:
        sm = OrderStateMachine(client_order_id="ORD-003")
        sm.transition_to(OrderState.SUBMITTED)
        sm.transition_to(OrderState.ACCEPTED)
        sm.transition_to(OrderState.PARTIALLY_FILLED)
        sm.transition_to(OrderState.FILLED)
        assert sm.current_state == OrderState.FILLED

    def test_valid_planned_to_cancelled(self) -> None:
        sm = OrderStateMachine(client_order_id="ORD-004")
        sm.transition_to(OrderState.CANCELLED)
        assert sm.current_state == OrderState.CANCELLED
        assert sm.is_terminal()


class TestOrderStateMachineIllegalTransitions:
    def test_planned_to_filled_raises(self) -> None:
        sm = OrderStateMachine(client_order_id="BAD-001")
        with pytest.raises(OrderStateError, match="Illegal order state transition"):
            sm.transition_to(OrderState.FILLED)

    def test_filled_to_cancelled_raises(self) -> None:
        sm = OrderStateMachine(client_order_id="BAD-002")
        sm.transition_to(OrderState.SUBMITTED)
        sm.transition_to(OrderState.ACCEPTED)
        sm.transition_to(OrderState.FILLED)
        with pytest.raises(OrderStateError):
            sm.transition_to(OrderState.CANCELLED)

    def test_rejected_to_submitted_raises(self) -> None:
        sm = OrderStateMachine(client_order_id="BAD-003")
        sm.transition_to(OrderState.SUBMITTED)
        sm.transition_to(OrderState.REJECTED)
        with pytest.raises(OrderStateError):
            sm.transition_to(OrderState.SUBMITTED)

    def test_cancelled_is_terminal(self) -> None:
        sm = OrderStateMachine(client_order_id="BAD-004")
        sm.transition_to(OrderState.CANCELLED)
        assert sm.is_terminal()
        with pytest.raises(OrderStateError):
            sm.transition_to(OrderState.SUBMITTED)

    def test_unknown_can_be_reconciled_to_filled(self) -> None:
        """UNKNOWN → FILLED is a valid reconciliation path."""
        sm = OrderStateMachine(client_order_id="RECON-001")
        sm.transition_to(OrderState.SUBMITTED)
        sm.transition_to(OrderState.UNKNOWN)
        # Reconciliation resolves it
        sm.transition_to(OrderState.FILLED)
        assert sm.current_state == OrderState.FILLED

    def test_error_message_contains_states(self) -> None:
        sm = OrderStateMachine(client_order_id="ERR-001")
        with pytest.raises(OrderStateError) as exc_info:
            sm.transition_to(OrderState.EXPIRED)
        assert "PLANNED" in str(exc_info.value)
        assert "EXPIRED" in str(exc_info.value)


class TestValidTransitionsCompleteness:
    def test_all_states_have_entries(self) -> None:
        """Every OrderState must appear as a key in VALID_TRANSITIONS."""
        for state in OrderState:
            assert state in VALID_TRANSITIONS, f"Missing VALID_TRANSITIONS entry for {state}"

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        terminals = {OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.REJECTED}
        for state in terminals:
            assert VALID_TRANSITIONS[state] == set(), f"{state} should have no outgoing transitions"
