"""Deterministic pivot (peak/valley) detector — PRD 1.1.2.

Rules:
1. Candidate valley at i: ``L[i] < min(L[i-n..i-1])`` (strict left) and ``L[i] <= min(L[i+1..i+n])``
   (non-strict right). Ties inside the window: the oldest bar wins. Symmetric for peaks.
2. Confirmation: the pivot only exists from bar ``c = i + n`` on.
3. Magnitude: accepted only if ``|price - last opposite pivot| >= k_atr * ATR14[c]`` (ATR at confirmation).
   While ATR is still warming up no pivot is accepted (fail safe).
4. Alternation: two valleys in a row keep the lower one (tie: the older); two peaks keep the higher.

Why an *event log*: rule 4 replaces an already-confirmed pivot when a better one is confirmed later. If a
consumer filtered the *final* pivot list by ``confirmed_date <= t`` it would not see the pivot that was
valid at ``t`` (it was replaced afterwards) — that is lookahead. Every acceptance is therefore an
append-only event, and :meth:`PivotHistory.asof` replays events up to ``t`` to rebuild exactly the state a
live run would have had on ``t`` (the prefix test).
"""
from __future__ import annotations

import bisect
from collections.abc import Sequence
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from ts.core.models import PivotEvent


class PivotTracker:
    """Incremental, streaming detector of confirmed pivots (one call per closed bar)."""

    def __init__(
        self,
        ticker: str,
        window_n: int = 2,
        magnitude_atr_k: float = 1.0,
        atr_period: int = 14,
    ) -> None:
        self.ticker = ticker
        self.window_n = window_n
        self.magnitude_atr_k = magnitude_atr_k
        self.atr_period = atr_period

        self.history: list[dict[str, float | date | None]] = []
        self.confirmed_pivots: list[PivotEvent] = []  # current state (after alternation)
        self.events: list[PivotEvent] = []  # append-only log of every accepted pivot
        self._tr_seed: list[float] = []
        self._atr: float | None = None

    def _update_atr(self, high: float, low: float) -> float | None:
        """Wilder ATR identical to ``ta.atr``: TR from the 2nd bar, SMA seed, then RMA."""
        if not self.history:
            return None
        prev_close = float(self.history[-1]["close"])  # type: ignore[arg-type]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        if self._atr is None:
            self._tr_seed.append(tr)
            if len(self._tr_seed) == self.atr_period:
                self._atr = float(np.mean(self._tr_seed))
        else:
            self._atr = self._atr + (tr - self._atr) / self.atr_period
        return self._atr

    def update(
        self,
        session_date: date,
        open_p: float,
        high: float,
        low: float,
        close: float,
        current_atr: float | None = None,
    ) -> list[PivotEvent]:
        """Process a new closed bar and return the pivot events accepted on this bar."""
        calc_atr = self._update_atr(high, low)
        if current_atr is not None and not np.isnan(current_atr):
            calc_atr = current_atr

        self.history.append(
            {"date": session_date, "open": open_p, "high": high, "low": low, "close": close, "atr": calc_atr}
        )

        c = len(self.history) - 1
        n = self.window_n
        i = c - n
        if i < n or calc_atr is None:
            return []

        cand = self.history[i]
        left = self.history[i - n : i]
        right = self.history[i + 1 : i + n + 1]
        newly: list[PivotEvent] = []

        cand_low = float(cand["low"])  # type: ignore[arg-type]
        cand_high = float(cand["high"])  # type: ignore[arg-type]
        min_dist = self.magnitude_atr_k * calc_atr

        is_valley = all(cand_low < float(b["low"]) for b in left) and all(  # type: ignore[arg-type]
            cand_low <= float(b["low"]) for b in right  # type: ignore[arg-type]
        )
        if is_valley:
            last_high = self._last_pivot_of_kind("HIGH")
            if last_high is None or abs(last_high.price - cand_low) >= min_dist:
                self._accept(self._event("LOW", cand, cand_low, session_date, calc_atr, i, c), newly)

        is_peak = all(cand_high > float(b["high"]) for b in left) and all(  # type: ignore[arg-type]
            cand_high >= float(b["high"]) for b in right  # type: ignore[arg-type]
        )
        if is_peak:
            last_low = self._last_pivot_of_kind("LOW")
            if last_low is None or abs(cand_high - last_low.price) >= min_dist:
                self._accept(self._event("HIGH", cand, cand_high, session_date, calc_atr, i, c), newly)

        return newly

    def _event(
        self, kind: Literal["HIGH", "LOW"], cand: dict, price: float, confirm: date, atr_v: float, i: int, c: int
    ) -> PivotEvent:
        return PivotEvent(
            ticker=self.ticker,
            kind=kind,
            pivot_date=cand["date"],
            confirmed_date=confirm,
            price=price,
            atr_at_confirm=atr_v,
            pivot_idx=i,
            confirmed_idx=c,
        )

    def _last_pivot_of_kind(self, kind: Literal["HIGH", "LOW"]) -> PivotEvent | None:
        for p in reversed(self.confirmed_pivots):
            if p.kind == kind:
                return p
        return None

    def _accept(self, ev: PivotEvent, newly: list[PivotEvent]) -> None:
        if apply_event(self.confirmed_pivots, ev):
            self.events.append(ev)
            newly.append(ev)

    def get_confirmed_pivots_asof(self, asof: date) -> list[PivotEvent]:
        """State that a live run would have seen at the close of ``asof``."""
        return replay(self.events, asof)


