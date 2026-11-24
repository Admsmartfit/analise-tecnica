"""Integração com o TradingAgents B3 ("Cérebro" fundamentalista) — especificação em INTEGRACAO_ROBO_TECNICO.md.

Este sistema é o "Robô Técnico":

① ``POST {base_url}/api/queue/demand``  — pede análise dos ativos que as regras técnicas querem comprar.
② webhook ``POST {webhook_path}``        — recebe a decisão (autorizado / rejeitado / pausa_analise) e grava.
③ ``POST {base_url}/api/memory/feedback`` — informa o resultado de cada trade encerrado.

Regras de segurança do desenho:

* O Cérebro só **veta ou libera** compras que as regras técnicas já escolheram. Ele nunca cria ordens, não mexe
  em stops nem em saídas. Uma mensagem falsa no webhook (que não tem assinatura) no pior caso bloqueia ou libera
  uma compra que o próprio sistema já queria fazer.
* A decisão vale "como era na abertura": uma compra no pregão ``d`` só usa decisões com ``data_autorizacao``
  anterior às 10:00 de ``d`` (sem olhar o futuro, mesmo quando o paper simula o dia à noite).
* Sem decisão válida, com o modo ``required``, a compra não acontece (falha segura).
* Pedidos e resultados vão por uma fila de saída (``cerebro_outbox``) com reenvio: o Cérebro fora do ar nunca
  trava o EOD.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import Request  # module level: FastAPI resolves the (postponed) annotation from module globals
from pydantic import BaseModel, Field, ValidationError, field_validator

from ts.core.config import CerebroSettings, Settings

logger = logging.getLogger("ts.app.cerebro")

B3_TZ = ZoneInfo("America/Sao_Paulo")
B3_OPEN = dtime(10, 0)
RETRY_DELAYS_S = (60, 300, 900, 3600, 3 * 3600, 6 * 3600, 12 * 3600)  # then 'failed'
EXIT_PT = {"STOP": "Stop acionado", "TARGET": "Alvo atingido", "TRAILING": "Stop móvel acionado",
           "TIME": "Saída por tempo", "STAGNATION": "Saída por estagnação", "BREAKEVEN": "Saída no zero a zero",
           "MANUAL": "Saída manual", "CIRCUIT_BREAKER": "Saída pelo circuit breaker"}
SETUP_PT = {"A": "Pivô de alta", "B": "Pullback", "C": "Donchian 20/10", "D": "Bollinger squeeze",
            "E": "Divergência", "F": "Reversão na baixa", "G": "Força mensal + MACD"}


# ============================================================================================ helpers
def utcnow() -> datetime:
    """Naive UTC (the Cérebro sends naive UTC timestamps)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def parse_utc(s: str) -> datetime:
    """Cérebro timestamps are UTC without offset; an explicit offset (if ever sent) is honoured."""
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def session_open_utc(d: date) -> datetime:
    """10:00 in São Paulo on session ``d`` as naive UTC — the moment a buy in that session happens."""
    return datetime.combine(d, B3_OPEN, tzinfo=B3_TZ).astimezone(UTC).replace(tzinfo=None)


def norm_ticker(t: str) -> str:
    t = t.strip().upper()
    return t[:-3] if t.endswith(".SA") else t


# ============================================================================================ ② webhook
class Decision(BaseModel):
    """Webhook body (spec section 4). Unknown fields are ignored; every key of indicadores_chave is optional."""

    ativo: str
    status: Literal["autorizado", "rejeitado", "pausa_analise"]
    conviccao: float = Field(ge=0.0, le=1.0)
    validade_dias: int = Field(ge=1)
    tese_resumida: str = ""
    indicadores_chave: dict[str, Any] = Field(default_factory=dict)
    data_autorizacao: str
    data_expiracao: str
    timestamp: str | None = None
    # Optional in the newer spec: without it a deterministic NEGATIVE id is derived from the content, so resends of
    # the same decision are still deduplicated and can never collide with a real (positive) id from the Cérebro.
    id_analise: int | None = None
    origem_decisao: str | None = None
    fontes_consultadas: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.id_analise is None:
            import zlib

            key = f"{self.ativo}|{self.status}|{self.data_autorizacao}|{self.data_expiracao}|{self.conviccao}"
            self.id_analise = -(zlib.crc32(key.encode("utf-8")) or 1)

    @field_validator("ativo")
    @classmethod
    def _ticker(cls, v: str) -> str:
        v = norm_ticker(v)
        if not v or len(v) > 12 or not v.isalnum():
            raise ValueError("ticker inválido")
        return v

    @field_validator("data_autorizacao", "data_expiracao")
    @classmethod
    def _dt(cls, v: str) -> str:
        return _iso(parse_utc(v))


