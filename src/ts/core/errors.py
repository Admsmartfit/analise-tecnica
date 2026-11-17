"""Exception hierarchy for Trading System B3."""
from __future__ import annotations


class TSError(Exception):
    """Base exception for all trading system errors."""


class ConfigError(TSError):
    """Invalid configuration file or missing mandatory parameter."""


class CalendarError(TSError):
    """Date outside recognized trading calendar or holiday mismatch."""


class DataError(TSError):
    """Base exception for data layer errors."""


class DataUnavailableError(DataError):
    """Market data not returned by broker/terminal."""


class DataQualityError(DataError):
    """Data consistency check failed (BLOCK severity)."""


class LookaheadError(DataError):
    """Lookahead bias detected: accessed future data relative to decision point."""


class ConnectivityError(TSError):
    """Terminal or connection issue."""


class TerminalNotRunningError(ConnectivityError):
    """MetaTrader 5 terminal process is not running."""


class IPCError(ConnectivityError):
    """Inter-process communication error with MetaTrader 5."""


class NotLoggedInError(ConnectivityError):
    """MetaTrader 5 terminal is not logged into an account."""


class BrokerError(TSError):
    """Order placement or broker execution error."""


class OrderRejectedError(BrokerError):
    """Order rejected by broker or market."""


class OrderStateUnknownError(BrokerError):
    """Order response lost or state cannot be verified without reconciliation."""


class TradingDisabledError(BrokerError):
    """Automated trading disabled in MT5 terminal or account."""


class RiskLimitError(TSError):
    """Order violates portfolio or risk limit."""


class OrderStateError(TSError):
    """Illegal state transition in order lifecycle."""
