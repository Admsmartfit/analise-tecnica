"""End-to-end paper routines with FakeMT5 (PRD S6 acceptance, without the real terminal)."""
from __future__ import annotations

from pathlib import Path

import pytest
from helpers import market

import ts.app.routines as routines
import ts.data.sync as sync_mod
from ts.app.weekly_report import generate_weekly_report
from ts.core.calendar import B3Calendar
from ts.core.config import Settings
from ts.data.fake_mt5 import FakeMT5
from ts.data.mt5_gateway import MT5Gateway
from ts.persistence.db import DatabaseManager


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    bars, ibov, sessions = market(330)
    tickers = sorted(bars)
    monkeypatch.setattr(routines, "universe_on", lambda d: tickers)
    monkeypatch.setattr(sync_mod, "universe_on", lambda d: tickers)
    monkeypatch.setattr(routines, "update_universe_file", lambda d: None)  # no network in tests
    s = Settings()
    s.system.data_dir = str(tmp_path / "data")
    s.system.db_path = str(tmp_path / "data" / "db" / "t.db")
    s.system.logs_dir = str(tmp_path / "logs")
    s.system.lock_path = str(tmp_path / "ts.lock")
    s.mt5.ibov_symbol = "IBOV"
    s.mt5.heartbeat_symbol = tickers[0]
    fake = FakeMT5()
    for t, df in bars.items():
        fake.set_bars(t, df)
    fake.set_bars("IBOV", ibov)
    gw = MT5Gateway(fake, s)
    gw.read_backoff_base_s = 0
    msgs: list[str] = []
    return s, gw, sessions, msgs


def test_paper_cycle(env) -> None:  # type: ignore[no-untyped-def]
    s, gw, sessions, msgs = env
    cal = B3Calendar()
    d1, d2, d3 = sessions[-3], sessions[-2], sessions[-1]

    assert routines.eod_routine(d1, settings=s, gateway=gw, calendar=cal, notify=msgs.append) == 0
    db = DatabaseManager(s.system.db_path)
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM equity_daily WHERE curve='PAPER'").fetchone()[0] == 1

    # running EOD again for the same session is a no-op (no duplicated orders)
    n_events = conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
    assert routines.eod_routine(d1, settings=s, gateway=gw, calendar=cal, notify=msgs.append) == 0
    assert conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == n_events

    assert routines.preopen_routine(d2, settings=s, gateway=gw, calendar=cal, notify=msgs.append) == 0
    # skip d2's EOD on purpose: d3's EOD must catch up d2 and d3 (no lost fills)
    assert routines.eod_routine(d3, settings=s, gateway=gw, calendar=cal, notify=msgs.append) == 0
    snaps = [r[0] for r in conn.execute("SELECT session_date FROM book_snapshots ORDER BY 1")]
    assert snaps == [d1.isoformat(), d2.isoformat(), d3.isoformat()]

    assert routines.deadman_routine(d3, settings=s, calendar=cal, notify=msgs.append) == 0
    assert routines.backup_routine(d3, settings=s, notify=msgs.append) == 0
    assert any(p.name.startswith("trading_") for p in (Path(s.system.data_dir) / "backups").iterdir())
    text = generate_weekly_report(s.system.db_path, d3, send_telegram=False)
    assert "Duplicate orders | ✅" in text
    assert not any("FAILED" in m for m in msgs), msgs


def test_eod_fails_safe_when_data_missing(env) -> None:  # type: ignore[no-untyped-def]
    s, gw, sessions, msgs = env
    gw.mt5._bars.pop("IBOV")
    rc = routines.eod_routine(sessions[-1], settings=s, gateway=gw, calendar=B3Calendar(), notify=msgs.append)
    assert rc == 4
    assert any("falha segura" in m for m in msgs)


def test_holiday_is_skipped(env) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    s, gw, _, msgs = env
    assert routines.eod_routine(date(2024, 12, 25), settings=s, gateway=gw, notify=msgs.append) == 0
