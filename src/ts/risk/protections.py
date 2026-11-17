"""Risk protections (PRD 1.2.5): per-asset cooldown, per-setup statistical monitor, circuit breaker with shadow curve."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np


def _business_days_between(a: date, b: date) -> int:
    """Weekdays in (a, b]; fallback when no B3 calendar is supplied."""
    return int(np.busday_count(a + timedelta(days=1), b + timedelta(days=1)))


@dataclass
class AssetCooldownTracker:
    """2 stops on the same asset within 30 sessions pause it for 10 sessions (counted in sessions)."""

    cooldown_stops_count: int = 2
    lookback_days: int = 30  # sessions
    pause_days: int = 10  # sessions
    stop_dates_by_ticker: dict[str, list[date]] = field(default_factory=dict)
    paused_from_by_ticker: dict[str, date] = field(default_factory=dict)
    sessions_between: Callable[[date, date], int] | None = None

    # kept for backward compatibility with callers that read it
    @property
    def paused_until_by_ticker(self) -> dict[str, date]:
        return dict(self.paused_from_by_ticker)

    def _sessions(self, a: date, b: date) -> int:
        if self.sessions_between is not None:
            return self.sessions_between(a, b)
        return _business_days_between(a, b)

    def record_stop(self, ticker: str, stop_date: date) -> bool:
        """Record a stop-loss exit. Returns True if the ticker entered cooldown."""
        lst = self.stop_dates_by_ticker.setdefault(ticker, [])
        lst.append(stop_date)
        recent = [d for d in lst if self._sessions(d, stop_date) < self.lookback_days]
        self.stop_dates_by_ticker[ticker] = recent
        if len(recent) >= self.cooldown_stops_count:
            self.paused_from_by_ticker[ticker] = stop_date
            self.stop_dates_by_ticker[ticker] = []
            return True
        return False

    def is_paused(self, ticker: str, asof: date) -> bool:
        start = self.paused_from_by_ticker.get(ticker)
        if start is None:
            return False
        return self._sessions(start, asof) <= self.pause_days


def bootstrap_mean_threshold(reference_r: Sequence[float], window: int, quantile: float,
                             n_boot: int = 2000, seed: int = 42) -> float:
    """``quantile`` of the distribution of the mean of ``window`` trades resampled from the backtest."""
    arr = np.asarray(reference_r, dtype=float)
    rng = np.random.default_rng(seed)
    means = rng.choice(arr, size=(n_boot, window), replace=True).mean(axis=1)
    return float(np.quantile(means, quantile))


class SetupStatisticalMonitor:
    """Suspends a setup when live R degrades vs the backtest (PRD 1.2.5, item 2).

    * Mean of the last ``window`` trades below the ``bootstrap_quantile`` of 30-trade means drawn from the
      backtest distribution of that setup (``reference_r``).
    * One-sided lower CUSUM on R (target = backtest mean) to detect a downward shift earlier.

    Without a reference distribution the monitor only suspends when the recent mean is significantly
    negative (upper bootstrap bound of the recent mean < 0) — conservative fallback, never silent.
    """

    def __init__(
        self,
        window: int = 30,
        bootstrap_quantile: float = 0.05,
        reference_r: dict[str, Sequence[float]] | None = None,
        cusum_k_sigma: float = 0.5,
        cusum_h_sigma: float = 5.0,
    ) -> None:
        self.window = window
        self.bootstrap_quantile = bootstrap_quantile
        self.reference_r = {k: list(v) for k, v in (reference_r or {}).items()}
        self.cusum_k_sigma = cusum_k_sigma
        self.cusum_h_sigma = cusum_h_sigma
        self.trades_r_by_setup: dict[str, list[float]] = {}
        self.suspended_setups: dict[str, str] = {}
        self._cusum: dict[str, float] = {}
        self._thresholds: dict[str, float] = {}

    def _threshold(self, setup_id: str) -> float | None:
        ref = self.reference_r.get(setup_id)
        if not ref or len(ref) < 10:
            return None
        if setup_id not in self._thresholds:
            self._thresholds[setup_id] = bootstrap_mean_threshold(ref, self.window, self.bootstrap_quantile)
        return self._thresholds[setup_id]

    def record_trade(self, setup_id: str, r_multiple: float) -> bool:
        """Record a closed trade's R. Returns True if the setup became suspended on this trade."""
        hist = self.trades_r_by_setup.setdefault(setup_id, [])
        hist.append(float(r_multiple))
        if setup_id in self.suspended_setups:
            return False

        ref = self.reference_r.get(setup_id)
        if ref and len(ref) >= 10:
            mu, sigma = float(np.mean(ref)), float(np.std(ref)) or 1.0
            s = min(0.0, self._cusum.get(setup_id, 0.0) + (r_multiple - mu) + self.cusum_k_sigma * sigma)
            self._cusum[setup_id] = s
            if s < -self.cusum_h_sigma * sigma:
                self.suspended_setups[setup_id] = f"CUSUM {s:.2f} < -{self.cusum_h_sigma}σ"
                return True

        recent = hist[-self.window:]
        if len(recent) < self.window:
            return False
        mean_r = float(np.mean(recent))
        thr = self._threshold(setup_id)
        if thr is not None:
            if mean_r < thr:
                self.suspended_setups[setup_id] = f"mean R {mean_r:.3f} < backtest P{self.bootstrap_quantile * 100:.0f} {thr:.3f}"
                return True
            return False
        rng = np.random.default_rng(7)
        boot = rng.choice(np.asarray(recent), size=(2000, self.window), replace=True).mean(axis=1)
        if float(np.quantile(boot, 0.95)) < 0.0:
            self.suspended_setups[setup_id] = f"mean R {mean_r:.3f} significantly < 0 (no backtest reference)"
            return True
        return False

    def is_suspended(self, setup_id: str) -> bool:
        return setup_id in self.suspended_setups


