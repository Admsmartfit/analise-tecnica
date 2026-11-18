"""Local terminal API (PRD v2, U1): read-only endpoints, token-protected actions, empty states."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
from helpers import market

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ts.app.api import create_app  # noqa: E402
from ts.core.config import Settings  # noqa: E402
from ts.data.ohlcv_store import OhlcvStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "config", tmp_path / "config", ignore=shutil.ignore_patterns("settings.local.yaml"))
    bars, ibov, _ = market(300)
    store = OhlcvStore(tmp_path / "data" / "ohlcv" / "d1")
    for t, df in bars.items():
        store.upsert(t, df)
    store.upsert("IBOV", ibov)
    (tmp_path / "config" / "universe").mkdir(exist_ok=True)
    for f in (tmp_path / "config" / "universe").glob("*.csv"):
        f.unlink()
    (tmp_path / "config" / "universe" / "ibx100_2024_Q1.csv").write_text(
        "ticker\n" + "\n".join(sorted(bars)) + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(root: Path) -> TestClient:
    s = Settings()
    s.system.data_dir = "data"
    s.system.db_path = "data/db/trading.db"
    app = create_app(root, settings=s, token="secret-token")
    return TestClient(app)


def test_index_injects_token_and_serves_static(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "secret-token" in r.text and "__TS_TOKEN__" not in r.text
    assert client.get("/static/app.js").status_code == 200
    assert "/static/app.js?v=" in r.text and "/static/app.css?v=" in r.text  # cache-busting after updates
    assert r.headers["cache-control"] == "no-store" and client.get("/static/app.css").headers["cache-control"] == "no-cache"
    assert client.get("/static/vendor/lightweight-charts.standalone.production.js").status_code == 200


def test_status_on_fresh_install(client: TestClient) -> None:
    s = client.get("/api/status").json()
    assert s["mode"] == "paper" and s["paper_started"] is False
    assert s["last_eod"] is None and s["data"]["last_bar"] is not None
    assert s["next_event"]["label"] in ("Pré-abertura", "Fim do dia (EOD)")


def test_watchlist_and_chart(client: TestClient) -> None:
    rows = client.get("/api/watchlist").json()
    assert rows[0]["ticker"] == "IBOV" and rows[0]["is_index"]
    stock = next(r for r in rows if not r["is_index"])
    assert stock["trend"] in ("up", "down") and stock["atr_pct"] > 0 and stock["signal"] is None
    ch = client.get(f"/api/chart/{stock['ticker']}?period=6M").json()
    assert 100 <= len(ch["candles"]) <= 126 and ch["candles"][0]["time"] < ch["candles"][-1]["time"]
    assert set(ch["lines"]) == {"sma200", "sma50", "ema20"}
    assert client.get("/api/chart/NAOEXISTE").status_code == 404


def test_checklist_endpoint(client: TestClient) -> None:
    rows = client.get("/api/watchlist").json()
    t = next(r["ticker"] for r in rows if not r["is_index"])
    d = client.get(f"/api/checklist/{t}").json()
    assert d["ticker"] == t and len(d["common"]) == 3
    ids = {s["id"]: s for s in d["setups"]}
    assert set(ids) == {"A", "B", "C", "D", "E", "F"} and ids["A"]["total"] >= 5
    from ts.core.config import load_strategy_config

    cfg, _ = load_strategy_config(REPO / "config" / "strategy.yaml")
    assert all(ids[k]["enabled"] == getattr(cfg.setups, k).enabled for k in ids)  # mirrors strategy.yaml
    assert all({"label", "ok", "value", "limit"} <= set(c) for c in ids["A"]["conditions"])
    assert client.get("/api/checklist/NAOEXISTE").status_code == 404


@pytest.mark.parametrize("router", [False, True])
def test_checklist_agrees_with_signal_engine(router: bool) -> None:
    """The Raio-X only explains: 'SINAL' must match 'every condition passed' on every day, and both must match
    what the signal engine + regime router decide."""
    from ts.app.checklist import build_checklist
    from ts.core.config import StrategyConfig

    bars, ibov, sessions = market(320)
    cfg = StrategyConfig()
    cfg.market_filter.require_bull_regime = False
    cfg.min_history_bars = 120
    cfg.regime_router.enabled = router
    seen = 0
    for t, df in list(bars.items())[:4]:
        for d in sessions[-60::3]:
            r = build_checklist(t, df[df["session_date"] <= d], ibov[ibov["session_date"] <= d], cfg)
            for s in r["setups"]:
                if not s["total"]:
                    continue
                all_ok = r["common_ok"] and all(c["ok"] for c in s["conditions"])
                assert s["fired"] == all_ok, (t, d, s)
                seen += 1
    assert seen > 0


def test_empty_terminal_tabs(client: TestClient) -> None:
    assert client.get("/api/positions").json() == []
    assert client.get("/api/orders").json() == []
    assert client.get("/api/signals").json() == {"date": None, "items": []}
    assert client.get("/api/runs").json() == []


def test_actions_require_token(client: TestClient) -> None:
    assert client.post("/api/actions/eod").status_code == 403
    assert client.post("/api/actions/eod", headers={"X-TS-Token": "wrong"}).status_code == 403
    assert client.post("/api/actions/rm-rf", headers={"X-TS-Token": "secret-token"}).status_code == 404


def test_last_closed_session_respects_close(client: TestClient) -> None:
    from datetime import datetime

    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    d = date(2024, 6, 27)  # Thursday, session
    assert ctx.last_closed_session(datetime(2024, 6, 27, 15, 0)) == date(2024, 6, 26)
    assert ctx.last_closed_session(datetime(2024, 6, 27, 20, 0)) == d


def test_equity_empty_and_eod_pending_on_fresh_install(client: TestClient) -> None:
    assert client.get("/api/equity").json() == {"paper": [], "shadow": [], "summary": None}
    assert client.get("/api/status").json()["eod_pending"] is True


def test_status_shows_quarantine_and_pending_candidate(client: TestClient, root: Path) -> None:
    import json as _json

    from ts.core.config import StrategyConfig
    from ts.execution.session_engine import BookState
    from ts.persistence.db import DatabaseManager
    from ts.persistence.repository import PaperRepository

    db = DatabaseManager(root / "data" / "db" / "trading.db")
    conn = db.get_conn()
    st = BookState.new(100_000, StrategyConfig())
    st.breaker._halt("6 perdas consecutivas")
    PaperRepository(conn).save_snapshot(date(2024, 6, 27), st, "h")
    conn.execute("INSERT INTO config_candidates (candidate_id, created_at, trigger_reason, market_regime, base_hash, "
                 "current_hash, status, params_json, summary_json) VALUES ('R1', '2024-06-27', '6 perdas consecutivas', "
                 "'IBOV SIDEWAYS', 'b', 'c', 'PENDING', ?, ?)",
                 (_json.dumps({"setups.B.rsi_max": 50}), _json.dumps({"expectancy_r": 0.12})))
    conn.commit()
    db.close()
    s = client.get("/api/status").json()
    assert s["quarantine"] == {"active": True, "reason": "6 perdas consecutivas", "loss_streak": 0}
    assert s["candidate"]["id"] == "R1" and s["candidate"]["params"] == {"setups.B.rsi_max": 50}
    assert client.get("/api/candidates").json()[0]["status"] == "PENDING"
    assert client.post("/api/actions/approve", headers={"X-TS-Token": "secret-token"}).status_code == 400
