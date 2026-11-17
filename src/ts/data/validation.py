"""Data quality and sanity checks for OHLCV bars (PRD Sprint 1, passo 7)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd

from ts.core.calendar import B3Calendar, get_calendar


@dataclass(frozen=True, slots=True)
class QualityIssue:
    session_date: date
    ticker: str
    check_name: str
    severity: Literal["INFO", "WARN", "BLOCK"]
    detail: str


class DataValidator:
    """Validates bar data against B3 market rules, consistency and calendar."""

    def __init__(self, calendar: B3Calendar | None = None) -> None:
        self.calendar = calendar or get_calendar()

    def validate_bars(
        self,
        ticker: str,
        df: pd.DataFrame,
        asof: date | None = None,
        check_calendar: bool = True,
        expected_start: date | None = None,
    ) -> list[QualityIssue]:
        """Run all data quality checks on a DataFrame of OHLCV bars.

        ``asof`` is the session the data must reach. When ``asof`` is a trading session, a missing bar
        on ``asof`` is BLOCK ("Pregão do calendário sem barra: BLOCK se for a última"), because the EOD
        decision would otherwise be taken on a stale bar.
        """
        issues: list[QualityIssue] = []

        if df is None or len(df) == 0:
            issues.append(
                QualityIssue(asof or date.today(), ticker, "empty_dataframe", "BLOCK", "No data provided in DataFrame")
            )
            return issues

        def add(d: date, name: str, sev: Literal["INFO", "WARN", "BLOCK"], detail: str) -> None:
            issues.append(QualityIssue(session_date=d, ticker=ticker, check_name=name, severity=sev, detail=detail))

        # 1. Duplicate session_date
        dup = df[df.duplicated(subset=["session_date"], keep=False)]
        for d in dup["session_date"].unique():
            add(d, "duplicate_date", "BLOCK", f"Duplicate session_date {d} found")

        # 2. Price > 0 and volume >= 0
        bad_px = df[(df["open"] <= 0) | (df["high"] <= 0) | (df["low"] <= 0) | (df["close"] <= 0)]
        for _, r in bad_px.iterrows():
            add(r["session_date"], "non_positive_price", "BLOCK",
                f"Non-positive price at {r['session_date']}: O={r['open']}, H={r['high']}, L={r['low']}, C={r['close']}")
        for _, r in df[df["real_volume"] < 0].iterrows():
            add(r["session_date"], "negative_volume", "BLOCK", f"Negative volume at {r['session_date']}: {r['real_volume']}")

        # 3. High >= max(Open, Close) and Low <= min(Open, Close)
        bad_hl = df[(df["high"] < df["open"]) | (df["high"] < df["close"]) | (df["low"] > df["open"]) | (df["low"] > df["close"])]
        for _, r in bad_hl.iterrows():
            add(r["session_date"], "high_low_inconsistency", "BLOCK",
                f"Bar high/low inconsistent at {r['session_date']}: O={r['open']}, H={r['high']}, L={r['low']}, C={r['close']}")

        sorted_dates = sorted(df["session_date"].unique())

        # 4. Calendar checks
        if check_calendar:
            for d in sorted_dates:
                if not self.calendar.is_session(d):
                    add(d, "non_session_bar", "BLOCK", f"Bar exists on non-session date {d}")

            start_date = min(sorted_dates[0], expected_start) if expected_start else sorted_dates[0]
            end_date = sorted_dates[-1]
            if asof is not None and self.calendar.is_session(asof) and asof > end_date:
                end_date = asof
            expected_sessions = self.calendar.sessions_between(start_date, end_date)
            present = set(sorted_dates)
            for exp_d in expected_sessions:
                if exp_d not in present:
                    is_last = exp_d == expected_sessions[-1]
                    add(exp_d, "missing_session", "BLOCK" if is_last else "WARN",
                        f"Missing trading session at {exp_d}" + (" (latest expected bar)" if is_last else ""))

        # 5. Daily close change > 25% (likely corporate action -> manual review)
        if len(df) > 1:
            s = df.sort_values(by="session_date").reset_index(drop=True)
            jump = s["close"].pct_change().abs()
            for _, r in s[jump > 0.25].iterrows():
                add(r["session_date"], "excessive_return_jump", "WARN",
                    f"Daily price change > 25% at {r['session_date']} (possible dividend/split)")

        # 6. Staleness: last bar older than the previous session (PRD: BLOCK / STALE)
        if asof is not None:
            last_date = sorted_dates[-1]
            ref = asof if self.calendar.is_session(asof) else self.calendar.previous_session(asof)
            prev = self.calendar.previous_session(ref)
            if last_date < prev:
                add(last_date, "stale_data", "BLOCK",
                    f"Latest bar ({last_date}) is older than previous expected session ({prev})")

        return issues

    def has_blocking_issues(self, issues: list[QualityIssue]) -> bool:
        return any(issue.severity == "BLOCK" for issue in issues)
