"""YahooGateway with a mocked HTTP transport (no network in CI)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from ts.core.config import ConfigError, Settings, load_settings
from ts.core.errors import DataUnavailableError
from ts.data.yahoo_gateway import YahooGateway, make_gateway

# 2024-01-02 and 2024-01-03 at 10:00 BRT (13:00 UTC), plus a zero-volume flat artifact on 2024-01-04
TS = [1704200400, 1704286800, 1704373200]


def _payload(symbol: str) -> dict:
    return {"chart": {"result": [{
        "meta": {"symbol": symbol, "exchangeTimezoneName": "America/Sao_Paulo", "regularMarketTime": TS[1]},
        "timestamp": TS,
        "events": {"dividends": {str(TS[1]): {"amount": 0.5, "date": TS[1]}}},
        "indicators": {"quote": [{
            "open": [37.5, 38.0000001, 38.4], "high": [38.1, 38.9, 38.4], "low": [37.2, 37.9, 38.4],
            "close": [37.779998779296875, 38.95999908, 38.4], "volume": [24043800, 52300200, 0],
        }], "adjclose": [{"adjclose": [30.0, 31.0, 31.0]}]},
    }], "error": None}}


def _gw(handler) -> YahooGateway:  # type: ignore[no-untyped-def]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    g = YahooGateway(Settings(), client=client)
    g.read_backoff_base_s = 0
    return g


def test_symbol_mapping() -> None:
    g = YahooGateway(Settings())
    assert g.yahoo_symbol("PETR4") == "PETR4.SA"
    assert g.yahoo_symbol("IBOV") == "^BVSP"
    assert g.yahoo_symbol("^BVSP") == "^BVSP"


def test_daily_bars_schema_dates_and_cleaning() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json=_payload("PETR4.SA"))

    df = _gw(handler).daily_bars("PETR4", date(2024, 1, 1), date(2024, 1, 31))
    assert seen[0].endswith("/PETR4.SA")
    assert list(df["session_date"]) == [date(2024, 1, 2), date(2024, 1, 3)]  # flat zero-volume bar dropped
    assert df["close"].iloc[0] == 37.78 and df["real_volume"].iloc[1] == 52300200
    assert df["ts_utc"].iloc[0] == datetime(2024, 1, 2, 13, 0, tzinfo=UTC)
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert set(df["source"]) == {"yahoo"}


def test_retries_then_raises_on_429() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={})

    with pytest.raises(DataUnavailableError):
        _gw(handler).daily_bars("PETR4", date(2024, 1, 1), date(2024, 1, 31))
    assert calls["n"] == 3


def test_recovers_after_transient_error() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] == 1 else httpx.Response(200, json=_payload("X"))

    assert len(_gw(handler).daily_bars("PETR4", date(2024, 1, 1), date(2024, 1, 31))) == 2


def test_dividend_factor() -> None:
    rows = _gw(lambda r: httpx.Response(200, json=_payload("PETR4.SA"))).dividend_actions(
        "PETR4", date(2024, 1, 1), date(2024, 1, 31))
    assert rows == [{"ticker": "PETR4", "ex_date": "2024-01-03", "action_type": "DIVIDEND", "value": 0.5,
                     "factor": round(1 - 0.5 / 37.78, 8)}]


def test_health_and_factory() -> None:
    h = _gw(lambda r: httpx.Response(200, json=_payload("^BVSP"))).health()
    assert h.ok and h.trade_allowed is False
    s = Settings()
    s.data.provider = "yahoo"
    assert isinstance(make_gateway(s), YahooGateway)


def test_local_settings_override_and_live_guard(tmp_path: Path) -> None:
    (tmp_path / "settings.yaml").write_text("execution:\n  mode: paper\n", encoding="utf-8")
    (tmp_path / "settings.local.yaml").write_text("data:\n  provider: yahoo\nmt5:\n  login: 123\n", encoding="utf-8")
    s = load_settings(tmp_path / "settings.yaml")
    assert s.data.provider == "yahoo" and s.mt5.login == 123 and s.execution.mode == "paper"
    (tmp_path / "settings.local.yaml").write_text("data:\n  provider: yahoo\nexecution:\n  mode: live\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "settings.yaml")
