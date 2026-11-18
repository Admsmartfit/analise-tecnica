"""Event-driven backtester (PRD S5) that runs the production session engine day by day.

Nothing here decides trades: fills, exits, sizing, portfolio limits, protections and signals all come from
:class:`ts.execution.session_engine.SessionEngine`, the same object the paper routine uses.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ts.core.calendar import B3Calendar, get_calendar
from ts.core.config import (
    CostsConfig,
    StrategyConfig,
    load_costs_config,
    load_strategy_config,
    strategy_config_hash,
)
from ts.execution.session_engine import (
    BookState,
    CompletedTrade,
    FillEvent,
    SessionEngine,
    SignalRecord,
)
from ts.indicators.ta import sma
from ts.strategy.regime import realized_vol_percentile_series
from ts.strategy.signal_engine import TickerData, prepare_ticker

logger = logging.getLogger("ts.backtest.engine")

__all__ = ["BacktestEngine", "BacktestResult", "CompletedTrade", "compute_monthly_tax"]


@dataclass
class BacktestResult:
    trades: list[CompletedTrade]
    equity_curve: pd.DataFrame
    config_hash: str
    initial_capital: float
    final_equity: float
    total_net_pnl: float
    total_fees: float
    signals: list[SignalRecord] = field(default_factory=list)
    shadow_trades: list[CompletedTrade] = field(default_factory=list)
    fills: list[FillEvent] = field(default_factory=list)
    tax: pd.DataFrame | None = None
    total_tax: float = 0.0
    expired_orders: int = 0

    @property
    def daily_returns(self) -> pd.Series:
        if self.equity_curve.empty:
            return pd.Series(dtype=float)
        eq = self.equity_curve.set_index("session_date")["equity"]
        return eq.pct_change().fillna(0.0)


def compute_monthly_tax(fills: list[FillEvent], trades: list[CompletedTrade], rate: float = 0.15,
                        exemption: float = 20000.0) -> pd.DataFrame:
    """IR swing trade (PRD 1.2.6): 15% on monthly net gains, exempt if stock sales <= R$20k in the month,
    losses carried forward. Gains are attributed to the month the position closed."""
    sales: dict[str, float] = {}
    for f in fills:
        if f.side == "SELL" and not f.is_shadow:
            m = f.session_date.strftime("%Y-%m")
            sales[m] = sales.get(m, 0.0) + f.qty * f.price
    gains: dict[str, float] = {}
    for t in trades:
        if t.is_shadow:
            continue
        m = t.exit_date.strftime("%Y-%m")
        gains[m] = gains.get(m, 0.0) + t.net_pnl_brl
    rows = []
    carry = 0.0
    for m in sorted(set(sales) | set(gains)):
        g = gains.get(m, 0.0)
        s = sales.get(m, 0.0)
        exempt = s <= exemption
        taxable = 0.0
        if g < 0:
            carry += -g
        elif not exempt:
            used = min(carry, g)
            carry -= used
            taxable = g - used
        rows.append({"month": m, "sales": s, "net_gain": g, "exempt": exempt, "taxable": taxable,
                     "tax": taxable * rate, "loss_carry": carry})
    return pd.DataFrame(rows)


class BacktestEngine:
    """Iterates sessions over historical bars using the production :class:`SessionEngine`."""

    def __init__(
        self,
        strategy_config: StrategyConfig | None = None,
        costs_config: CostsConfig | None = None,
        initial_capital: float = 100000.0,
        calendar: B3Calendar | None = None,
        reference_r: dict[str, list[float]] | None = None,
        fractional: bool = False,
    ) -> None:
        if strategy_config is None:
            strategy_config, _ = load_strategy_config()
        self.strategy_config = strategy_config
        self.config_hash = strategy_config_hash(strategy_config)
        self.costs_config = costs_config or load_costs_config()
        self.initial_capital = initial_capital
        self.calendar = calendar or get_calendar()
        self.reference_r = reference_r
        self.session_engine = SessionEngine(
            strategy_config, self.costs_config, self.config_hash, self.calendar, fractional=fractional, order_prefix="BT"
        )

    def prepare(self, bars: dict[str, pd.DataFrame]) -> dict[str, TickerData]:
        return {t: prepare_ticker(t, df, self.strategy_config) for t, df in bars.items() if df is not None and not df.empty}

    def run(
        self,
        bars: dict[str, pd.DataFrame],
        index_bars: pd.DataFrame,
        start_date: date | None = None,
        end_date: date | None = None,
        universe_fn: Callable[[date], list[str]] | None = None,
        prepared: dict[str, TickerData] | None = None,
    ) -> BacktestResult:
        prepared = prepared if prepared is not None else self.prepare(bars)
        cfg = self.strategy_config

        idx = index_bars.sort_values("session_date").reset_index(drop=True) if not index_bars.empty else index_bars
        market_ok: dict[date, bool] = {}
        market_bear: dict[date, bool] = {}
        regime_pct: dict[date, float] = {}
        index_returns = pd.Series(dtype=float)
        if not idx.empty:
            c = idx["close"].astype(float)
            sl, ss = sma(c, cfg.market_filter.ibov_sma_long), sma(c, cfg.market_filter.ibov_sma_short)
            ok = (c > sl) & (ss > sl) & sl.notna()
            if not cfg.market_filter.require_bull_regime:
                ok = pd.Series(True, index=idx.index)
            market_ok = dict(zip(idx["session_date"], ok.astype(bool), strict=True))
            bear = (c < sl) & (ss < sl) & sl.notna()
            market_bear = dict(zip(idx["session_date"], bear.astype(bool), strict=True))
            regime_pct = dict(zip(idx["session_date"], realized_vol_percentile_series(idx, cfg.regime), strict=True))
            index_returns = pd.Series(c.pct_change().to_numpy(), index=list(idx["session_date"]))
        elif not cfg.market_filter.require_bull_regime:
            market_ok = {}

        all_dates: set[date] = set()
        for td in prepared.values():
            all_dates.update(td.dates)
        sessions = sorted(d for d in all_dates if (start_date is None or d >= start_date) and (end_date is None or d <= end_date))

        state = BookState.new(self.initial_capital, cfg, self.calendar, self.reference_r)
        trades: list[CompletedTrade] = []
        shadow: list[CompletedTrade] = []
        fills: list[FillEvent] = []
        sig_log: list[SignalRecord] = []
        rows: list[dict[str, Any]] = []
        expired = 0

        for i, d in enumerate(sessions):
            m_ok = market_ok.get(d, not cfg.market_filter.require_bull_regime)
            rp = regime_pct.get(d)
            universe = set(universe_fn(d)) if universe_fn is not None else None
            res = self.session_engine.run_session(
                state, d, prepared, market_ok=m_ok,
                regime_percentile=None if rp is None or np.isnan(rp) else float(rp),
                index_returns=index_returns if len(index_returns) else None,
                universe=universe, plan=i < len(sessions) - 1, market_bear=market_bear.get(d, False),
            )
            for t in res.closed_trades:
                (shadow if t.is_shadow else trades).append(t)
            fills.extend(res.fills)
            sig_log.extend(res.signals)
            expired += len(res.expired)
            e = res.equity
            assert e is not None
            rows.append({
                "session_date": d, "equity": e.equity, "cash": e.cash, "heat_pct": e.heat_pct,
                "open_positions": e.open_positions, "shadow_equity": e.shadow_equity, "cb_level": e.cb_level,
                "regime_percentile": e.regime_percentile,
            })

        eq_df = pd.DataFrame(rows)
        if not eq_df.empty:
            eq_df["peak"] = eq_df["equity"].cummax()
            eq_df["drawdown_pct"] = (eq_df["peak"] - eq_df["equity"]) / eq_df["peak"]
        else:
            eq_df = pd.DataFrame(columns=["session_date", "equity", "cash", "heat_pct", "drawdown_pct"])

        tax_cfg = self.costs_config.costs.tax
        tax_df = compute_monthly_tax(fills, trades, tax_cfg.swing_trade_rate, tax_cfg.monthly_sales_exemption_brl)
        final_equity = float(eq_df["equity"].iloc[-1]) if not eq_df.empty else self.initial_capital
        return BacktestResult(
            trades=trades,
            equity_curve=eq_df,
            config_hash=self.config_hash,
            initial_capital=self.initial_capital,
            final_equity=final_equity,
            total_net_pnl=round(sum(t.net_pnl_brl for t in trades), 2),
            total_fees=round(sum(t.fees_brl for t in trades), 2),
            signals=sig_log,
            shadow_trades=shadow,
            fills=fills,
            tax=tax_df,
            total_tax=round(float(tax_df["tax"].sum()) if not tax_df.empty else 0.0, 2),
            expired_orders=expired,
        )
