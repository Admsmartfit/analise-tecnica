"""Trading System B3 - Core Models."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal


class SetupId(str, Enum):
    A_PIVOT = "A"
    B_PULLBACK = "B"
    C_DONCHIAN = "C"
    D_SQUEEZE = "D"
    E_DIVERGENCE = "E"
    F_BEAR_REVERSION = "F"
    G_MONTHLY_MACD = "G"


class TradeClass(str, Enum):
    TREND = "TREND"
    MEAN_REVERSION = "MEAN_REVERSION"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderState(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    CLOSED = "CLOSED"


class ExitReason(str, Enum):
    STOP = "STOP"
    TARGET = "TARGET"
    TRAILING = "TRAILING"
    TIME = "TIME"
    STAGNATION = "STAGNATION"
    BREAKEVEN = "BREAKEVEN"
    MANUAL = "MANUAL"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


@dataclass(frozen=True, slots=True)
class Bar:
    ticker: str
    session_date: date
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    real_volume: int
    tick_volume: int
    spread: int


@dataclass(frozen=True, slots=True)
class PivotEvent:
    ticker: str
    kind: Literal["HIGH", "LOW"]
    pivot_date: date
    confirmed_date: date
    price: float
    atr_at_confirm: float
    # Positional bar indices within the series the detector ran on (-1 when unknown).
    pivot_idx: int = -1
    confirmed_idx: int = -1


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    ticker: str
    setup: SetupId
    trade_class: TradeClass
    decision_date: date
    entry_type: Literal["OPEN_AUCTION", "BUY_STOP"]
    entry_ref: float
    stop_price: float
    target_price: float | None
    features: dict[str, float]
    config_hash: str


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str
    signal_id: str | None
    ticker: str
    side: Literal["BUY", "SELL"]
    qty: int
    order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    price: Decimal | None
    stop_loss: Decimal | None
    valid_until: date
    reason: str
    position_id: str | None = None
    mode: Literal["paper", "live"] = "paper"
    take_profit: Decimal | None = None  # absolute TP attached to the entry (mean-reversion setups)
    limit_price: Decimal | None = None  # STOP_LIMIT: highest acceptable fill (risk cap)
    risk_budget: Decimal | None = None  # BRL the entry may lose at the stop; audited before sending


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: str
    broker_deal_id: str | None
    ts_utc: datetime
    qty: int
    price: Decimal
    fees: Decimal


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    position_id: str
    ticker: str
    qty: int
    entry_price: Decimal
    current_price: Decimal
    current_stop: Decimal | None
    target_price: Decimal | None
    ts_utc: datetime


@dataclass(frozen=True, slots=True)
class BrokerOrderState:
    client_order_id: str
    broker_order_id: str | None
    ticker: str
    state: OrderState
    qty: int
    filled_qty: int
    price: Decimal | None
    stop_loss: Decimal | None
    retcode: int | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    success: bool
    client_order_id: str
    broker_order_id: str | None
    state: OrderState
    retcode: int
    message: str


@dataclass(frozen=True, slots=True)
class HealthReport:
    ok: bool
    terminal_version: str | None
    connected: bool
    trade_allowed: bool
    tick_age_seconds: float | None
    ping_ms: float | None
    details: dict[str, Any]