def store_decision(conn: sqlite3.Connection, d: Decision, raw: dict[str, Any]) -> bool:
    """Insert a decision. Returns False for a duplicate ``id_analise`` (a resend)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO cerebro_decisions (id_analise, ativo, status, conviccao, validade_dias, tese, "
        "indicadores_json, data_autorizacao, data_expiracao, origem_decisao, fontes, received_at, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d.id_analise, d.ativo, d.status, d.conviccao, d.validade_dias, d.tese_resumida[:4000],
         json.dumps(d.indicadores_chave, ensure_ascii=False)[:20000], d.data_autorizacao, d.data_expiracao,
         d.origem_decisao, d.fontes_consultadas, _iso(utcnow()), json.dumps(raw, ensure_ascii=False)[:50000]),
    )
    conn.commit()
    return cur.rowcount == 1


def latest_decision(conn: sqlite3.Connection, ticker: str, at: datetime | None = None) -> dict[str, Any] | None:
    """Most recent decision for ``ticker`` known at ``at`` (naive UTC; default now). Higher id_analise wins."""
    at = at or utcnow()
    row = conn.execute(
        "SELECT * FROM cerebro_decisions WHERE ativo=? AND data_autorizacao <= ? ORDER BY id_analise DESC LIMIT 1",
        (norm_ticker(ticker), _iso(at)),
    ).fetchone()
    if row is None:
        return None
    cols = [c[0] for c in conn.execute("SELECT * FROM cerebro_decisions LIMIT 0").description]
    return dict(zip(cols, row, strict=True))


def evaluate(dec: dict[str, Any] | None, at: datetime, cfg: CerebroSettings) -> tuple[bool, str]:
    """(authorized, human reason) for a buy at ``at``."""
    if dec is None:
        return False, "Cérebro: sem análise fundamentalista para este ativo"
    exp = dec["data_expiracao"]
    if dec["status"] == "rejeitado":
        return False, f"Cérebro: rejeitado (convicção {dec['conviccao']:.0%})"
    if dec["status"] == "pausa_analise":
        return False, "Cérebro: análise em pausa (dados indisponíveis)"
    if exp <= _iso(at):
        return False, f"Cérebro: autorização vencida em {exp[:16].replace('T', ' ')} UTC"
    if dec["conviccao"] < cfg.min_conviccao:
        return False, f"Cérebro: convicção {dec['conviccao']:.0%} abaixo do mínimo {cfg.min_conviccao:.0%}"
    if not cfg.allow_contingency and str(dec.get("origem_decisao") or "").startswith("Regra de contingência"):
        return False, "Cérebro: autorização pela regra de contingência (IA indisponível) não aceita"
    return True, f"Cérebro: autorizado (convicção {dec['conviccao']:.0%}) até {exp[:10]}"


class CerebroGate:
    """Entry gate injected into the SessionEngine (paper/live; never in backtests: there is no history)."""

    def __init__(self, conn: sqlite3.Connection, cfg: CerebroSettings) -> None:
        self.conn, self.cfg = conn, cfg
        self.advisories: list[str] = []

    def __call__(self, ticker: str, d: date) -> str | None:
        at = session_open_utc(d)
        ok, why = evaluate(latest_decision(self.conn, ticker, at), at, self.cfg)
        if ok:
            return None
        if self.cfg.mode == "advisory":
            self.advisories.append(f"{ticker} {d}: {why} (modo consultivo: a compra seguiu)")
            return None
        return why


def create_webhook_app(settings: Settings, db_path: str | Path | None = None) -> Any:
    """Small FastAPI app that only receives decisions (no other route can change anything)."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from ts.persistence.db import apply_migrations, get_connection

    cfg = settings.cerebro
    path = db_path or settings.system.db_path
    app = FastAPI(title="Trading System B3 — webhook do Cérebro", docs_url=None, redoc_url=None, openapi_url=None)
    allowed = set(cfg.allowed_ips)
    first = threading.Lock()
    migrated = {"done": False}

    def conn() -> sqlite3.Connection:
        c = get_connection(path)
        with first:
            if not migrated["done"]:
                apply_migrations(c)
                migrated["done"] = True
        return c

    last_ping: dict[str, Any] = {}

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ts-cerebro-webhook", "last_ping": last_ping.get("at")}

    @app.post(cfg.webhook_path)
    async def decisao(request: Request) -> JSONResponse:
        host = request.client.host if request.client else ""
        if "*" not in allowed and host not in allowed:
            logger.warning("cerebro webhook: refused %s (not in allowed_ips)", host)
            return JSONResponse({"ok": False, "erro": "origem não autorizada"}, status_code=403)
        try:
            raw = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "erro": "JSON inválido"}, status_code=422)
        if isinstance(raw, dict) and raw.get("tipo") == "ping_teste":
            # "Testar conexão" do Cérebro: responde na hora, não grava nada
            last_ping["at"] = _iso(utcnow())
            logger.info("cerebro ping_teste de %s: %s", raw.get("origem"), str(raw.get("mensagem", ""))[:200])
            return JSONResponse({"status": "ok", "recebido": True})
        try:
            d = Decision.model_validate(raw)
        except (ValidationError, ValueError) as e:
            logger.warning("cerebro webhook: invalid payload: %s", e)
            return JSONResponse({"ok": False, "erro": "payload inválido"}, status_code=422)
        c = conn()
        try:
            new = store_decision(c, d, raw)
        finally:
            c.close()
        logger.info("cerebro decision %s %s %s (%.0f%%) id=%s%s", d.ativo, d.status, d.data_expiracao,
                    d.conviccao * 100, d.id_analise, "" if new else " (duplicada)")
        return JSONResponse({"status": "ok", "recebido": True, "ok": True, "duplicado": not new})

    return app


