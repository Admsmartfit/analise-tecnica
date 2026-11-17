"""Weekly G1/G2 performance report generator.

Generates a Markdown report comparing live paper-trading metrics against
backtest bootstrap bounds. Sent via Telegram every Friday EOD.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

logger = logging.getLogger("ts.app.weekly_report")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@contextmanager
def _db_conn(db_path: str) -> Iterator[sqlite3.Connection]:
    """Read-only connection that is always closed (sqlite3's own context manager only commits)."""
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _load_equity_df(db_path: str, since: date, curve: str = "PAPER") -> pd.DataFrame:
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT session_date, equity, cash, heat_pct, drawdown_pct FROM equity_daily "
            "WHERE curve=? AND session_date >= ? ORDER BY session_date",
            conn,
            params=(curve, since.isoformat()),
        )
    return df


def _load_trades_df(db_path: str, since: date) -> pd.DataFrame:
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT ticker, setup, trade_class, opened_on, closed_on, entry_price, "
            "pnl_brl, r_multiple, exit_reason, bars_held, mae_r, mfe_r "
            "FROM positions WHERE status='CLOSED' AND is_shadow=0 AND closed_on >= ? ORDER BY closed_on",
            conn,
            params=(since.isoformat(),),
        )
    return df


def _load_runs_df(db_path: str, since: date) -> pd.DataFrame:
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT routine, session_date, status FROM runs WHERE session_date >= ? AND routine IN ('preopen','eod','watchdog')",
            conn,
            params=(since.isoformat(),),
        )
    return df


def _load_data_quality_df(db_path: str, since: date) -> pd.DataFrame:
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT session_date, ticker, severity, count(*) as cnt FROM data_quality "
            "WHERE session_date >= ? GROUP BY session_date, severity",
            conn,
            params=(since.isoformat(),),
        )
    return df


def _has_duplicate_orders(db_path: str, since: date) -> bool:
    with _db_conn(db_path) as conn:
        dup_signal = conn.execute(
            "SELECT 1 FROM order_intents WHERE signal_id IS NOT NULL AND side='BUY' AND created_at >= ? "
            "GROUP BY signal_id HAVING COUNT(*) > 1 LIMIT 1",
            (since.isoformat(),),
        ).fetchone()
        dup_fill = conn.execute(
            "SELECT 1 FROM order_events e JOIN order_intents i ON i.client_order_id = e.client_order_id "
            "WHERE e.state='FILLED' AND i.side='BUY' AND e.ts_utc >= ? "
            "GROUP BY i.ticker, substr(e.ts_utc, 1, 10) HAVING COUNT(DISTINCT e.client_order_id) > 1 LIMIT 1",
            (since.isoformat(),),
        ).fetchone()
        unknown = conn.execute(
            "SELECT 1 FROM order_events WHERE state='UNKNOWN' AND ts_utc >= ? LIMIT 1", (since.isoformat(),)
        ).fetchone()
    return bool(dup_signal or dup_fill or unknown)


# ---------------------------------------------------------------------------
# Metric calculations
# ---------------------------------------------------------------------------


class WeeklyMetrics(NamedTuple):
    week_start: date
    week_end: date
    n_sessions: int
    n_trades: int
    win_rate: float
    expectancy_r: float
    expectancy_ci_lower: float
    expectancy_ci_upper: float
    max_drawdown_pct: float
    sharpe_weekly: float
    total_return_pct: float
    n_sessions_ok: int
    n_sessions_total: int
    has_duplicate_orders: bool
    n_data_quality_blocks: int


def _bootstrap_ci(values: list[float], n_boot: int = 1000, confidence: float = 0.95) -> tuple[float, float]:
    if len(values) < 3:
        m = float(np.mean(values)) if values else 0.0
        return m, m
    arr = np.array(values)
    np.random.seed(42)
    boot = [float(np.mean(np.random.choice(arr, size=len(arr), replace=True))) for _ in range(n_boot)]
    alpha = (1.0 - confidence) / 2.0
    return float(np.percentile(boot, alpha * 100)), float(np.percentile(boot, (1.0 - alpha) * 100))


