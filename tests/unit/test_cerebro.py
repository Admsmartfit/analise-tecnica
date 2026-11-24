"""Integração com o TradingAgents B3 ("Cérebro") — INTEGRACAO_ROBO_TECNICO.md."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from helpers import market

from ts.app import cerebro as cb
from ts.core.config import CerebroSettings, CostsConfig, Settings, StrategyConfig, strategy_config_hash
from ts.core.models import TradeClass
from ts.execution.session_engine import BookState, PendingOrder, SessionEngine
from ts.persistence.db import apply_migrations
from ts.strategy.signal_engine import prepare_ticker


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    apply_migrations(c)
    return c


def _decision(ativo: str = "PETR4.SA", status: str = "autorizado", at: datetime | None = None, days: int = 10,
              id_: int = 1, conv: float = 0.88, origem: str = "IA: gemini") -> dict:
    at = at or datetime(2026, 11, 23, 14, 48, 3, 740668)
    return {"ativo": ativo, "status": status, "conviccao": conv, "validade_dias": days,
            "tese_resumida": "Fundamentos sólidos.", "indicadores_chave": {"PL": 3.92, "DY_pct": 8.5},
            "data_autorizacao": at.isoformat(), "data_expiracao": (at + timedelta(days=days)).isoformat(),
            "timestamp": at.isoformat(), "id_analise": id_, "origem_decisao": origem,
            "fontes_consultadas": "Fundamentus, Yahoo Finance", "campo_novo_ignorado": 1}


def _store(conn: sqlite3.Connection, **kw: object) -> bool:
    raw = _decision(**kw)  # type: ignore[arg-type]
    return cb.store_decision(conn, cb.Decision.model_validate(raw), raw)


# ------------------------------------------------------------------------------------------- ② decisions
def test_decision_is_normalized_and_deduplicated(conn: sqlite3.Connection) -> None:
    assert _store(conn) is True
    assert _store(conn) is False  # resend of the same id_analise
    d = cb.latest_decision(conn, "petr4", datetime(2026, 11, 24))
    assert d is not None and d["ativo"] == "PETR4" and d["status"] == "autorizado"
    assert json.loads(d["indicadores_json"])["PL"] == 3.92
    with pytest.raises(ValueError):
        cb.Decision.model_validate({**_decision(), "status": "talvez"})
    with pytest.raises(ValueError):
        cb.Decision.model_validate({**_decision(), "ativo": "PETR4; DROP TABLE"})


def test_evaluate_rules(conn: sqlite3.Connection) -> None:
    cfg = CerebroSettings(enabled=True)
    at = datetime(2026, 11, 25, 13, 0)
    assert cb.evaluate(None, at, cfg)[0] is False
    _store(conn)
    ok, why = cb.evaluate(cb.latest_decision(conn, "PETR4", at), at, cfg)
    assert ok and "autorizado" in why
    late = datetime(2026, 12, 4, 13, 0)  # after data_expiracao (23/11 + 10 days)
    assert cb.evaluate(cb.latest_decision(conn, "PETR4", late), late, cfg) == (
        False, "Cérebro: autorização vencida em 2026-12-03 14:48 UTC")
    _store(conn, status="rejeitado", id_=2, at=datetime(2026, 11, 24, 12, 0))
    assert cb.evaluate(cb.latest_decision(conn, "PETR4", at), at, cfg)[0] is False  # higher id replaces
    _store(conn, ativo="VALE3", id_=3, conv=0.55)
    assert cb.evaluate(cb.latest_decision(conn, "VALE3", at), at, CerebroSettings(min_conviccao=0.7))[0] is False
    _store(conn, ativo="WEGE3", id_=4, origem="Regra de contingência (sem IA)")
    assert cb.evaluate(cb.latest_decision(conn, "WEGE3", at), at, CerebroSettings(allow_contingency=False))[0] is False
    assert cb.evaluate(cb.latest_decision(conn, "WEGE3", at), at, CerebroSettings())[0] is True


def test_session_open_is_10h_sao_paulo_in_utc() -> None:
    assert cb.session_open_utc(date(2026, 11, 23)) == datetime(2026, 11, 23, 13, 0)


# ------------------------------------------------------------------------------------------- gate in the engine
def _engine_with_order(gate: cb.CerebroGate | None) -> tuple:
    bars, _, sessions = market(330)
    cfg = StrategyConfig()
    eng = SessionEngine(cfg, CostsConfig(), strategy_config_hash(cfg), order_prefix="T")
    eng.entry_gate = gate
    t = "AAAA3"
    prepared = {t: prepare_ticker(t, bars[t], cfg)}
    d = sessions[-5]
    o = float(prepared[t].view._cols["raw_open"][prepared[t].index_of(d)])
    st = BookState.new(100_000, cfg)
    st.pending.append(PendingOrder("T1", None, t, "BUY", "OPEN_AUCTION", 100, o, o * 1.05, o * 0.95, None, "A",
                                   "TREND", d, risk_budget=10_000.0, max_fill_price=o * 1.05))
    return eng, st, d, prepared, t


def test_gate_blocks_buy_without_authorization_and_never_looks_ahead(conn: sqlite3.Connection) -> None:
    eng, st, d, prepared, t = _engine_with_order(cb.CerebroGate(conn, CerebroSettings(enabled=True)))
    res = eng.run_session(st, d, prepared, market_ok=False)
    assert not res.fills and res.expired[0][1].startswith("Cérebro: sem análise")
    # authorization that only arrived AFTER that day's open must not count (paper simulates the day at night)
    eng, st, d, prepared, t = _engine_with_order(cb.CerebroGate(conn, CerebroSettings(enabled=True)))
    _store(conn, ativo=t, id_=10, at=cb.session_open_utc(d) + timedelta(minutes=5))
    res = eng.run_session(st, d, prepared, market_ok=False)
    assert not res.fills
    # authorized before the open -> the buy happens
    eng, st, d, prepared, t = _engine_with_order(cb.CerebroGate(conn, CerebroSettings(enabled=True)))
    _store(conn, ativo=t, id_=11, at=cb.session_open_utc(d) - timedelta(hours=12))
    res = eng.run_session(st, d, prepared, market_ok=False)
    assert any(f.reason == "OPEN_AUCTION" for f in res.fills) and t in st.positions


def test_gate_advisory_only_records(conn: sqlite3.Connection) -> None:
    gate = cb.CerebroGate(conn, CerebroSettings(enabled=True, mode="advisory"))
    eng, st, d, prepared, t = _engine_with_order(gate)
    res = eng.run_session(st, d, prepared, market_ok=False)
    assert res.fills and gate.advisories and "consultivo" in gate.advisories[0]


def test_no_gate_keeps_old_behaviour() -> None:
    eng, st, d, prepared, t = _engine_with_order(None)
    assert eng.run_session(st, d, prepared, market_ok=False).fills


# ------------------------------------------------------------------------------------------- webhook app
def test_webhook_endpoint(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    s = Settings()
    s.cerebro = CerebroSettings(enabled=True, allowed_ips=["testclient"])
    db = tmp_path / "t.db"
    client = TestClient(cb.create_webhook_app(s, db))
    assert client.get("/health").json()["service"] == "ts-cerebro-webhook"
    r = client.post("/webhook/decisao", json=_decision())
    assert r.status_code == 200 and r.json()["recebido"] is True and r.json()["duplicado"] is False
    assert client.post("/webhook/decisao", json=_decision()).json()["duplicado"] is True
    assert client.post("/webhook/decisao", json={"ativo": "PETR4"}).status_code == 422
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM cerebro_decisions").fetchone()[0] == 1
    s2 = Settings()
    s2.cerebro = CerebroSettings(enabled=True)  # default: only 127.0.0.1 / ::1
    assert TestClient(cb.create_webhook_app(s2, db)).post("/webhook/decisao", json=_decision(id_=9)).status_code == 403


def test_webhook_ping_and_decision_without_id(tmp_path: Path) -> None:
    """Newer spec: 'ping_teste' answered at once, and decisions may come without id_analise."""
    from fastapi.testclient import TestClient

    s = Settings()
    s.cerebro = CerebroSettings(enabled=True, allowed_ips=["testclient"])
    db = tmp_path / "t.db"
    client = TestClient(cb.create_webhook_app(s, db))
    r = client.post("/webhook/decisao", json={"tipo": "ping_teste", "origem": "TradingAgents B3", "mensagem": "oi"})
    assert r.status_code == 200 and r.json() == {"status": "ok", "recebido": True}
    assert client.get("/health").json()["last_ping"]
    body = {"ativo": "PETR4", "status": "autorizado", "conviccao": 0.85, "validade_dias": 7,
            "tese_resumida": "Resumo.", "indicadores_chave": {"PL": 4.5, "ROE_pct": 28.0, "DY_pct": 14.0},
            "data_autorizacao": "2026-11-24T12:00:00", "data_expiracao": "2026-12-01T12:00:00"}
    r = client.post("/webhook/decisao", json=body)
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["duplicado"] is False
    assert client.post("/webhook/decisao", json=body).json()["duplicado"] is True  # same content: deduplicated
    c = sqlite3.connect(db)
    (id_,) = c.execute("SELECT id_analise FROM cerebro_decisions").fetchone()
    assert id_ < 0  # synthetic ids never collide with the Cérebro's positive ones
    assert cb.latest_decision(c, "PETR4", datetime(2026, 11, 25))["status"] == "autorizado"
    assert c.execute("SELECT COUNT(*) FROM cerebro_decisions").fetchone()[0] == 1  # the ping stored nothing


# ------------------------------------------------------------------------------------------- ① ③ outbox
def _trade(net: float) -> SimpleNamespace:
    return SimpleNamespace(position_id="TP1", ticker="PETR4", setup="F", trade_class=TradeClass.MEAN_REVERSION,
                           entry_date=date(2026, 11, 10), exit_date=date(2026, 11, 14), entry_price=40.0,
                           exit_price=41.2, qty=100, r_multiple=0.6, net_pnl_brl=net, exit_reason="TARGET",
                           bars_held=4, is_shadow=False)


def test_feedback_payload_follows_spec() -> None:
    p = cb.feedback_payload(_trade(118.0), "paper")
    assert p["ativo"] == "PETR4" and p["resultado"] == "WIN" and p["lucro_prejuizo_pct"] == 2.95
    assert "Alvo atingido" in p["motivo_grafico"] and p["detalhes"]["setup"] == "Reversão na baixa"
    assert cb.feedback_payload(_trade(-50.0), "paper")["resultado"] == "LOSS"


def test_outbox_sends_retries_and_gives_up(conn: sqlite3.Connection) -> None:
    calls: list[tuple[str, dict]] = []
    mode = {"code": 200}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.url.path, json.loads(req.content)))
        return httpx.Response(mode["code"], json={"status": "queued"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = CerebroSettings(enabled=True, base_url="http://cerebro:8000/")
    cb.enqueue_demand(conn, ["petr4.sa", "VALE3"], "alta", key="k1")
    cb.enqueue_demand(conn, ["PETR4"], "alta", key="k1")  # same key: not duplicated
    assert cb.enqueue_feedback(conn, _trade(10.0), "paper") and not cb.enqueue_feedback(conn, _trade(10.0), "paper")
    assert cb.flush_outbox(conn, cfg, client=client) == {"sent": 2, "retry": 0, "failed": 0}
    assert calls[0] == ("/api/queue/demand", {"ativos": ["PETR4", "VALE3"], "prioridade": "alta",
                                              "origem": "robo_tecnico"})
    assert calls[1][0] == "/api/memory/feedback" and calls[1][1]["resultado"] == "WIN"
    mode["code"] = 503
    cb.enqueue_demand(conn, ["WEGE3"], key="k2")
    assert cb.flush_outbox(conn, cfg, client=client)["retry"] == 1
    assert cb.flush_outbox(conn, cfg, client=client) == {"sent": 0, "retry": 0, "failed": 0}  # not due yet
    nxt = conn.execute("SELECT next_attempt_at, attempts FROM cerebro_outbox WHERE dedup_key='k2'").fetchone()
    assert nxt[1] == 1 and nxt[0] > cb._iso(cb.utcnow())
    mode["code"] = 422
    cb.enqueue_demand(conn, ["ITUB4"], key="k3")
    assert cb.flush_outbox(conn, cfg, client=client)["failed"] == 1  # invalid body: no point in resending
    assert cb.retry_failed(conn) == 1


def test_after_eod_asks_only_for_buys_without_valid_authorization(conn: sqlite3.Connection,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(cb, "flush_outbox", lambda c, cfg, budget_s=0: (
        sent.extend(json.loads(r[0]) for r in c.execute("SELECT payload_json FROM cerebro_outbox"))
        or {"sent": 0, "retry": 0, "failed": 0}))
    s = Settings()
    s.cerebro = CerebroSettings(enabled=True)
    nxt = date(2026, 11, 25)
    _store(conn, ativo="VALE3", id_=1, at=cb.session_open_utc(nxt) - timedelta(days=1))  # valid tomorrow
    po = lambda t, side="BUY", shadow=False: PendingOrder("x" + t, None, t, side, "OPEN_AUCTION", 100, 10.0, 10.3,  # noqa: E731
                                                          9.5, None, "A", "TREND", nxt, is_shadow=shadow)
    state = SimpleNamespace(pending=[po("PETR4"), po("VALE3"), po("WEGE3", shadow=True), po("ITUB4", side="SELL")])
    notes = cb.after_eod(conn, s, [_trade(5.0)], state, nxt)
    demand = [p for p in sent if "ativos" in p]
    assert demand == [{"ativos": ["PETR4"], "prioridade": "alta", "origem": "robo_tecnico"}]
    assert any(p.get("resultado") == "WIN" for p in sent) and any("PETR4" in n for n in notes)
    s.cerebro.enabled = False
    assert cb.after_eod(conn, s, [], state, nxt) == []


def test_local_config_is_written_outside_git(tmp_path: Path) -> None:
    import yaml

    from ts.core.config import load_settings

    (tmp_path / "settings.yaml").write_text("execution:\n  mode: paper\n", encoding="utf-8")
    cb.write_local_config(tmp_path, {"enabled": True, "base_url": "http://192.168.0.10:8000", "mode": None})
    raw = yaml.safe_load((tmp_path / "settings.local.yaml").read_text(encoding="utf-8"))
    assert raw == {"cerebro": {"enabled": True, "base_url": "http://192.168.0.10:8000"}}
    s = load_settings(tmp_path / "settings.yaml")
    assert s.cerebro.enabled and s.cerebro.base_url.endswith(":8000") and s.cerebro.mode == "required"
