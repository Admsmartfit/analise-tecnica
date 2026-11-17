"""MetaTrader 5 gateway wrapping MT5 API with fault tolerance and schema conversion."""
from __future__ import annotations

import logging
import random
import time as time_mod
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

import ts.data.fake_mt5 as fake_mt5_consts
from ts.core.clock import Clock, SystemClock
from ts.core.config import Settings
from ts.core.errors import (
    ConnectivityError,
    DataUnavailableError,
    NotLoggedInError,
    TerminalNotRunningError,
)
from ts.core.models import HealthReport

logger = logging.getLogger("ts.data.mt5_gateway")


class MT5Gateway:
    """Robust adapter for interacting with MetaTrader 5 terminal."""

    def __init__(
        self,
        mt5_module: Any = None,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.mt5 = mt5_module
        self.settings = settings or Settings()
        self.clock = clock or SystemClock()
        self._connected = False
        # Base delays (seconds). Tests may set them to 0 to run fast.
        self.read_backoff_base_s = 1.0
        self.connect_backoff_base_s = 2.0

    def _ensure_module(self) -> Any:
        if self.mt5 is None:
            try:
                import MetaTrader5

                self.mt5 = MetaTrader5
            except ImportError as e:
                raise ConnectivityError(
                    "MetaTrader5 package is not available. Please install it on Windows or pass FakeMT5."
                ) from e
        return self.mt5

    def connect(self) -> None:
        """Connect and authenticate to MT5 terminal with retry."""
        mt5 = self._ensure_module()
        mt5_cfg = self.settings.mt5

        # Get password from keyring if available
        password = ""
        if mt5_cfg.login and mt5_cfg.keyring_service:
            try:
                import keyring

                pw = keyring.get_password(mt5_cfg.keyring_service, str(mt5_cfg.login))
                if pw:
                    password = pw
            except Exception as e:
                logger.warning(f"Could not retrieve password from keyring: {e}")

        # Retry initialize
        attempts = 0
        max_attempts = 5
        init_ok = False

        while attempts < max_attempts:
            attempts += 1
            try:
                init_ok = mt5.initialize(
                    path=mt5_cfg.terminal_path,
                    login=mt5_cfg.login if mt5_cfg.login > 0 else None,
                    server=mt5_cfg.server if mt5_cfg.login > 0 else None,
                    password=password if password else None,
                    timeout=mt5_cfg.timeout_ms,
                )
                if init_ok:
                    break
            except Exception as e:  # noqa: BLE001 - the MT5 API may raise on IPC failures
                logger.warning(f"initialize() attempt {attempts} raised: {e}")
            if attempts < max_attempts:
                # PRD 2.6: exponential 2 s .. 30 s
                time_mod.sleep(min(self.connect_backoff_base_s * (2 ** (attempts - 1)), 30.0))

        if not init_ok:
            code, desc = mt5.last_error() if hasattr(mt5, "last_error") else (-1, "Init failed")
            raise TerminalNotRunningError(
                f"Failed to initialize MT5 terminal after {max_attempts} attempts: {code} - {desc}"
            )

        term_info = mt5.terminal_info()
        if term_info is None or not getattr(term_info, "connected", False):
            raise ConnectivityError("MT5 terminal initialized but reports disconnected.")

        acc_info = mt5.account_info()
        if acc_info is None:
            raise NotLoggedInError("MT5 terminal connected but account_info is None (not logged in).")

        self._connected = True
        logger.info(
            f"Connected to MT5 on {acc_info.server} account {acc_info.login} (mode={acc_info.trade_mode})"
        )

    def shutdown(self) -> None:
        """Disconnect safely from MT5 terminal."""
        if self.mt5 is not None and self._connected:
            try:
                self.mt5.shutdown()
            except Exception as e:
                logger.warning(f"Error during MT5 shutdown: {e}")
            finally:
                self._connected = False

    def __enter__(self) -> MT5Gateway:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.shutdown()

    def ensure_symbol(self, ticker: str) -> None:
        """Ensure symbol is selected in Market Watch."""
        mt5 = self._ensure_module()
        sym_info = mt5.symbol_info(ticker)
        if sym_info is None:
            # Try to select it
            ok = mt5.symbol_select(ticker, True)
            if not ok:
                raise DataUnavailableError(f"Symbol '{ticker}' not found in MT5 broker symbol catalog.")
            return

        if not getattr(sym_info, "visible", False):
            ok = mt5.symbol_select(ticker, True)
            if not ok:
                raise DataUnavailableError(f"Could not select '{ticker}' in Market Watch.")

    def daily_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Fetch daily OHLCV bars for ticker from start to end dates."""
        mt5 = self._ensure_module()
        self.ensure_symbol(ticker)

        # Build start and end datetime
        start_dt = datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=UTC)
        end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)

        timeframe = getattr(mt5, "TIMEFRAME_D1", fake_mt5_consts.TIMEFRAME_D1)

        # Read retry (PRD 2.6): 3 attempts, exponential 1 s..8 s with jitter.
        rates = None
        for attempt in range(3):
            rates = mt5.copy_rates_range(ticker, timeframe, start_dt, end_dt)
            if rates is not None:
                break
            err_code, err_desc = mt5.last_error() if hasattr(mt5, "last_error") else (-10004, "IPC Error")
            logger.warning(f"Attempt {attempt + 1}/3 copy_rates_range({ticker}) failed: {err_code} - {err_desc}")
            if attempt < 2:
                delay = min(self.read_backoff_base_s * (2**attempt), 8.0)
                time_mod.sleep(delay + random.uniform(0, delay * 0.25))

        if rates is None or len(rates) == 0:
            raise DataUnavailableError(f"No rates returned for {ticker} between {start} and {end}")

        df = pd.DataFrame(rates)
        # MT5 returns bar times as epoch seconds expressed in the *server* clock. The calendar date of that
        # server-local timestamp is the session date; ts_utc is the real UTC instant (PRD 1.3.1, fuso).
        server_local = pd.to_datetime(df["time"], unit="s")
        df["session_date"] = server_local.dt.date
        df["ts_utc"] = server_local.dt.tz_localize(
            self.settings.mt5.server_tz, ambiguous="NaT", nonexistent="shift_forward"
        ).dt.tz_convert("UTC")
        df["open"] = df["open"].astype("float64")
        df["high"] = df["high"].astype("float64")
        df["low"] = df["low"].astype("float64")
        df["close"] = df["close"].astype("float64")
        df["real_volume"] = df["real_volume"].astype("int64")
        df["tick_volume"] = df["tick_volume"].astype("int64")
        df["spread"] = df["spread"].astype("int32")
        df["adj_factor"] = 1.0
        server_name = self.settings.mt5.server
        df["source"] = f"mt5:{server_name}"
        df["ingested_at"] = self.clock.now_utc()

        # Deduplicate and sort
        df = df.drop_duplicates(subset=["session_date"], keep="last")
        df = df.sort_values(by="session_date").reset_index(drop=True)

        cols = [
            "session_date",
            "ts_utc",
            "open",
            "high",
            "low",
            "close",
            "real_volume",
            "tick_volume",
            "spread",
            "adj_factor",
            "source",
            "ingested_at",
        ]
        return df[cols]

    def last_tick_age_seconds(self, ticker: str) -> float | None:
        """Calculate age in seconds of the latest tick received for ticker."""
        mt5 = self._ensure_module()
        self.ensure_symbol(ticker)
        tick = mt5.symbol_info_tick(ticker)
        if tick is None or not getattr(tick, "time", None):
            return None
        now_ts = int(self.clock.now_utc().timestamp())
        return max(0.0, float(now_ts - tick.time))

    def health(self) -> HealthReport:
        """Run connectivity and health checks on terminal."""
        mt5 = self._ensure_module()
        details: dict[str, Any] = {}

        t0 = time_mod.perf_counter()
        ver = mt5.version() if hasattr(mt5, "version") else None
        ping_ms = (time_mod.perf_counter() - t0) * 1000.0

        term_info = mt5.terminal_info()
        acc_info = mt5.account_info()

        connected = bool(term_info and getattr(term_info, "connected", False))
        trade_allowed = bool(
            term_info
            and getattr(term_info, "trade_allowed", False)
            and acc_info
            and getattr(acc_info, "trade_expert", False)
        )

        # Silent-disconnect detection uses a liquid stock (PRD 1.3.1); IBOV is reported as extra info.
        hb_symbol = self.settings.mt5.heartbeat_symbol
        tick_age: float | None = None
        try:
            tick_age = self.last_tick_age_seconds(hb_symbol)
        except Exception as e:
            details["tick_error"] = str(e)
        try:
            details["ibov_tick_age"] = self.last_tick_age_seconds(self.settings.mt5.ibov_symbol)
        except Exception as e:
            details["ibov_tick_error"] = str(e)

        version_str = f"{ver[0]}.{ver[1]} ({ver[2]})" if ver else None
        ok = connected and (acc_info is not None)

        details["terminal_path"] = self.settings.mt5.terminal_path
        details["server"] = self.settings.mt5.server
        details["login"] = acc_info.login if acc_info else None
        details["balance"] = acc_info.balance if acc_info else None
        details["equity"] = acc_info.equity if acc_info else None

        return HealthReport(
            ok=ok,
            terminal_version=version_str,
            connected=connected,
            trade_allowed=trade_allowed,
            tick_age_seconds=tick_age,
            ping_ms=round(ping_ms, 2),
            details=details,
        )