def compute_weekly_metrics(
    db_path: str,
    week_end: date | None = None,
) -> WeeklyMetrics:
    """Compute G1/G2 metrics for the week ending on `week_end` (default: last Friday)."""
    if week_end is None:
        today = date.today()
        days_since_friday = (today.weekday() - 4) % 7
        week_end = today - timedelta(days=days_since_friday)

    week_start = week_end - timedelta(days=4)  # Monday

    equity_df = _load_equity_df(db_path, week_start)
    trades_df = _load_trades_df(db_path, week_start)
    runs_df = _load_runs_df(db_path, week_start)

    # G1 metrics — operational
    n_sessions = len(equity_df)
    if not runs_df.empty:
        eod_runs = runs_df[runs_df["routine"] == "eod"]
        n_sessions_ok = int((eod_runs["status"] == "OK").sum())
        n_sessions_total = len(eod_runs)
    else:
        n_sessions_ok = 0
        n_sessions_total = 0

    # G1: zero duplicated orders — more than one entry intent per signal, or more than one filled BUY of the
    # same ticker on the same day, or any order left in UNKNOWN state.
    has_duplicate_orders = _has_duplicate_orders(db_path, week_start)

    # Data quality
    dq_df = _load_data_quality_df(db_path, week_start)
    n_blocks = int(dq_df[dq_df["severity"] == "BLOCK"]["cnt"].sum()) if not dq_df.empty else 0

    # G2 metrics — statistical
    n_trades = len(trades_df)
    if n_trades > 0:
        wins = trades_df[trades_df["pnl_brl"] > 0]
        win_rate = len(wins) / n_trades
        r_values = trades_df["r_multiple"].dropna().tolist()
        expectancy_r = float(np.mean(r_values)) if r_values else 0.0
        ci_lower, ci_upper = _bootstrap_ci(r_values)
    else:
        win_rate = 0.0
        expectancy_r = 0.0
        ci_lower = 0.0
        ci_upper = 0.0

    if not equity_df.empty:
        max_dd = float(equity_df["drawdown_pct"].max())
        eq = equity_df["equity"]
        returns = eq.pct_change().dropna()
        total_ret = float((eq.iloc[-1] - eq.iloc[0]) / eq.iloc[0]) if len(eq) > 1 else 0.0
        sharpe = float(returns.mean() / returns.std() * math.sqrt(52)) if len(returns) > 1 and returns.std() > 0 else 0.0
    else:
        max_dd = 0.0
        total_ret = 0.0
        sharpe = 0.0

    return WeeklyMetrics(
        week_start=week_start,
        week_end=week_end,
        n_sessions=n_sessions,
        n_trades=n_trades,
        win_rate=win_rate,
        expectancy_r=expectancy_r,
        expectancy_ci_lower=ci_lower,
        expectancy_ci_upper=ci_upper,
        max_drawdown_pct=max_dd,
        sharpe_weekly=sharpe,
        total_return_pct=total_ret,
        n_sessions_ok=n_sessions_ok,
        n_sessions_total=n_sessions_total,
        has_duplicate_orders=has_duplicate_orders,
        n_data_quality_blocks=n_blocks,
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_markdown_report(m: WeeklyMetrics) -> str:
    """Render a Markdown G1/G2 weekly report."""
    g1_ops = "✅" if m.n_sessions_ok == m.n_sessions_total and m.n_sessions_total > 0 else "⚠️"
    g1_dup = "✅" if not m.has_duplicate_orders else "❌"
    g1_dq = "✅" if m.n_data_quality_blocks == 0 else f"⚠️ {m.n_data_quality_blocks} BLOCKs"

    exp_color = "🟢" if m.expectancy_r > 0 and m.expectancy_ci_lower > 0 else ("🟡" if m.expectancy_r > 0 else "🔴")
    dd_color = "🔴" if m.max_drawdown_pct > 0.08 else "🟢"

    report = f"""# 📊 Weekly G1/G2 Report — {m.week_start} to {m.week_end}

## G1 — Operational

| Check | Result |
|-------|--------|
| Sessions processed without intervention | {g1_ops} {m.n_sessions_ok}/{m.n_sessions_total} |
| Duplicate orders | {g1_dup} |
| Data quality blocks | {g1_dq} |

## G2 — Statistical Consistency

| Metric | Value |
|--------|-------|
| Trades this week | {m.n_trades} |
| Win rate | {m.win_rate:.1%} |
| Expectancy (R) | {exp_color} {m.expectancy_r:+.3f}R |
| 95% CI Expectancy | [{m.expectancy_ci_lower:+.3f}R, {m.expectancy_ci_upper:+.3f}R] |
| Max Drawdown | {dd_color} {m.max_drawdown_pct:.2%} |
| Annualized Sharpe (weekly) | {m.sharpe_weekly:.2f} |
| Week Return | {m.total_return_pct:+.2%} |

## Kill Switch

Drawdown > 10%: {"🚨 ACTIVATE" if m.max_drawdown_pct > 0.10 else "✅ Clear"}

---
_Generated automatically by Trading System B3 — paper mode_
"""
    return report


def generate_weekly_report(db_path: str, week_end: date | None = None, send_telegram: bool = True) -> str:
    """Generate and optionally send the weekly G1/G2 report.

    Returns the Markdown report text.
    """
    metrics = compute_weekly_metrics(db_path, week_end)
    report_text = render_markdown_report(metrics)

    if send_telegram:
        try:
            from ts.app.notify import send_telegram as _send
            # Send a compact summary to Telegram (HTML)
            summary = (
                f"📊 <b>Weekly Report {metrics.week_start} → {metrics.week_end}</b>\n"
                f"Trades: {metrics.n_trades} | WR: {metrics.win_rate:.0%} | E(R): {metrics.expectancy_r:+.3f}\n"
                f"MaxDD: {metrics.max_drawdown_pct:.2%} | Sessions OK: {metrics.n_sessions_ok}/{metrics.n_sessions_total}"
            )
            _send(summary)
        except Exception as exc:
            logger.warning("Could not send Telegram: %s", exc)

    return report_text
