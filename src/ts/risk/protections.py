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
        resume_trades: int = 0,
    ) -> None:
        self.window = window
        self.resume_trades = resume_trades
        self.probation_r: dict[str, list[float]] = {}  # shadow R of suspended setups (since suspension)
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

    def record_probation(self, setup_id: str, r_multiple: float) -> bool:
        """Shadow trade of a suspended setup. Returns True when the setup is resumed.

        Without this a suspension is absorbing: a suspended setup produces no trades, so it can never show that
        it recovered. Resume rule: the last ``resume_trades`` shadow trades have a positive mean (and, when a
        backtest reference exists, a mean above its bootstrap threshold). The live window restarts from zero.
        """
        if setup_id not in self.suspended_setups or self.resume_trades <= 0:
            return False
        hist = self.probation_r.setdefault(setup_id, [])
        hist.append(float(r_multiple))
        if len(hist) < self.resume_trades:
            return False
        mean_r = float(np.mean(hist[-self.resume_trades:]))
        thr = self._threshold(setup_id)
        if mean_r <= 0.0 or (thr is not None and mean_r < thr):
            return False
        del self.suspended_setups[setup_id]
        self.probation_r.pop(setup_id, None)
        self.trades_r_by_setup[setup_id] = []
        self._cusum[setup_id] = 0.0
        return True


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
        consecutive_losses_limit: int = 0,
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
        self.consecutive_losses_limit = consecutive_losses_limit
        self.loss_streak = 0
        self.halt_reason: str | None = None
        self.just_halted = False  # True only on the session the quarantine started (triggers re-optimization)

    def record_trade(self, r_multiple: float) -> None:
        """Closed real trade: tracks the losing streak (quarantine trigger 2, PRD v2.1 ajustado)."""
        self.loss_streak = self.loss_streak + 1 if r_multiple < 0 else 0
        if (not self.is_halted and self.consecutive_losses_limit > 0
                and self.loss_streak >= self.consecutive_losses_limit):
            self._halt(f"{self.loss_streak} perdas consecutivas")

    def _halt(self, reason: str) -> None:
        self.is_halted = True
        self.sessions_halted = 0
        self.halt_reason = reason
        self.just_halted = True

    def resume(self, equity: float, reason: str = "") -> None:
        """Leave quarantine (shadow curve recovered or trader approved a new configuration)."""
        self.is_halted = False
        self.sessions_halted = 0
        self.peak_equity = equity
        self.drawdown = 0.0
        self.loss_streak = 0
        self.halt_reason = None

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
                    self.resume(equity, "curva sombra acima da média")
        elif self.drawdown >= self.dd_level2:
            self._halt(f"drawdown {self.drawdown:.1%} >= {self.dd_level2:.0%}")

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
