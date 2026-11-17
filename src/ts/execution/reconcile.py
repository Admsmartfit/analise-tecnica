"""Daily reconciliation between system positions and broker accounts."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ts.core.models import BrokerPosition

logger = logging.getLogger("ts.execution.reconcile")


@dataclass
class Divergence:
    ticker: str
    kind: str  # ORPHAN_INTERNAL, ORPHAN_BROKER, QTY_MISMATCH, STOP_MISMATCH
    detail: str


@dataclass
class ReconciliationReport:
    ok: bool
    matched_count: int
    divergences: list[Divergence] = field(default_factory=list)


def reconcile_positions(
    internal_positions: dict[str, dict[str, Any]],
    broker_positions: Sequence[BrokerPosition],
) -> ReconciliationReport:
    """Compare internal tracking positions with live broker book to detect divergences."""
    broker_dict = {p.ticker: p for p in broker_positions}
    divergences: list[Divergence] = []
    matched = 0

    # 1. Check all internal positions
    for ticker, internal in internal_positions.items():
        if ticker not in broker_dict:
            divergences.append(
                Divergence(
                    ticker=ticker,
                    kind="ORPHAN_INTERNAL",
                    detail=f"Internal position exists for {ticker} ({internal['qty']} shares) but not found on broker",
                )
            )
        else:
            b_pos = broker_dict[ticker]
            int_qty = int(internal["qty"])
            if b_pos.qty != int_qty:
                divergences.append(
                    Divergence(
                        ticker=ticker,
                        kind="QTY_MISMATCH",
                        detail=f"Quantity mismatch for {ticker}: internal={int_qty}, broker={b_pos.qty}",
                    )
                )
            else:
                matched += 1

    # 2. Check for orphan broker positions
    for ticker, b_pos in broker_dict.items():
        if ticker not in internal_positions:
            divergences.append(
                Divergence(
                    ticker=ticker,
                    kind="ORPHAN_BROKER",
                    detail=f"Broker has open position for {ticker} ({b_pos.qty} shares) not tracked by internal system",
                )
            )

    is_ok = len(divergences) == 0
    if not is_ok:
        logger.error(f"Reconciliation failed with {len(divergences)} divergences!")
    else:
        logger.info(f"Reconciliation successful. {matched} positions verified.")

    return ReconciliationReport(
        ok=is_ok,
        matched_count=matched,
        divergences=divergences,
    )
