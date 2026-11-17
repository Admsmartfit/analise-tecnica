"""Fake MetaTrader 5 module for offline testing, fault injection and CI."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, NamedTuple

import numpy as np
import pandas as pd

# MT5 Dtype for rates
MT5_RATES_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<i8"),
        ("spread", "<i4"),
        ("real_volume", "<i8"),
    ]
)

# MT5 Constants
TIMEFRAME_D1 = 16408
TIMEFRAME_M5 = 5

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_REMOVE = 8

ORDER_TIME_GTC = 0
ORDER_TIME_DAY = 1

# Scripted order_send outcome: the order IS executed at the broker but the response is lost (PRD 2.5).
LOST_RESPONSE = object()

ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_CHANGED = 10020
TRADE_RETCODE_PRICE_OFF = 10021
TRADE_RETCODE_TOO_MANY_REQUESTS = 10024
TRADE_RETCODE_TRADE_DISABLED = 10027
TRADE_RETCODE_INVALID_FILL = 10030


class TerminalInfo(NamedTuple):
    community_account: bool = False
    connected: bool = True
    dlls_allowed: bool = True
    trade_allowed: bool = True
    tradeapi_disabled: bool = False
    email_enabled: bool = False
    ftp_enabled: bool = False
    notifications_enabled: bool = False
    mqid: bool = False
    build: int = 4150
    maxbars: int = 100000
    codepage: int = 1252
    ping_last: int = 15000
    community_balance: float = 0.0
    community_connection: bool = False
    name: str = "MetaTrader 5"
    company: str = "MetaQuotes Ltd."
    language: str = "Portuguese"
    path: str = "C:\\Program Files\\MetaTrader 5"
    data_path: str = "C:\\Users\\ralan\\AppData\\Roaming\\MetaQuotes\\Terminal"
    commondata_path: str = "C:\\ProgramData\\MetaQuotes\\Terminal\\Common"


class AccountInfo(NamedTuple):
    login: int = 12345678
    trade_mode: int = 0
    leverage: int = 1
    limit_orders: int = 500
    margin_so_mode: int = 0
    trade_allowed: bool = True
    trade_expert: bool = True
    margin_mode: int = 2
    currency_digits: int = 2
    balance: float = 100000.0
    credit: float = 0.0
    profit: float = 0.0
    equity: float = 100000.0
    margin: float = 0.0
    margin_free: float = 100000.0
    margin_level: float = 0.0
    margin_so_call: float = 0.0
    margin_so_so: float = 0.0
    margin_initial: float = 0.0
    margin_maintenance: float = 0.0
    assets: float = 0.0
    liabilities: float = 0.0
    commission_blocked: float = 0.0
    name: str = "Investidor Teste"
    server: str = "XP-PRD"
    currency: str = "BRL"
    company: str = "XP Investimentos"


class SymbolInfo(NamedTuple):
    name: str
    visible: bool = True
    trade_tick_size: float = 0.01
    filling_mode: int = ORDER_FILLING_RETURN
    volume_min: float = 100.0
    volume_step: float = 100.0
    point: float = 0.01
    digits: int = 2
    spread: int = 1
    session_deals: int = 1000
    bid: float = 30.0
    ask: float = 30.01
    last: float = 30.0


class SymbolTick(NamedTuple):
    time: int
    bid: float
    ask: float
    last: float
    volume: int
    time_msc: int
    flags: int
    volume_real: float


class OrderSendResult(NamedTuple):
    retcode: int
    deal: int
    order: int
    volume: float
    price: float
    bid: float
    ask: float
    comment: str
    request_id: int
    retcode_external: int


class OrderCheckResult(NamedTuple):
    retcode: int
    balance: float
    equity: float
    profit: float
    margin: float
    margin_free: float
    margin_level: float
    comment: str


class FakeMT5:
    """Mock implementation of the MetaTrader 5 Python API."""

    def __init__(self, bars: dict[str, pd.DataFrame] | None = None) -> None:
        self._connected = False
        self._logged_in = True
        self._last_error = (1, "OK")
        self._calls_remaining: int | None = None
        self._scripted_responses: dict[str, list[Any]] = defaultdict(list)
        self._symbols: dict[str, SymbolInfo] = {}
        self._bars: dict[str, np.ndarray] = {}
        self._orders: dict[int, dict[str, Any]] = {}
        self._positions: dict[int, dict[str, Any]] = {}
        self._deals: list[dict[str, Any]] = []
        self._next_order_id = 1001
        self._next_deal_id = 5001

        if bars:
            for ticker, df in bars.items():
                self.set_bars(ticker, df)

    def script(self, method_name: str, returns: list[Any]) -> None:
        """Queue scripted responses for method calls (FIFO)."""
        self._scripted_responses[method_name].extend(returns)

    def disconnect_after(self, calls: int) -> None:
        """Inject sudden disconnection after N method invocations."""
        self._calls_remaining = calls

    def _tick_call(self) -> None:
        if self._calls_remaining is not None:
            self._calls_remaining -= 1
            if self._calls_remaining <= 0:
                self._connected = False

    def set_last_error(self, code: int, description: str) -> None:
        self._last_error = (code, description)

    def set_logged_in(self, logged_in: bool) -> None:
        self._logged_in = logged_in

    def truncate_history(self, ticker: str, keep_last: int) -> None:
        """Simulate incomplete history by truncating rates."""
        if ticker in self._bars:
            arr = self._bars[ticker]
            self._bars[ticker] = arr[-keep_last:]

    def set_bars(self, ticker: str, df: pd.DataFrame) -> None:
        """Load DataFrame into MT5 structured array."""
        n = len(df)
        arr = np.zeros(n, dtype=MT5_RATES_DTYPE)
        # Parse timestamp or session_date robustly
        if "ts_utc" in df.columns:
            ts = [int(pd.Timestamp(t).timestamp()) for t in df["ts_utc"]]
        elif "session_date" in df.columns:
            ts = [int(pd.Timestamp(t).timestamp()) for t in df["session_date"]]
        else:
            ts = [int(pd.Timestamp(t).timestamp()) for t in df.index]

        arr["time"] = ts
        arr["open"] = df["open"].values
        arr["high"] = df["high"].values
        arr["low"] = df["low"].values
        arr["close"] = df["close"].values
        arr["tick_volume"] = df.get("tick_volume", 1000).values
        arr["spread"] = df.get("spread", 1).values
        arr["real_volume"] = df.get("real_volume", 50000).values
        self._bars[ticker] = arr

        if ticker not in self._symbols:
            self._symbols[ticker] = SymbolInfo(name=ticker)

    # MT5 API Methods
    def initialize(
        self,
        path: str | None = None,
        login: int | None = None,
        server: str | None = None,
        password: str | None = None,
        timeout: int | None = None,
        portable: bool = False,
    ) -> bool:
        self._tick_call()
        if "initialize" in self._scripted_responses and self._scripted_responses["initialize"]:
            res = self._scripted_responses["initialize"].pop(0)
            if isinstance(res, Exception):
                raise res
            if res:
                self._connected = True
            return bool(res)
        self._connected = True
        return True

    def shutdown(self) -> None:
        self._connected = False

    def login(self, login: int, password: str = "", server: str = "", timeout: int = 60000) -> bool:
        self._tick_call()
        if self._logged_in:
            return True
        return False

    def version(self) -> tuple[int, int, str]:
        return (500, 4150, "08 Mar 2024")

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    def terminal_info(self) -> TerminalInfo | None:
        self._tick_call()
        if not self._connected:
            return None
        return TerminalInfo(connected=self._connected)

    def account_info(self) -> AccountInfo | None:
        self._tick_call()
        if not self._connected or not self._logged_in:
            return None
        return AccountInfo()

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        self._tick_call()
        if symbol not in self._symbols:
            self._symbols[symbol] = SymbolInfo(name=symbol, visible=enable)
        else:
            current = self._symbols[symbol]
            self._symbols[symbol] = current._replace(visible=enable)
        return True

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        self._tick_call()
        if "symbol_info" in self._scripted_responses and self._scripted_responses["symbol_info"]:
            return self._scripted_responses["symbol_info"].pop(0)
        return self._symbols.get(symbol, SymbolInfo(name=symbol, visible=False))

    def symbol_info_tick(self, symbol: str) -> SymbolTick | None:
        self._tick_call()
        if "symbol_info_tick" in self._scripted_responses and self._scripted_responses["symbol_info_tick"]:
            return self._scripted_responses["symbol_info_tick"].pop(0)
        now_ts = int(datetime.now(UTC).timestamp())
        sym = self.symbol_info(symbol)
        last_price = sym.last if sym else 30.0
        return SymbolTick(
            time=now_ts,
            bid=last_price,
            ask=last_price + 0.01,
            last=last_price,
            volume=100,
            time_msc=now_ts * 1000,
            flags=0,
            volume_real=100.0,
        )

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> np.ndarray | None:
        self._tick_call()
        if "copy_rates_range" in self._scripted_responses and self._scripted_responses["copy_rates_range"]:
            resp = self._scripted_responses["copy_rates_range"].pop(0)
            if resp is None:
                self.set_last_error(-10004, "IPC Error")
            return resp

        if symbol not in self._bars:
            return None

        arr = self._bars[symbol]
        t_from = int(date_from.timestamp())
        t_to = int(date_to.timestamp())
        mask = (arr["time"] >= t_from) & (arr["time"] <= t_to)
        sub = arr[mask]
        return sub if len(sub) > 0 else np.array([], dtype=MT5_RATES_DTYPE)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> np.ndarray | None:
        self._tick_call()
        if symbol not in self._bars:
            return None
        arr = self._bars[symbol]
        if start_pos >= len(arr):
            return np.array([], dtype=MT5_RATES_DTYPE)
        end = len(arr) - start_pos
        start = max(0, end - count)
        return arr[start:end]

    def order_check(self, request: dict[str, Any]) -> OrderCheckResult:
        self._tick_call()
        return OrderCheckResult(
            retcode=0,
            balance=100000.0,
            equity=100000.0,
            profit=0.0,
            margin=1000.0,
            margin_free=99000.0,
            margin_level=10000.0,
            comment="Done",
        )

    def order_send(self, request: dict[str, Any]) -> OrderSendResult | None:
        self._tick_call()
        lost = False
        if "order_send" in self._scripted_responses and self._scripted_responses["order_send"]:
            resp = self._scripted_responses["order_send"].pop(0)
            if resp is LOST_RESPONSE:
                lost = True  # execute below, then drop the response
            else:
                if resp is None:
                    self.set_last_error(-10005, "IPC timeout")
                return resp  # type: ignore[no-any-return]

        if request.get("action") == TRADE_ACTION_SLTP:
            for p in self._positions.values():
                if p["symbol"] == request.get("symbol"):
                    p["sl"] = float(request.get("sl", 0.0))
            return OrderSendResult(TRADE_RETCODE_DONE, 0, 0, 0.0, 0.0, 0.0, 0.0, "SLTP", 1, 0)
        if request.get("action") == TRADE_ACTION_REMOVE:
            self._orders.pop(int(request.get("order", 0)), None)
            return OrderSendResult(TRADE_RETCODE_DONE, 0, 0, 0.0, 0.0, 0.0, 0.0, "Removed", 1, 0)

        # Default simulated execution
        oid = self._next_order_id
        deal_id = self._next_deal_id
        self._next_order_id += 1
        self._next_deal_id += 1

        action = request.get("action", TRADE_ACTION_DEAL)
        vol = float(request.get("volume", 100))
        price = float(request.get("price", 30.0))
        comment = request.get("comment", "")
        symbol = request.get("symbol", "PETR4")

        retcode = TRADE_RETCODE_DONE if action == TRADE_ACTION_DEAL else TRADE_RETCODE_PLACED

        order_record = {
            "order": oid,
            "symbol": symbol,
            "volume": vol,
            "price_open": price,
            "sl": float(request.get("sl", 0.0)),
            "tp": float(request.get("tp", 0.0)),
            "type": request.get("type", ORDER_TYPE_BUY),
            "state": 1,  # active
            "comment": comment,
            "magic": request.get("magic", 0),
        }
        if action != TRADE_ACTION_DEAL:
            self._orders[oid] = order_record  # pending orders stay in orders_get; market orders do not

        if action == TRADE_ACTION_DEAL:
            deal_record = {
                "deal": deal_id,
                "order": oid,
                "symbol": symbol,
                "volume": vol,
                "price": price,
                "commission": 0.0,
                "fee": 0.0,
                "time": int(datetime.now(UTC).timestamp()),
                "comment": comment,
                "magic": request.get("magic", 0),
            }
            self._deals.append(deal_record)
            self._positions[oid] = {**order_record, "ticket": oid, "price_current": price}

        if lost:
            self.set_last_error(-10005, "IPC timeout")
            return None
        return OrderSendResult(
            retcode=retcode,
            deal=deal_id if action == TRADE_ACTION_DEAL else 0,
            order=oid,
            volume=vol,
            price=price,
            bid=price,
            ask=price + 0.01,
            comment=comment,
            request_id=1,
            retcode_external=0,
        )

    def orders_get(self, symbol: str | None = None, ticket: int | None = None) -> Sequence[Any] | None:
        self._tick_call()
        results: list[Any] = []
        for o in self._orders.values():
            if ticket is not None and o["order"] != ticket:
                continue
            if symbol is not None and o["symbol"] != symbol:
                continue
            results.append(SimpleNamespace(**o))
        return tuple(results)

    def positions_get(self, symbol: str | None = None, ticket: int | None = None) -> Sequence[Any] | None:
        self._tick_call()
        results: list[Any] = []
        for p in self._positions.values():
            if ticket is not None and p["order"] != ticket:
                continue
            if symbol is not None and p["symbol"] != symbol:
                continue
            results.append(SimpleNamespace(**p))
        return tuple(results)

    def history_deals_get(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        group: str | None = None,
        ticket: int | None = None,
        position: int | None = None,
    ) -> Sequence[Any] | None:
        self._tick_call()
        results: list[Any] = []
        for d in self._deals:
            if ticket is not None and d["deal"] != ticket:
                continue
            results.append(SimpleNamespace(**d))
        return tuple(results)
