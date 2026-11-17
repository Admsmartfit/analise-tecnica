"""Free D1 market data from Yahoo Finance (testing / paper without a broker login).

Same interface as :class:`ts.data.mt5_gateway.MT5Gateway` (``connect``, ``daily_bars``, ``health``,
``last_tick_age_seconds``, ``shutdown``), so ``sync`` and the routines do not know which source is used.

Caveats (documented on purpose — read before trusting a backtest on this data):
* Yahoo OHLC are **split-adjusted** but not dividend-adjusted. Splits therefore must NOT be added to
  ``corporate_actions.csv`` when this provider is used; dividends can be imported with
  :meth:`YahooGateway.dividend_actions` (``python -m ts corp-actions``).
* Quotes during the session are delayed (~15 min). Irrelevant here: decisions use the closed D1 bar.
* Unofficial endpoint: it can rate-limit (HTTP 429) or change. Reads retry with backoff; a failure marks
  the ticker as unavailable for the day (same fail-safe policy as MT5).
* ``tick_volume`` and ``spread`` do not exist on Yahoo and are stored as 0.
"""
from __future__ import annotations

import logging
import random
import time as time_mod
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd

from ts.core.clock import Clock, SystemClock
from ts.core.config import Settings
from ts.core.errors import ConnectivityError, DataUnavailableError
from ts.core.models import HealthReport

logger = logging.getLogger("ts.data.yahoo_gateway")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ts-b3-swing/0.1", "Accept": "application/json"}
COLUMNS = ["session_date", "ts_utc", "open", "high", "low", "close", "real_volume", "tick_volume", "spread",
           "adj_factor", "source", "ingested_at"]