# ============================================================================================ ① ③ outbox
def _enqueue(conn: sqlite3.Connection, kind: str, key: str, payload: dict[str, Any]) -> bool:
    now = _iso(utcnow())
    cur = conn.execute(
        "INSERT OR IGNORE INTO cerebro_outbox (kind, dedup_key, payload_json, created_at, next_attempt_at) "
        "VALUES (?,?,?,?,?)", (kind, key, json.dumps(payload, ensure_ascii=False), now, now))
    conn.commit()
    return cur.rowcount == 1


def enqueue_demand(conn: sqlite3.Connection, tickers: Iterable[str], priority: str = "alta",
                   key: str | None = None) -> list[str]:
    ativos = sorted({norm_ticker(t) for t in tickers if t})
    if not ativos:
        return []
    payload = {"ativos": ativos, "prioridade": priority, "origem": "robo_tecnico"}
    _enqueue(conn, "demand", key or f"demand:{_iso(utcnow())}:{','.join(ativos)}", payload)
    return ativos


def feedback_payload(t: Any, mode: str) -> dict[str, Any]:
    """Spec section 5 from a CompletedTrade."""
    invested = float(t.entry_price) * int(t.qty)
    pct = round(float(t.net_pnl_brl) / invested * 100.0, 2) if invested else 0.0
    setup = SETUP_PT.get(t.setup, t.setup)
    return {
        "ativo": norm_ticker(t.ticker),
        "resultado": "WIN" if float(t.net_pnl_brl) > 0 else "LOSS",
        "motivo_grafico": f"{EXIT_PT.get(str(t.exit_reason), str(t.exit_reason))} — setup {setup} "
                          f"({float(t.r_multiple):+.2f}R em {int(t.bars_held)} pregões)",
        "lucro_prejuizo_pct": pct,
        "detalhes": {
            "entrada": round(float(t.entry_price), 4), "saida": round(float(t.exit_price), 4), "setup": setup,
            "setup_id": t.setup, "r_multiple": round(float(t.r_multiple), 3), "pregoes": int(t.bars_held),
            "data_entrada": t.entry_date.isoformat(), "data_saida": t.exit_date.isoformat(),
            "motivo_saida": str(t.exit_reason), "modo": mode, "position_id": t.position_id,
        },
    }


def enqueue_feedback(conn: sqlite3.Connection, trade: Any, mode: str) -> bool:
    if getattr(trade, "is_shadow", False):
        return False
    return _enqueue(conn, "feedback", f"feedback:{trade.position_id}", feedback_payload(trade, mode))


