"""Unit tests for reconcile_positions — matched, orphan internal, orphan broker, qty mismatch."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ts.core.models import BrokerPosition
from ts.execution.reconcile import reconcile_positions


def _broker_pos(ticker: str, qty: int, entry: float = 30.0) -> BrokerPosition:
    return BrokerPosition(
        position_id=f"POS-{ticker}",
        ticker=ticker,
        qty=qty,
        entry_price=Decimal(str(entry)),
        current_price=Decimal(str(entry)),
        current_stop=None,
        target_price=None,
        ts_utc=datetime(2026, 11, 17, tzinfo=UTC),
    )


class TestReconcilePositions:
    def test_matched_positions_ok(self) -> None:
        internal = {"PETR4": {"qty": 100}, "VALE3": {"qty": 200}}
        broker = [_broker_pos("PETR4", 100), _broker_pos("VALE3", 200)]
        report = reconcile_positions(internal, broker)
        assert report.ok is True
        assert report.matched_count == 2
        assert len(report.divergences) == 0

    def test_empty_both_sides_ok(self) -> None:
        report = reconcile_positions({}, [])
        assert report.ok is True
        assert report.matched_count == 0

    def test_orphan_internal_detected(self) -> None:
        """Internal has PETR4 but broker has no position."""
        internal = {"PETR4": {"qty": 100}}
        broker: list[BrokerPosition] = []
        report = reconcile_positions(internal, broker)
        assert report.ok is False
        assert any(d.kind == "ORPHAN_INTERNAL" and d.ticker == "PETR4" for d in report.divergences)

    def test_orphan_broker_detected(self) -> None:
        """Broker has VALE3 but internal tracking has no position."""
        internal: dict = {}
        broker = [_broker_pos("VALE3", 300)]
        report = reconcile_positions(internal, broker)
        assert report.ok is False
        assert any(d.kind == "ORPHAN_BROKER" and d.ticker == "VALE3" for d in report.divergences)

    def test_qty_mismatch_detected(self) -> None:
        internal = {"MGLU3": {"qty": 100}}
        broker = [_broker_pos("MGLU3", 150)]  # Different qty
        report = reconcile_positions(internal, broker)
        assert report.ok is False
        mismatch = [d for d in report.divergences if d.kind == "QTY_MISMATCH"]
        assert len(mismatch) == 1
        assert "MGLU3" in mismatch[0].detail
        assert "100" in mismatch[0].detail
        assert "150" in mismatch[0].detail

    def test_multiple_divergences_all_reported(self) -> None:
        """Several problems at once — all are reported."""
        internal = {"PETR4": {"qty": 100}, "VALE3": {"qty": 200}}
        broker = [
            _broker_pos("PETR4", 50),   # qty mismatch
            _broker_pos("WEGE3", 300),  # orphan broker
            # VALE3 missing from broker → orphan internal
        ]
        report = reconcile_positions(internal, broker)
        assert report.ok is False
        kinds = {d.kind for d in report.divergences}
        assert "QTY_MISMATCH" in kinds
        assert "ORPHAN_BROKER" in kinds
        assert "ORPHAN_INTERNAL" in kinds