class YahooGateway:
    def __init__(self, settings: Settings | None = None, clock: Clock | None = None,
                 client: httpx.Client | None = None) -> None:
        self.settings = settings or Settings()
        self.clock = clock or SystemClock()
        self._client = client
        self._owns_client = client is None
        self.read_backoff_base_s = 1.0

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        if self._client is None:
            self._client = httpx.Client(headers=HEADERS, timeout=self.settings.data.yahoo_timeout_s,
                                        http2=False, follow_redirects=True)

    def shutdown(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> YahooGateway:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    # ------------------------------------------------------------------ symbols
    def yahoo_symbol(self, ticker: str) -> str:
        t = ticker.upper()
        if t == self.settings.mt5.ibov_symbol.upper():
            return self.settings.data.yahoo_index_symbol
        if t.startswith("^") or "." in t:
            return t
        return t + self.settings.data.yahoo_suffix

    def ensure_symbol(self, ticker: str) -> None:  # interface parity with MT5Gateway
        return None

    # ------------------------------------------------------------------ http
    def _chart(self, ticker: str, start: date, end: date, events: str = "div,split") -> dict[str, Any]:
        self.connect()
        assert self._client is not None
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
        p2 = int((datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)).timestamp())
        params = {"period1": p1, "period2": p2, "interval": "1d", "events": events, "includeAdjustedClose": "true"}
        url = CHART_URL.format(symbol=self.yahoo_symbol(ticker))
        last_err = ""
        for attempt in range(3):
            try:
                r = self._client.get(url, params=params)
                if r.status_code == 200:
                    body = r.json()
                    res = (body.get("chart") or {}).get("result")
                    if res:
                        return dict(res[0])
                    last_err = str((body.get("chart") or {}).get("error"))
                    if "No data found" in last_err or "delisted" in last_err.lower():
                        break
                elif r.status_code == 404:
                    last_err = "symbol not found (404)"
                    break
                else:
                    last_err = f"HTTP {r.status_code}"
            except (httpx.HTTPError, ValueError) as e:
                last_err = f"{type(e).__name__}: {e}"
            logger.warning("Yahoo %s attempt %d/3 failed: %s", ticker, attempt + 1, last_err)
            if attempt < 2:
                delay = min(self.read_backoff_base_s * (2**attempt), 8.0)
                time_mod.sleep(delay + random.uniform(0, delay * 0.25))
        raise DataUnavailableError(f"Yahoo returned no data for {ticker}: {last_err}")

    # ------------------------------------------------------------------ bars
    def daily_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        res = self._chart(ticker, start, end)
        return self._to_frame(ticker, res, end)

    def _to_frame(self, ticker: str, res: dict[str, Any], end: date) -> pd.DataFrame:
        ts = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        if not ts or not quote:
            raise DataUnavailableError(f"Yahoo returned an empty series for {ticker}")
        tz = (res.get("meta") or {}).get("exchangeTimezoneName") or "America/Sao_Paulo"
        df = pd.DataFrame({
            "t": ts,
            "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
            "close": quote.get("close"), "volume": quote.get("volume"),
        }).dropna(subset=["open", "high", "low", "close"])
        if df.empty:
            raise DataUnavailableError(f"Yahoo returned only null bars for {ticker}")

        local = pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_convert(tz)
        df["session_date"] = local.dt.date
        df["ts_utc"] = local.dt.tz_convert("UTC")
        for c in ("open", "high", "low", "close"):
            df[c] = df[c].astype(float).round(2)
        df["real_volume"] = df["volume"].fillna(0).astype("int64")
        # Yahoo float noise can leave H/L a hair inside O/C after rounding: keep the bar consistent.
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        # Holiday artifacts: zero-volume flat bars repeating the previous close (stocks only).
        is_index = self.yahoo_symbol(ticker).startswith("^")
        if not is_index:
            flat = (df["real_volume"] == 0) & (df["high"] == df["low"])
            if flat.any():
                logger.info("Yahoo %s: dropping %d zero-volume flat bars", ticker, int(flat.sum()))
                df = df[~flat]
        df = df[df["session_date"] <= end]
        df = df.drop_duplicates(subset=["session_date"], keep="last").sort_values("session_date")
        df["tick_volume"] = 0
        df["spread"] = 0
        df["adj_factor"] = 1.0
        df["source"] = "yahoo"
        df["ingested_at"] = self.clock.now_utc()
        return df[COLUMNS].reset_index(drop=True)

    # ------------------------------------------------------------------ corporate actions
    def dividend_actions(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        """Cash dividends/JCP as rows for corporate_actions.csv (factor = 1 - div / previous close)."""
        res = self._chart(ticker, start, end, events="div")
        bars = self._to_frame(ticker, res, end)
        divs = ((res.get("events") or {}).get("dividends") or {}).values()
        tz = (res.get("meta") or {}).get("exchangeTimezoneName") or "America/Sao_Paulo"
        rows = []
        for d in sorted(divs, key=lambda x: x["date"]):
            ex = pd.Timestamp(d["date"], unit="s", tz="UTC").tz_convert(tz).date()
            prev = bars[bars["session_date"] < ex]
            if prev.empty:
                continue
            prev_close = float(prev["close"].iloc[-1])
            amount = float(d["amount"])
            if prev_close <= 0 or amount <= 0 or amount >= prev_close:
                continue
            rows.append({"ticker": ticker.upper(), "ex_date": ex.isoformat(), "action_type": "DIVIDEND",
                         "value": round(amount, 6), "factor": round(1.0 - amount / prev_close, 8)})
        return rows

    # ------------------------------------------------------------------ health
    def last_tick_age_seconds(self, ticker: str) -> float | None:
        try:
            today = self.clock.now_utc().date()
            meta = self._chart(ticker, today - timedelta(days=7), today, events="").get("meta") or {}
        except DataUnavailableError:
            return None
        t = meta.get("regularMarketTime")
        if not t:
            return None
        return max(0.0, self.clock.now_utc().timestamp() - float(t))

    def health(self) -> HealthReport:
        t0 = time_mod.perf_counter()
        details: dict[str, Any] = {"provider": "yahoo"}
        try:
            age = self.last_tick_age_seconds(self.settings.mt5.ibov_symbol)
            ok = age is not None
        except ConnectivityError as e:
            age, ok = None, False
            details["error"] = str(e)
        ping = (time_mod.perf_counter() - t0) * 1000.0
        return HealthReport(ok=ok, terminal_version="yahoo-chart-v8", connected=ok, trade_allowed=False,
                            tick_age_seconds=age, ping_ms=round(ping, 1), details=details)


def make_gateway(settings: Settings) -> Any:
    """Factory used by sync and routines: the configured market-data provider."""
    if settings.data.provider == "yahoo":
        return YahooGateway(settings=settings)
    from ts.data.mt5_gateway import MT5Gateway

    return MT5Gateway(settings=settings)