def flush_outbox(conn: sqlite3.Connection, cfg: CerebroSettings, budget_s: float = 60.0,
                 client: Any = None) -> dict[str, int]:
    """Send due items (demands first). Never raises: failures are rescheduled with backoff."""
    import httpx

    out = {"sent": 0, "retry": 0, "failed": 0}
    rows = conn.execute(
        "SELECT id, kind, payload_json, attempts FROM cerebro_outbox WHERE status='pending' AND next_attempt_at <= ? "
        "ORDER BY CASE kind WHEN 'demand' THEN 0 ELSE 1 END, id", (_iso(utcnow()),)).fetchall()
    if not rows:
        return out
    own = client is None
    client = client or httpx.Client()
    t0 = time.monotonic()
    try:
        for oid, kind, payload, attempts in rows:
            if time.monotonic() - t0 > budget_s:
                break
            url = cfg.base_url.rstrip("/") + ("/api/queue/demand" if kind == "demand" else "/api/memory/feedback")
            timeout = cfg.demand_timeout_s if kind == "demand" else cfg.feedback_timeout_s
            now = utcnow()
            try:
                r = client.post(url, json=json.loads(payload), timeout=timeout)
                if 200 <= r.status_code < 300:
                    conn.execute("UPDATE cerebro_outbox SET status='sent', attempts=?, last_attempt_at=?, "
                                 "response_json=?, error=NULL WHERE id=?",
                                 (attempts + 1, _iso(now), r.text[:20000], oid))
                    out["sent"] += 1
                    conn.commit()
                    continue
                err = f"HTTP {r.status_code}: {r.text[:300]}"
                permanent = r.status_code == 422  # invalid JSON: resending the same body never works
            except httpx.HTTPError as e:
                err, permanent = f"{type(e).__name__}: {e}", False
            n = attempts + 1
            if permanent or n > len(RETRY_DELAYS_S):
                conn.execute("UPDATE cerebro_outbox SET status='failed', attempts=?, last_attempt_at=?, error=? "
                             "WHERE id=?", (n, _iso(now), err, oid))
                out["failed"] += 1
            else:
                nxt = now + timedelta(seconds=RETRY_DELAYS_S[n - 1])
                conn.execute("UPDATE cerebro_outbox SET attempts=?, last_attempt_at=?, next_attempt_at=?, error=? "
                             "WHERE id=?", (n, _iso(now), _iso(nxt), err, oid))
                out["retry"] += 1
            conn.commit()
            logger.warning("cerebro %s #%s not delivered (%s)", kind, oid, err)
    finally:
        if own:
            client.close()
    return out


def retry_failed(conn: sqlite3.Connection) -> int:
    """Put every failed item back in the queue (e.g. after fixing the Cérebro URL)."""
    cur = conn.execute("UPDATE cerebro_outbox SET status='pending', attempts=0, next_attempt_at=? WHERE status='failed'",
                       (_iso(utcnow()),))
    conn.commit()
    return cur.rowcount


# ============================================================================================ EOD hook
def after_eod(conn: sqlite3.Connection, settings: Settings, closed_trades: list[Any], state: Any,
              next_session: date, budget_s: float = 45.0) -> list[str]:
    """Called at the end of the EOD: feedback of closed trades + demand for tomorrow's buys, then a short flush.

    Returns human-readable notes for the EOD notification. Never raises.
    """
    cfg = settings.cerebro
    notes: list[str] = []
    if not cfg.enabled:
        return notes
    try:
        if cfg.send_feedback:
            n = sum(enqueue_feedback(conn, t, settings.execution.mode) for t in closed_trades)
            if n:
                notes.append(f"{n} resultado(s) de trade enviado(s) ao Cérebro")
        at = session_open_utc(next_session)
        buys = sorted({o.ticker for o in state.pending if not o.is_shadow and o.side == "BUY"})
        need = [t for t in buys if not evaluate(latest_decision(conn, t, at), at, cfg)[0]]
        if need:
            enqueue_demand(conn, need, cfg.demand_priority, key=f"demand:{next_session.isoformat()}:{','.join(need)}")
            notes.append(f"Cérebro: análise pedida para {', '.join(need)} (compras de {next_session:%d/%m} "
                         "só saem se autorizadas)")
        res = flush_outbox(conn, cfg, budget_s)
        if res["retry"] or res["failed"]:
            notes.append(f"Cérebro: {res['retry'] + res['failed']} envio(s) pendente(s) — o serviço do webhook "
                         "reenvia sozinho")
    except Exception as e:  # noqa: BLE001 - the integration must never break the EOD
        logger.exception("cerebro after_eod")
        notes.append(f"Cérebro: falha na integração ({e!r}); o EOD seguiu normalmente")
    return notes