def apply_event(state: list[PivotEvent], ev: PivotEvent) -> bool:
    """Apply the alternation rule in place. Returns True if the event changed the state."""
    if not state or state[-1].kind != ev.kind:
        state.append(ev)
        return True
    last = state[-1]
    better = ev.price < last.price if ev.kind == "LOW" else ev.price > last.price
    if better:  # tie keeps the older pivot
        state[-1] = ev
        return True
    return False


def replay(events: Sequence[PivotEvent], asof: date) -> list[PivotEvent]:
    state: list[PivotEvent] = []
    for ev in events:
        if ev.confirmed_date > asof:
            break
        apply_event(state, ev)
    return state


class PivotHistory:
    """Point-in-time pivot state with O(log n) lookup (used by the backtest feature cache)."""

    def __init__(self, events: Sequence[PivotEvent]) -> None:
        self.events = list(events)
        self._dates = [e.confirmed_date for e in self.events]
        self._states: list[tuple[PivotEvent, ...]] = []
        state: list[PivotEvent] = []
        for ev in self.events:
            apply_event(state, ev)
            self._states.append(tuple(state))

    def asof(self, d: date) -> tuple[PivotEvent, ...]:
        k = bisect.bisect_right(self._dates, d)
        return self._states[k - 1] if k > 0 else ()


def detect_pivot_events(
    df: pd.DataFrame,
    ticker: str,
    window_n: int = 2,
    magnitude_atr_k: float = 1.0,
    atr_period: int = 14,
) -> list[PivotEvent]:
    """Run the streaming tracker over ``df`` (sorted) and return the append-only event log."""
    tracker = PivotTracker(ticker, window_n=window_n, magnitude_atr_k=magnitude_atr_k, atr_period=atr_period)
    clean = df.sort_values(by="session_date").reset_index(drop=True)
    for d, o, h, lo, c in zip(
        clean["session_date"], clean["open"].astype(float), clean["high"].astype(float),
        clean["low"].astype(float), clean["close"].astype(float), strict=True,
    ):
        tracker.update(d, o, h, lo, c)
    return tracker.events


def detect_pivots(
    df: pd.DataFrame,
    ticker: str,
    window_n: int = 2,
    magnitude_atr_k: float = 1.0,
    atr_period: int = 14,
) -> list[PivotEvent]:
    """Final pivot state after processing all of ``df`` (what a live run sees at the last bar)."""
    events = detect_pivot_events(df, ticker, window_n, magnitude_atr_k, atr_period)
    state: list[PivotEvent] = []
    for ev in events:
        apply_event(state, ev)
    return state