class CircuitBreaker:
    """Drawdown circuit breaker with shadow-curve resumption (PRD 1.2.5, item 3).

    * DD >= level 1 (8%): risk per trade multiplied by ``level1_risk_multiplier``.
    * DD >= level 2 (12%): no new entries; signals keep running in a *shadow* book.
    * Resume when the shadow curve closes above its own SMA(``shadow_sma_period``) after at least that many
      sessions halted; the high-water mark resets to the current equity.
    """

    def __init__(
        self,
        dd_level1: float = 0.08,
        dd_level2: float = 0.12,
        shadow_sma_period: int = 20,
        initial_equity: float = 100000.0,
        level1_risk_multiplier: float = 0.5,
    ) -> None:
        self.dd_level1 = dd_level1
        self.dd_level2 = dd_level2
        self.shadow_sma_period = shadow_sma_period
        self.level1_risk_multiplier = level1_risk_multiplier
        self.peak_equity = initial_equity
        self.current_equity = initial_equity
        self.shadow_equity_curve: list[float] = [initial_equity]
        self.is_halted = False
        self.sessions_halted = 0
        self.drawdown = 0.0

    def update(self, equity: float, shadow_equity: float) -> None:
        """Feed today's real equity and shadow-curve value (real equity + shadow book P&L)."""
        self.current_equity = equity
        self.shadow_equity_curve.append(shadow_equity)
        if not self.is_halted and equity > self.peak_equity:
            self.peak_equity = equity
        self.drawdown = (self.peak_equity - equity) / self.peak_equity if self.peak_equity > 0 else 0.0

        if self.is_halted:
            self.sessions_halted += 1
            p = self.shadow_sma_period
            if self.sessions_halted >= p and len(self.shadow_equity_curve) >= p:
                sma = float(np.mean(self.shadow_equity_curve[-p:]))
                if shadow_equity > sma:
                    self.is_halted = False
                    self.sessions_halted = 0
                    self.peak_equity = equity
                    self.drawdown = 0.0
        elif self.drawdown >= self.dd_level2:
            self.is_halted = True
            self.sessions_halted = 0

    def update_equity(self, current_equity: float, shadow_delta: float = 0.0) -> None:
        """Backward-compatible API: shadow curve advanced by ``shadow_delta``."""
        self.update(current_equity, self.shadow_equity_curve[-1] + shadow_delta)

    def can_open_new_positions(self) -> bool:
        return not self.is_halted

    def risk_multiplier(self) -> float:
        if self.is_halted:
            return 0.0
        if self.drawdown >= self.dd_level1:
            return self.level1_risk_multiplier
        return 1.0

    @property
    def level(self) -> int:
        if self.is_halted:
            return 2
        return 1 if self.drawdown >= self.dd_level1 else 0