# ============================================================================================ status / config
def status(conn: sqlite3.Connection | None, cfg: CerebroSettings, probe: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"enabled": cfg.enabled, "mode": cfg.mode, "base_url": cfg.base_url,
                           "webhook_url": f"http://{'localhost' if cfg.webhook_host in ('127.0.0.1', '0.0.0.0') else cfg.webhook_host}"
                                          f":{cfg.webhook_port}{cfg.webhook_path}"}
    if conn is not None:
        try:
            r = conn.execute("SELECT MAX(received_at), COUNT(*) FROM cerebro_decisions").fetchone()
            out["last_decision_at"], out["decisions"] = r[0], r[1]
            now = _iso(utcnow())
            out["authorized_now"] = conn.execute(
                "SELECT COUNT(*) FROM (SELECT ativo, status, data_expiracao, MAX(id_analise) FROM cerebro_decisions "
                "WHERE data_autorizacao <= ? GROUP BY ativo) WHERE status='autorizado' AND data_expiracao > ?",
                (now, now)).fetchone()[0]
            q = dict(conn.execute("SELECT status, COUNT(*) FROM cerebro_outbox GROUP BY status").fetchall())
            out["outbox"] = {"pending": q.get("pending", 0), "failed": q.get("failed", 0), "sent": q.get("sent", 0)}
            e = conn.execute("SELECT error, last_attempt_at FROM cerebro_outbox WHERE error IS NOT NULL "
                             "ORDER BY last_attempt_at DESC LIMIT 1").fetchone()
            out["last_error"] = None if e is None else {"error": e[0], "at": e[1]}
        except sqlite3.Error:
            out.update(decisions=0, outbox={"pending": 0, "failed": 0, "sent": 0})
    if probe and cfg.enabled:
        out["listener_alive"] = _alive(cfg)
    return out


def _alive(cfg: CerebroSettings) -> bool:
    import httpx

    host = "127.0.0.1" if cfg.webhook_host in ("0.0.0.0", "") else cfg.webhook_host
    try:
        # 3 s: on a slower PC the webhook answered in ~1,3 s and the old 0,8 s limit showed it as "parado"
        r = httpx.get(f"http://{host}:{cfg.webhook_port}/health", timeout=3.0)
        return r.status_code == 200 and r.json().get("service") == "ts-cerebro-webhook"
    except (httpx.HTTPError, ValueError):
        return False


def write_local_config(config_dir: Path, updates: dict[str, Any]) -> Path:
    """Merge ``updates`` into config/settings.local.yaml under ``cerebro`` (git-ignored, survives updates)."""
    import yaml

    p = config_dir / "settings.local.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    data = data or {}
    data.setdefault("cerebro", {}).update({k: v for k, v in updates.items() if v is not None})
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


# ============================================================================================ service
def serve(settings: Settings, root: Path) -> int:
    """Long-running service (scheduled task TS_Cerebro at logon): webhook ② + outbox flush every minute."""
    import uvicorn

    from ts.app.desktop import ensure_streams
    from ts.persistence.db import DatabaseManager

    ensure_streams(root)
    cfg = settings.cerebro
    if not cfg.enabled:
        print("Integração com o Cérebro desligada (cerebro.enabled=false). Nada a fazer.")
        return 0
    if _alive(cfg):
        print("O serviço do webhook já está rodando.")
        return 0

    stop = threading.Event()

    def outbox_loop() -> None:
        db = DatabaseManager(settings.system.db_path)
        while not stop.wait(60):
            try:
                flush_outbox(db.get_conn(), cfg, budget_s=50)
            except Exception:  # noqa: BLE001
                logger.exception("cerebro outbox loop")

    threading.Thread(target=outbox_loop, daemon=True).start()
    app = create_webhook_app(settings)
    try:
        uvicorn.Server(uvicorn.Config(app, host=cfg.webhook_host, port=cfg.webhook_port, log_level="warning",
                                      access_log=False, log_config=None)).run()
    finally:
        stop.set()
    return 0
