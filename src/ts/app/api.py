"""Local API for the visual terminal (PRD v2 — Terminal Visual, sprint U1).

* Read-only over SQLite (``mode=ro``) and the Parquet store; never writes trading state.
* Actions (EOD, sync, update) run the existing CLI in a separate process, so they take the same
  single-instance lock as the scheduled routines.
* Binds to 127.0.0.1 only. State-changing endpoints require the ``X-TS-Token`` header with a per-run
  random token that is injected into the page: a malicious web page cannot send that custom header
  cross-origin (no CORS is enabled), which blocks drive-by "CSRF" calls to localhost.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ts.core.calendar import B3Calendar
from ts.core.config import Settings, load_settings
from ts.data.ohlcv_store import OhlcvStore
from ts.indicators.ta import atr, diamond_phase, efficiency_ratio, ema, sma
from ts.strategy.router import PHASE_NAMES

WEB_DIR = Path(__file__).resolve().parent / "web"

DISPOSITION_PT = {
    "ACCEPTED": "Aceito", "SHADOW": "Carteira sombra (circuit breaker)", "REJECTED_HEAT": "Heat da carteira no limite",
    "REJECTED_CLUSTER": "Cluster de correlação cheio", "REJECTED_CORR": "Risco correlacionado acima do limite",
    "REJECTED_BETA": "Beta da carteira acima do limite", "REJECTED_REGIME": "Regime de volatilidade bloqueia entradas",
    "REJECTED_COOLDOWN": "Ativo em cooldown após stops", "REJECTED_SETUP": "Setup suspenso pelo monitor",
    "REJECTED_SIZE": "Stop/tamanho inválido", "REJECTED_RR": "Retorno/risco abaixo de 3", "REJECTED_DUPLICATE": "Já posicionado",
}
SETUP_PT = {"A": "Pivô de alta", "B": "Pullback", "C": "Donchian 20/10", "D": "Bollinger squeeze", "E": "Divergência",
            "F": "Reversão na baixa", "G": "Força mensal + MACD"}
STATE_PT = {"PLANNED": "Planejada", "SUBMITTED": "Enviada", "ACCEPTED": "Aceita", "PARTIALLY_FILLED": "Parcial",
            "FILLED": "Executada", "CANCELLED": "Cancelada", "EXPIRED": "Expirada", "REJECTED": "Rejeitada",
            "UNKNOWN": "Desconhecida"}
EXIT_PT = {"STOP": "no stop", "TARGET": "no alvo", "TRAILING": "no stop móvel", "TIME": "por tempo",
           "STAGNATION": "por estagnação", "BREAKEVEN": "no zero a zero", "MANUAL": "manual",
           "CIRCUIT_BREAKER": "pelo circuit breaker"}
KIND_PT = {"OPEN_AUCTION": "Leilão de abertura", "BUY_STOP": "Stop de compra", "EXIT_OPEN": "Saída na abertura"}
ROUTINE_PT = {"eod": "Fim do dia (EOD)", "preopen": "Pré-abertura", "watchdog": "Watchdog", "sync": "Cotações",
              "backup": "Backup", "deadman": "Dead-man", "backtest": "Backtest", "weekly": "Relatório semanal"}


def _clean(v: Any) -> Any:
    if isinstance(v, float) and not np.isfinite(v):
        return None
    if isinstance(v, np.floating):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, date | datetime):
        return v.isoformat()
    return v


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


@dataclass
class Job:
    id: str
    kind: str
    started: float
    status: str = "running"  # running | done
    code: int | None = None
    message: str = ""
    output: str = ""


@dataclass
class TerminalContext:
    root: Path
    settings: Settings
    calendar: B3Calendar
    token: str
    dd_level1: float = 0.08
    jobs: dict[str, Job] = field(default_factory=dict)
    _cache: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ paths & db
    def path(self, p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else self.root / p

    @property
    def store(self) -> OhlcvStore:
        return OhlcvStore(self.path(self.settings.system.data_dir) / "ohlcv" / "d1")

    def query(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        db = self.path(self.settings.system.db_path)
        if not db.exists():
            return pd.DataFrame()
        with closing(sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, check_same_thread=False)) as conn:
            try:
                return pd.read_sql_query(sql, conn, params=params)
            except Exception:  # noqa: BLE001 - table may not exist yet (fresh install)
                return pd.DataFrame()

    def cached(self, key: str, stamp: Any, fn: Any) -> Any:
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] == stamp:
                return hit[1]
        value = fn()
        with self._lock:
            self._cache[key] = (stamp, value)
        return value

    def data_stamp(self) -> tuple[float, float]:
        d = self.store.data_dir
        files = list(d.glob("*.parquet")) if d.exists() else []
        db = self.path(self.settings.system.db_path)
        wal = db.with_name(db.name + "-wal")  # WAL mode: new rows (e.g. Cérebro webhooks) land here first
        return (max((f.stat().st_mtime for f in files), default=0.0),
                max(db.stat().st_mtime if db.exists() else 0.0, wal.stat().st_mtime if wal.exists() else 0.0))

    # ------------------------------------------------------------------ calendar helpers
    def last_closed_session(self, now: datetime | None = None) -> date:
        now = now or datetime.now()
        today = now.date()
        cal = self.calendar
        if cal.is_session(today) and now >= datetime.combine(today, cal.close_time(today)) + timedelta(minutes=45):
            return today
        return cal.previous_session(today)

    def next_event(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now()
        cal = self.calendar
        d = now.date()
        for _ in range(15):
            if cal.is_session(d):
                for label, hhmm in (("Pré-abertura", "08:45"), ("Fim do dia (EOD)", "19:30")):
                    at = datetime.combine(d, datetime.strptime(hhmm, "%H:%M").time())
                    if at > now:
                        return {"label": label, "at": at.isoformat(timespec="minutes")}
            d += timedelta(days=1)
        return {"label": "—", "at": None}


# ====================================================================================== builders
def build_status(ctx: TerminalContext, now: datetime | None = None) -> dict[str, Any]:
    s = ctx.settings
    store = ctx.store
    ibov = s.mt5.ibov_symbol.upper()
    last_bar = store.get_latest_session_date(ibov) if store.exists(ibov) else None
    last_closed = ctx.last_closed_session(now)
    eod = ctx.query("SELECT session_date, status, finished_at, error FROM runs WHERE routine='eod' "
                    "ORDER BY started_at DESC LIMIT 1")
    wd = ctx.query("SELECT status, finished_at, error FROM runs WHERE routine='watchdog' ORDER BY started_at DESC LIMIT 1")
    eq = ctx.query("SELECT session_date, equity, drawdown_pct, heat_pct FROM equity_daily WHERE curve='PAPER' "
                   "ORDER BY session_date DESC LIMIT 1")
    snap = ctx.query("SELECT session_date, state_json FROM book_snapshots ORDER BY session_date DESC LIMIT 1")
    last_processed = None if snap.empty else snap["session_date"].iloc[0]
    cb_level, cb_dd = 0, 0.0
    quarantine = {"active": False, "reason": None, "loss_streak": 0}
    if not snap.empty:
        try:
            br = json.loads(snap["state_json"].iloc[0]).get("breaker", {})
            quarantine = {"active": bool(br.get("halted")), "reason": br.get("halt_reason"),
                          "loss_streak": int(br.get("loss_streak", 0))}
            cb_dd = float(br.get("dd", 0.0))
            cb_level = 2 if br.get("halted") else (1 if cb_dd >= ctx.dd_level1 else 0)
        except (ValueError, TypeError):
            pass
    connection: dict[str, Any]
    if s.data.provider == "yahoo":
        connection = {"kind": "yahoo", "label": "Yahoo Finance", "detail": "cotações com ~15 min de atraso; decisões usam a barra diária fechada"}
    else:
        ok = (not wd.empty) and wd["status"].iloc[0] == "OK"
        connection = {"kind": "mt5", "label": "MT5 " + ("conectado" if ok else "verificar"),
                      "detail": (wd["error"].iloc[0] if not wd.empty and wd["error"].iloc[0] else "último watchdog")}
    return {
        "mode": s.execution.mode,
        "provider": s.data.provider,
        "data": {
            "last_bar": _clean(last_bar), "last_closed_session": last_closed.isoformat(),
            "stale": last_bar is None or last_bar < last_closed,
        },
        "last_eod": None if eod.empty else {k: _clean(v) for k, v in eod.iloc[0].items()},
        "next_event": ctx.next_event(now),
        "connection": connection,
        "paper_started": not eq.empty,
        "last_processed_session": last_processed,
        # True when a closed session still has to go through the EOD (the "run now" button only makes sense then)
        "eod_pending": last_processed is None or last_processed < last_closed.isoformat(),
        "equity": None if eq.empty else {k: _clean(v) for k, v in eq.iloc[0].items()},
        "circuit_breaker": {"level": cb_level, "drawdown": cb_dd},
        "quarantine": quarantine,
        "candidate": _latest_candidate(ctx),
        "override": _override_status(ctx),
        "breadth": _breadth_status(ctx),
        "reference_capital": s.execution.reference_capital,
        "version": _version_info(ctx),
        "g0": g0_status(ctx, (now or datetime.now()).date()),
        "server": server_freshness(ctx),
        "cerebro": cerebro_status(ctx),
        "server_time": (now or datetime.now()).isoformat(timespec="seconds"),
    }


def _ro_conn(ctx: TerminalContext) -> sqlite3.Connection | None:
    db = ctx.path(ctx.settings.system.db_path)
    if not db.exists():
        return None
    return sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, check_same_thread=False)


def cerebro_status(ctx: TerminalContext) -> dict[str, Any]:
    from ts.app import cerebro as cb

    cfg = ctx.settings.cerebro
    if not cfg.enabled:
        return {"enabled": False}
    c = _ro_conn(ctx)
    try:
        return cb.status(c, cfg)
    finally:
        if c is not None:
            c.close()


def cerebro_decisions_now(ctx: TerminalContext) -> dict[str, dict[str, Any]]:
    """Latest decision per ticker known now, with whether it authorizes a buy at the next open."""
    from ts.app import cerebro as cb

    cfg = ctx.settings.cerebro
    if not cfg.enabled:
        return {}
    now = cb.utcnow()
    df = ctx.query(
        "SELECT id_analise, ativo, status, conviccao, data_expiracao, data_autorizacao, origem_decisao, tese, "
        "indicadores_json, fontes, validade_dias FROM cerebro_decisions d WHERE id_analise = "
        "(SELECT MAX(id_analise) FROM cerebro_decisions WHERE ativo=d.ativo AND data_autorizacao <= ?)",
        (cb._iso(now),))
    nxt = ctx.calendar.next_session(ctx.last_closed_session())
    at = cb.session_open_utc(nxt)
    out = {}
    for r in df.to_dict(orient="records") if not df.empty else []:
        ok, why = cb.evaluate(r, at, cfg)
        out[r["ativo"]] = {
            "status": r["status"], "conviccao": r["conviccao"], "expira": r["data_expiracao"],
            "autorizado": r["data_autorizacao"], "origem": r["origem_decisao"], "tese": r["tese"],
            "fontes": r["fontes"], "indicadores": json.loads(r["indicadores_json"] or "{}"),
            "libera_compra": ok, "motivo": why, "proximo_pregao": nxt.isoformat(), "id": r["id_analise"],
        }
    return out


G0_EVERY_DAYS = 90


def g0_status(ctx: TerminalContext, today: date) -> dict[str, Any]:
    """Latest G0 report vs the configuration in force: is a new run due (config changed, or >= 90 days)?"""
    try:
        from ts.core.config import load_strategy_config

        _, cfg_hash = load_strategy_config(ctx.path("config/strategy.yaml"))
    except Exception:  # noqa: BLE001 - invalid config is reported by the override banner
        cfg_hash = ""
    rdir = ctx.path(ctx.settings.system.reports_dir)
    reports = []
    for f in rdir.glob("G0_*.md") if rdir.exists() else []:
        parts = f.stem.split("_")  # G0_<yyyy-mm-dd>_<hash>
        try:
            reports.append((date.fromisoformat(parts[1]), parts[2] if len(parts) > 2 else "", f))
        except (ValueError, IndexError):
            continue
    same = sorted((r for r in reports if cfg_hash and cfg_hash.startswith(r[1])), key=lambda r: r[0])
    last = same[-1] if same else None
    out: dict[str, Any] = {"config_hash": cfg_hash[:16], "every_days": G0_EVERY_DAYS, "last": None}
    if last is None:
        out.update(due=True, reason="Ainda não há relatório G0 para a configuração atual (as regras mudaram).")
        return out
    head = last[2].read_text(encoding="utf-8", errors="replace")[:1500]
    verdict = "APROVADO" if "APROVADO (GO)" in head or "Resultado Global do Portão G0:** APROVADO" in head else (
        "REPROVADO" if "REPROVADO" in head else "?")
    age = (today - last[0]).days
    out["last"] = {"date": last[0].isoformat(), "verdict": verdict, "file": str(last[2]), "age_days": age}
    out["next_due"] = (last[0] + timedelta(days=G0_EVERY_DAYS)).isoformat()
    out["due"] = age >= G0_EVERY_DAYS
    out["reason"] = (f"O último G0 foi há {age} dias (rodar a cada {G0_EVERY_DAYS})." if out["due"] else "")
    return out


def _latest_candidate(ctx: TerminalContext) -> dict[str, Any] | None:
    df = ctx.query("SELECT candidate_id, created_at, status, trigger_reason, market_regime, params_json, summary_json "
                   "FROM config_candidates WHERE status IN ('RUNNING', 'PENDING') ORDER BY created_at DESC LIMIT 1")
    if df.empty:
        return None
    r = df.iloc[0]
    return {"id": r["candidate_id"], "status": r["status"], "created_at": r["created_at"], "reason": r["trigger_reason"],
            "regime": r["market_regime"], "params": json.loads(r["params_json"] or "{}"),
            "summary": json.loads(r["summary_json"] or "{}")}


def _breadth_status(ctx: TerminalContext) -> dict[str, Any] | None:
    """Share of the universe above its SMA50 (from the Market Watch cache) + router on/off."""
    with ctx._lock:
        hit = ctx._cache.get("watchlist")
    if not hit:
        return None
    flags = [r["above_sma50"] for r in hit[1] if not r.get("is_index") and r.get("above_sma50") is not None]
    enabled = False
    try:
        from ts.core.config import load_strategy_config

        enabled = load_strategy_config(ctx.root / "config" / "strategy.yaml")[0].regime_router.enabled
    except Exception:  # noqa: BLE001
        pass
    return {"pct_above_sma50": (sum(flags) / len(flags)) if flags else None, "n": len(flags), "router_enabled": enabled}


def _override_status(ctx: TerminalContext) -> dict[str, Any]:
    try:
        from ts.core.config import load_effective_strategy

        _, h, eff = load_effective_strategy(ctx.root / "config" / "strategy.yaml")
        return {"status": eff.override_status, "hash": h,
                "candidate_id": (eff.override or {}).get("candidate_id"), "error": None}
    except Exception as e:  # noqa: BLE001 - shown in the UI instead of breaking the status
        return {"status": "error", "hash": None, "candidate_id": None, "error": str(e)}


def build_candidates(ctx: TerminalContext) -> list[dict[str, Any]]:
    df = ctx.query("SELECT candidate_id, created_at, status, trigger_reason, market_regime, params_json, summary_json, "
                   "report_path, decided_at, decision_note FROM config_candidates ORDER BY created_at DESC LIMIT 20")
    out = []
    for r in df.itertuples():
        report = None
        if r.status == "PENDING" and r.report_path:
            p = ctx.path(r.report_path)
            report = p.read_text(encoding="utf-8") if p.exists() else None
        out.append({"id": r.candidate_id, "created_at": r.created_at, "status": r.status, "reason": r.trigger_reason,
                    "regime": r.market_regime, "params": json.loads(r.params_json or "{}"),
                    "summary": json.loads(r.summary_json or "{}"), "report": report,
                    "decided_at": r.decided_at, "note": r.decision_note})
    return out


def _version_info(ctx: TerminalContext) -> dict[str, Any]:
    """git fetch can take seconds: refreshed in a background thread every 5 minutes."""
    with ctx._lock:
        cached = ctx._cache.get("version")
    fresh = cached is not None and time.time() - cached[0] < 300
    if not fresh and not getattr(ctx, "_version_running", False):
        ctx._version_running = True  # type: ignore[attr-defined]

        def work() -> None:
            try:
                _store_version(ctx, _check_version_now(ctx))
            finally:
                ctx._version_running = False  # type: ignore[attr-defined]

        threading.Thread(target=work, daemon=True).start()
    return cached[1] if cached else {"checking": True, "update_available": False}


def _head_commit(root: Path) -> str | None:
    """Commit currently on disk (cheap: reads .git, no subprocess)."""
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = root / ".git" / head[5:]
            if ref.exists():
                return ref.read_text(encoding="utf-8").strip()
            packed = root / ".git" / "packed-refs"
            for line in packed.read_text(encoding="utf-8").splitlines() if packed.exists() else []:
                if line.endswith(" " + head[5:]):
                    return line.split()[0]
            return None
        return head
    except OSError:
        return None


def server_freshness(ctx: TerminalContext) -> dict[str, Any]:
    """Was a new version installed (e.g. by the automatic update) after this terminal started?"""
    started = getattr(ctx, "started_commit", None)
    disk = _head_commit(ctx.root)
    return {"started": started, "disk": disk, "stale": bool(started and disk and started != disk)}


def restart_terminal(root: Path) -> None:
    """Start a fresh terminal (it waits for this one to release the port) and exit this process."""
    pyw = root / ".venv" / "Scripts" / "pythonw.exe"
    exe = str(pyw) if pyw.exists() else sys.executable
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([exe, "-m", "ts", "ui", "--restart"], cwd=root, creationflags=flags, close_fds=True)  # noqa: S603
    threading.Timer(1.0, lambda: os._exit(0)).start()  # let the HTTP response go out first


def _check_version_now(ctx: TerminalContext) -> dict[str, Any]:
    from ts.app.version import check_version

    try:
        v = check_version(ctx.root)
        info = {"installed": v.installed, "installed_date": v.installed_date, "available": v.available,
                "update_available": v.update_available, "pending": v.pending, "error": v.error,
                "modified": v.modified or [], "data_files": v.data_files or []}
    except Exception as e:  # noqa: BLE001
        info = {"error": str(e), "update_available": False}
    info["checked_at"] = datetime.now().isoformat(timespec="seconds")
    return info


def _store_version(ctx: TerminalContext, info: dict[str, Any]) -> None:
    with ctx._lock:
        ctx._cache["version"] = (time.time(), info)


def build_watchlist(ctx: TerminalContext) -> list[dict[str, Any]]:
    def compute() -> list[dict[str, Any]]:
        from ts.data.universe import universe_on

        store = ctx.store
        ibov = ctx.settings.mt5.ibov_symbol.upper()
        try:
            tickers = [ibov] + [t for t in universe_on(date.today(), ctx.path("config/universe")) if t != ibov]
        except FileNotFoundError:
            tickers = [ibov] + sorted(p.stem for p in store.data_dir.glob("*.parquet") if p.stem != ibov)
        last_session = ctx.last_closed_session()
        sig = ctx.query("SELECT ticker, setup, disposition, reason, decision_date FROM signals "
                        "WHERE decision_date = (SELECT MAX(decision_date) FROM signals)")
        pos = ctx.query("SELECT ticker FROM positions WHERE status IN ('OPEN','PARTIAL') AND is_shadow=0")
        held = set(pos["ticker"]) if not pos.empty else set()
        brain = cerebro_decisions_now(ctx)
        rows: list[dict[str, Any]] = []
        for t in tickers:
            if not store.exists(t):
                rows.append({"ticker": t, "is_index": t == ibov, "has_data": False})
                continue
            df = store.read(t).tail(260)
            if df.empty:
                continue
            c, h, lo = df["close"].astype(float), df["high"].astype(float), df["low"].astype(float)
            last, prev = float(c.iloc[-1]), float(c.iloc[-2]) if len(c) > 1 else float("nan")
            s200 = float(sma(c, 200).iloc[-1]) if len(c) >= 200 else float("nan")
            s50 = float(sma(c, 50).iloc[-1]) if len(c) >= 50 else float("nan")
            a14 = float(atr(h, lo, c, 14).iloc[-1])
            phase = float(diamond_phase(pd.Series([last]), pd.Series([s50]), pd.Series([s200])).iloc[0]) \
                if s200 == s200 and s50 == s50 else float("nan")
            er = float(efficiency_ratio(c, 10).iloc[-1])
            tsig = sig[sig["ticker"] == t] if not sig.empty else sig
            accepted = (not tsig.empty) and (tsig["disposition"] == "ACCEPTED").any()
            rows.append({
                "ticker": t, "is_index": t == ibov, "has_data": True,
                "last": last, "change_pct": (last / prev - 1) if prev == prev and prev else None,
                "trend": None if s200 != s200 else ("up" if last > s200 else "down"),
                "atr_pct": a14 / last if last else None,
                "above_sma50": None if s50 != s50 else bool(last > s50),
                "phase": None if phase != phase else int(phase),
                "phase_name": None if phase != phase else PHASE_NAMES.get(int(phase)),
                "er": None if er != er else er,
                "last_date": df["session_date"].iloc[-1].isoformat(),
                "stale": df["session_date"].iloc[-1] < last_session,
                "signal": None if tsig.empty else ("accepted" if accepted else "rejected"),
                "signals": [] if tsig.empty else [
                    {"setup": r.setup, "setup_name": SETUP_PT.get(r.setup, r.setup), "disposition": r.disposition,
                     "result": DISPOSITION_PT.get(r.disposition, r.disposition), "reason": r.reason,
                     "date": r.decision_date} for r in tsig.itertuples()],
                "held": t in held,
                "cerebro": None if t not in brain else {k: brain[t][k] for k in
                                                        ("status", "conviccao", "expira", "libera_compra", "motivo")},
            })
        return [{k: _clean(v) for k, v in r.items()} for r in rows]

    return ctx.cached("watchlist", ctx.data_stamp(), compute)  # type: ignore[no-any-return]


PERIODS = {"3M": 63, "6M": 126, "1A": 252, "2A": 504, "5A": 1260, "TUDO": 100_000}


def build_chart(ctx: TerminalContext, ticker: str, period: str = "1A") -> dict[str, Any]:
    ticker = ticker.upper()
    store = ctx.store
    if not store.exists(ticker):
        raise HTTPException(404, f"Sem cotações para {ticker}")

    def compute() -> dict[str, Any]:
        from ts.data.corporate_actions import CorporateActionsStore, to_signal_frame

        df = store.read(ticker)
        corp = CorporateActionsStore(ctx.path(ctx.settings.system.corporate_actions_path))
        f = to_signal_frame(corp.apply_adjustment_factors(ticker, df)).reset_index(drop=True)
        c = f["close"]
        f["sma200"], f["sma50"], f["ema20"] = sma(c, 200), sma(c, 50), ema(c, 20)
        return {"frame": f}

    full = ctx.cached(f"chart:{ticker}", ctx.data_stamp(), compute)["frame"]
    v = full.tail(PERIODS.get(period.upper(), 252))
    ts = [d.isoformat() for d in v["session_date"]]

    def line(col: str) -> list[dict[str, Any]]:
        return [{"time": t, "value": round(float(x), 4)} for t, x in zip(ts, v[col], strict=True) if x == x]

    candles = [{"time": t, "open": round(float(o), 4), "high": round(float(h), 4), "low": round(float(lo), 4),
                "close": round(float(cl), 4)}
               for t, o, h, lo, cl in zip(ts, v["open"], v["high"], v["low"], v["close"], strict=True)]
    volume = [{"time": t, "value": float(x), "up": bool(cl >= o)}
              for t, x, o, cl in zip(ts, v["real_volume"], v["open"], v["close"], strict=True)]
    sig = ctx.query("SELECT decision_date, setup, disposition, reason, entry_ref FROM signals WHERE ticker=? "
                    "AND decision_date >= ? ORDER BY decision_date", (ticker, ts[0] if ts else "9999"))
    pos = ctx.query("SELECT opened_on, closed_on, status, entry_price, current_stop, target_price, exit_reason, "
                    "r_multiple, setup FROM positions WHERE ticker=? AND is_shadow=0 AND opened_on >= ? ORDER BY opened_on",
                    (ticker, ts[0] if ts else "9999"))
    trades, position = [], None
    for r in pos.itertuples() if not pos.empty else []:
        trades.append({"time": r.opened_on[:10], "kind": "entry", "text": f"Compra ({SETUP_PT.get(r.setup, r.setup)})"})
        if r.closed_on:
            rm = _clean(r.r_multiple)
            trades.append({"time": r.closed_on[:10], "kind": "exit", "win": (rm or 0) > 0,
                           "text": f"Saída {EXIT_PT.get(r.exit_reason, r.exit_reason or '')}"
                                   + (f" {rm:+.2f}R" if rm is not None else "")})
        if r.status in ("OPEN", "PARTIAL"):
            # the latest adjusted bar has factor 1, so raw position prices line up with the chart's last bars
            position = {"entry": float(r.entry_price), "stop": float(r.current_stop), "target": _clean(r.target_price)}
    return {
        "ticker": ticker,
        "candles": candles,
        "volume": volume,
        "lines": {"sma200": line("sma200"), "sma50": line("sma50"), "ema20": line("ema20")},
        "signals": [{"time": r.decision_date, "setup": r.setup, "accepted": r.disposition == "ACCEPTED",
                     "text": f"{SETUP_PT.get(r.setup, r.setup)} — {DISPOSITION_PT.get(r.disposition, r.disposition)}"
                             + (f": {r.reason}" if r.reason else "")}
                    for r in sig.itertuples()] if not sig.empty else [],
        "trades": [t for t in trades if t["time"] in set(ts)],
        "position": position,
        "adjusted": True,
    }


def build_checklist_api(ctx: TerminalContext, ticker: str) -> dict[str, Any]:
    ticker = ticker.upper()
    if not ctx.store.exists(ticker):
        raise HTTPException(404, f"Sem cotações para {ticker}")

    def compute() -> dict[str, Any]:
        from ts.app.checklist import checklist_for
        from ts.core.config import load_strategy_config

        cfg, _ = load_strategy_config(ctx.path("config/strategy.yaml"))
        return checklist_for(ctx.root, ctx.store, ctx.path(ctx.settings.system.corporate_actions_path), ticker,
                             ctx.settings.mt5.ibov_symbol.upper(), cfg)

    try:
        return ctx.cached(f"checklist:{ticker}", ctx.data_stamp(), compute)  # type: ignore[no-any-return]
    except KeyError as e:
        raise HTTPException(404, f"Sem cotações para {ticker}") from e


def build_positions(ctx: TerminalContext) -> list[dict[str, Any]]:
    pos = ctx.query(
        "SELECT ticker, setup, opened_on, entry_price, current_stop, target_price, qty_open, risk_per_share, "
        "bars_held FROM positions WHERE status IN ('OPEN','PARTIAL') AND is_shadow=0 ORDER BY opened_on DESC")
    if pos.empty:
        return []
    eq = ctx.query("SELECT equity FROM equity_daily WHERE curve='PAPER' ORDER BY session_date DESC LIMIT 1")
    equity = float(eq["equity"].iloc[0]) if not eq.empty else ctx.settings.execution.reference_capital
    store = ctx.store
    out = []
    for r in pos.itertuples():
        last = float(store.read(r.ticker)["close"].iloc[-1]) if store.exists(r.ticker) else float(r.entry_price)
        rps = float(r.risk_per_share or 0) or float("nan")
        out.append({
            "ticker": r.ticker, "setup": r.setup, "setup_name": SETUP_PT.get(r.setup, r.setup), "opened_on": r.opened_on,
            "qty": int(r.qty_open), "entry_price": float(r.entry_price), "last": last,
            "stop": float(r.current_stop), "target": _clean(r.target_price),
            "pnl_brl": (last - float(r.entry_price)) * int(r.qty_open),
            "pnl_r": _clean((last - float(r.entry_price)) / rps),
            "exposure_pct": last * int(r.qty_open) / equity if equity else None,
            "open_risk_pct": max(0.0, (last - float(r.current_stop)) * int(r.qty_open)) / equity if equity else None,
            "bars_held": _clean(r.bars_held),
        })
    return [{k: _clean(v) for k, v in o.items()} for o in out]


def build_orders(ctx: TerminalContext) -> list[dict[str, Any]]:
    df = ctx.query(
        "SELECT oi.client_order_id, oi.ticker, oi.side, oi.qty, oi.entry_kind, oi.price, oi.stop_loss, oi.valid_until, "
        "oi.reason, oi.setup, (SELECT state FROM order_events e WHERE e.client_order_id = oi.client_order_id "
        "ORDER BY event_id DESC LIMIT 1) AS state FROM order_intents oi WHERE oi.is_shadow = 0 "
        "ORDER BY oi.created_at DESC LIMIT 200")
    if df.empty:
        return []
    df["active"] = df["state"].isin(["PLANNED", "SUBMITTED", "ACCEPTED"])
    df["state_pt"] = df["state"].map(STATE_PT).fillna(df["state"])
    df["kind_pt"] = df["entry_kind"].map(KIND_PT).fillna(df["entry_kind"])
    df["valid_until"] = df["valid_until"].where(df["valid_until"] != "9999-12-31", "até executar")
    return _records(df)


def build_signals(ctx: TerminalContext, day: str | None = None) -> dict[str, Any]:
    if day is None:
        last = ctx.query("SELECT MAX(decision_date) AS d FROM signals")
        day = None if last.empty else last["d"].iloc[0]
    if not day:
        return {"date": None, "items": []}
    df = ctx.query("SELECT ticker, setup, entry_type, entry_ref, stop_price, target_price, disposition, reason "
                   "FROM signals WHERE decision_date=? ORDER BY disposition != 'ACCEPTED', ticker", (day,))
    if not df.empty:
        df["setup_name"] = df["setup"].map(SETUP_PT).fillna(df["setup"])
        df["result"] = df["disposition"].map(DISPOSITION_PT).fillna(df["disposition"])
        df["entry_kind"] = df["entry_type"].map(KIND_PT).fillna(df["entry_type"])
    return {"date": day, "items": _records(df)}


def build_equity(ctx: TerminalContext) -> dict[str, Any]:
    df = ctx.query("SELECT session_date, curve, equity, cash, heat_pct, drawdown_pct FROM equity_daily "
                   "WHERE curve IN ('PAPER', 'SHADOW') ORDER BY session_date")
    if df.empty:
        return {"paper": [], "shadow": [], "summary": None}
    paper = df[df["curve"] == "PAPER"]
    shadow = df[df["curve"] == "SHADOW"]
    cap = ctx.settings.execution.reference_capital
    last = paper.iloc[-1]
    peak = float(paper["equity"].max())
    trades = ctx.query("SELECT pnl_brl, r_multiple FROM positions WHERE status='CLOSED' AND is_shadow=0")
    summary = {
        "equity": float(last["equity"]), "cash": float(last["cash"]), "return_pct": float(last["equity"]) / cap - 1,
        "drawdown_pct": float(last["drawdown_pct"]), "max_drawdown_pct": float(paper["drawdown_pct"].max()),
        "heat_pct": float(last["heat_pct"]), "peak": peak, "sessions": int(len(paper)),
        "start": paper["session_date"].iloc[0], "last": paper["session_date"].iloc[-1],
        "trades": int(len(trades)), "win_rate": float((trades["pnl_brl"] > 0).mean()) if len(trades) else None,
        "expectancy_r": float(trades["r_multiple"].mean()) if len(trades) else None,
    }
    return {
        "paper": [{"time": r.session_date, "value": float(r.equity), "dd": float(r.drawdown_pct)} for r in paper.itertuples()],
        "shadow": [{"time": r.session_date, "value": float(r.equity)} for r in shadow.itertuples()],
        "summary": {k: _clean(v) for k, v in summary.items()},
    }


def build_runs(ctx: TerminalContext) -> list[dict[str, Any]]:
    df = ctx.query("SELECT routine, session_date, started_at, finished_at, status, error FROM runs "
                   "ORDER BY started_at DESC LIMIT 100")
    if df.empty:
        return []
    df["routine_pt"] = df["routine"].map(ROUTINE_PT).fillna(df["routine"])
    return _records(df)


# ====================================================================================== actions
ACTIONS = {"eod", "sync", "update", "approve", "reject", "g0"}


def start_job(ctx: TerminalContext, kind: str, arg: str | None = None) -> Job:
    with ctx._lock:
        if any(j.status == "running" for j in ctx.jobs.values()):
            raise HTTPException(409, "Já existe uma ação em andamento. Aguarde terminar.")
        job = Job(id=uuid.uuid4().hex[:8], kind=kind, started=time.time())
        ctx.jobs[job.id] = job

    def work() -> None:
        try:
            if kind == "update":
                from ts.app.version import run_update

                code, msg = run_update(ctx.root)
                job.code, job.message = code, msg
            else:
                if kind == "eod":
                    args = ["eod", "--asof", ctx.last_closed_session().isoformat()]
                elif kind in ("approve", "reject"):
                    args = ["config", kind, arg or "", "--note", "via terminal"]
                elif kind == "g0":
                    args = ["backtest", "--g0"]
                else:
                    args = ["sync"]
                env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
                proc = subprocess.run([sys.executable, "-m", "ts", *args], cwd=ctx.root, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=3600, env=env,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                job.code = proc.returncode
                job.output = (proc.stdout + proc.stderr)[-4000:]
                if kind == "g0":
                    tail = job.output.strip().splitlines()[-1:] or [""]
                    job.message = ("G0 APROVADO" if "APROVADO" in tail[0] and "REPROVADO" not in tail[0]
                                   else "G0 REPROVADO" if "REPROVADO" in tail[0] else "G0 terminou") + \
                        " — relatório na pasta reports."
                    return
                job.message = {0: "Concluído.", 3: "Erro: veja o Diário.", 4: "Dados insuficientes (falha segura).",
                               5: "Outra rotina está rodando; tente em alguns minutos."}.get(proc.returncode,
                                                                                            f"Código {proc.returncode}")
        except Exception as e:  # noqa: BLE001
            job.code, job.message = -1, f"Falha ao executar: {e}"
        finally:
            job.status = "done"
            with ctx._lock:
                ctx._cache.pop("watchlist", None)

    threading.Thread(target=work, daemon=True).start()
    return job


# ====================================================================================== app
def create_app(root: Path | None = None, settings: Settings | None = None, token: str | None = None) -> FastAPI:
    root = (root or Path.cwd()).resolve()
    ctx = TerminalContext(
        root=root,
        settings=settings or load_settings(root / "config" / "settings.yaml"),
        calendar=B3Calendar(root / "config" / "b3_calendar.yaml") if (root / "config" / "b3_calendar.yaml").exists()
        else B3Calendar(),
        token=token or secrets.token_urlsafe(24),
    )
    ctx.started_commit = _head_commit(root)  # type: ignore[attr-defined]  # code this server process is running
    try:
        from ts.core.config import load_strategy_config

        cfg, _ = load_strategy_config(root / "config" / "strategy.yaml")
        ctx.dd_level1 = cfg.protections.circuit_breaker_dd_level1
    except Exception:  # noqa: BLE001 - status still works with the default threshold
        pass
    app = FastAPI(title="Trading System B3 — Terminal", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    def check_token(x_ts_token: str | None) -> None:
        if not x_ts_token or not secrets.compare_digest(x_ts_token, ctx.token):
            raise HTTPException(403, "Token inválido")

    @app.middleware("http")
    async def no_stale_assets(request: Any, call_next: Any) -> Any:
        # The desktop window (WebView2) caches heuristically: after an update it could keep the old app.js/css.
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        for asset in ("app.js", "app.css"):  # versioned URLs: a new file always means a new URL
            v = int((WEB_DIR / asset).stat().st_mtime)
            html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={v}\"")
        return HTMLResponse(html.replace("__TS_TOKEN__", ctx.token), headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return build_status(ctx)

    @app.get("/api/watchlist")
    def watchlist() -> list[dict[str, Any]]:
        return build_watchlist(ctx)

    @app.get("/api/chart/{ticker}")
    def chart(ticker: str, period: str = "1A") -> dict[str, Any]:
        return build_chart(ctx, ticker, period)

    # ---------------------------------------------------------------- Central de Serviços
    def _reload_settings() -> None:
        ctx.settings = load_settings(root / "config" / "settings.yaml")  # picks up settings.local.yaml
        with ctx._lock:
            ctx._cache.pop("watchlist", None)

    @app.get("/api/services")
    def services_overview() -> dict[str, Any]:
        from ts.app import services as sv

        s = ctx.settings
        last_runs = ctx.query(
            "SELECT routine, session_date, started_at, status, error FROM runs r WHERE started_at = "
            "(SELECT MAX(started_at) FROM runs WHERE routine = r.routine) ORDER BY routine")
        return {
            "tasks": sv.tasks_status(),
            "cerebro": cerebro_status(ctx) | {"config": s.cerebro.model_dump()},
            "data_provider": s.data.provider, "telegram_enabled": s.telegram.enabled, "mode": s.execution.mode,
            "root": str(root), "local_config": str(root / "config" / "settings.local.yaml"),
            "last_runs": _records(last_runs) if not last_runs.empty else [],
        }

    @app.post("/api/services/task")
    def services_task(name: str, action: str, x_ts_token: str | None = Header(default=None)) -> dict[str, Any]:
        check_token(x_ts_token)
        from ts.app import services as sv

        ok, msg = sv.restart_task(name) if action == "restart" else sv.task_action(name, action)
        return {"ok": ok, "message": msg}

    @app.post("/api/actions/restart")
    def action_restart(x_ts_token: str | None = Header(default=None)) -> dict[str, Any]:
        check_token(x_ts_token)
        if any(j.status == "running" for j in ctx.jobs.values()):
            raise HTTPException(409, "Há uma ação em andamento; reinicie quando ela terminar.")
        restart_terminal(root)
        return {"ok": True, "message": "Reiniciando o terminal… a janela fecha e abre de novo em alguns segundos."}

    @app.post("/api/services/register")
    def services_register(x_ts_token: str | None = Header(default=None)) -> dict[str, Any]:
        check_token(x_ts_token)
        from ts.app import services as sv

        ok, msg = sv.register_tasks(root)
        return {"ok": ok, "message": msg}

    @app.post("/api/services/test")
    def services_test(kind: str, x_ts_token: str | None = Header(default=None)) -> dict[str, Any]:
        check_token(x_ts_token)
        from ts.app import services as sv

        s = ctx.settings
        tests = {"cerebro": lambda: sv.test_cerebro_reach(s.cerebro.base_url),
                 "webhook": lambda: sv.test_webhook(s.cerebro),
                 "telegram": lambda: sv.test_telegram(s),
                 "dados": lambda: sv.test_data_source(s)}
        if kind not in tests:
            raise HTTPException(404, "Teste desconhecido")
        ok, msg = tests[kind]()
        return {"ok": ok, "message": msg}

    @app.post("/api/services/cerebro-retry")
    def services_cerebro_retry(x_ts_token: str | None = Header(default=None)) -> dict[str, Any]:
        check_token(x_ts_token)
        from ts.app import cerebro as cb
        from ts.persistence.db import DatabaseManager

        db = DatabaseManager(ctx.path(ctx.settings.system.db_path))
        try:
            conn = db.get_conn()
            n = cb.retry_failed(conn)
            res = cb.flush_outbox(conn, ctx.settings.cerebro, budget_s=20)
        finally:
            db.close()
        return {"ok": not res["failed"] and not res["retry"],
                "message": f"{n} envio(s) devolvido(s) à fila · enviados {res['sent']}, para reenviar {res['retry']}, "
                           f"falharam {res['failed']}"}

    @app.post("/api/settings/cerebro")
    async def settings_cerebro(request: Request, x_ts_token: str | None = Header(default=None)) -> dict[str, Any]:
        check_token(x_ts_token)
        from ts.app import services as sv

        try:
            body = await request.json()
            saved = sv.save_cerebro(root, body if isinstance(body, dict) else {})
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        _reload_settings()
        cfg = ctx.settings.cerebro
        # apply: the webhook service reads the settings only when it starts
        if cfg.enabled:
            ok, msg = sv.restart_task("TS_Cerebro")
            applied = "Webhook do Cérebro reiniciado com a configuração nova." if ok else f"Salvo, mas: {msg}"
        else:
            sv.task_action("TS_Cerebro", "stop")
            applied = "Integração desligada; webhook parado."
        return {"ok": True, "saved": saved, "message": f"Configuração salva. {applied}", "config": cfg.model_dump()}

    @app.get("/api/version")
    def version(refresh: bool = False) -> dict[str, Any]:
        """Installed version and pending changes; ``refresh=true`` checks GitHub now (git fetch, a few seconds)."""
        if refresh:
            info = _check_version_now(ctx)
            _store_version(ctx, info)
            return info
        return _version_info(ctx)

    @app.get("/api/cerebro/{ticker}")
    def cerebro_ticker(ticker: str) -> dict[str, Any]:
        from ts.app.cerebro import norm_ticker

        t = norm_ticker(ticker)
        hist = ctx.query("SELECT id_analise, status, conviccao, data_autorizacao, data_expiracao, origem_decisao "
                         "FROM cerebro_decisions WHERE ativo=? ORDER BY id_analise DESC LIMIT 6", (t,))
        return {"enabled": ctx.settings.cerebro.enabled, "mode": ctx.settings.cerebro.mode, "ticker": t,
                "atual": cerebro_decisions_now(ctx).get(t), "historico": _records(hist) if not hist.empty else []}

    @app.post("/api/cerebro/demand")
    def cerebro_demand(ticker: str, x_ts_token: str | None = Header(default=None)) -> dict[str, Any]:
        check_token(x_ts_token)
        from ts.app import cerebro as cb
        from ts.persistence.db import DatabaseManager

        cfg = ctx.settings.cerebro
        if not cfg.enabled:
            raise HTTPException(409, "Integração com o Cérebro desligada (python -m ts cerebro config --enable)")
        t = cb.norm_ticker(ticker)
        if not t.isalnum() or len(t) > 12:
            raise HTTPException(400, "Ativo inválido")
        db = DatabaseManager(ctx.path(ctx.settings.system.db_path))
        try:
            conn = db.get_conn()
            cb.enqueue_demand(conn, [t], "urgente")
            res = cb.flush_outbox(conn, cfg, budget_s=15)
        finally:
            db.close()
        if res["sent"]:
            return {"ok": True, "message": f"Análise de {t} pedida ao Cérebro; a decisão chega em alguns segundos a ~1 min."}
        return {"ok": False, "message": f"Cérebro não respondeu agora; o pedido de {t} fica na fila e é reenviado sozinho."}

    @app.get("/api/checklist/{ticker}")
    def checklist(ticker: str) -> dict[str, Any]:
        return build_checklist_api(ctx, ticker)

    @app.get("/api/positions")
    def positions() -> list[dict[str, Any]]:
        return build_positions(ctx)

    @app.get("/api/orders")
    def orders() -> list[dict[str, Any]]:
        return build_orders(ctx)

    @app.get("/api/signals")
    def signals(day: str | None = None) -> dict[str, Any]:
        return build_signals(ctx, day)

    @app.get("/api/equity")
    def equity() -> dict[str, Any]:
        return build_equity(ctx)

    @app.get("/api/candidates")
    def candidates() -> list[dict[str, Any]]:
        return build_candidates(ctx)

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return build_runs(ctx)

    @app.post("/api/actions/{kind}")
    def action(kind: str, id: str | None = None, x_ts_token: str | None = Header(default=None)) -> dict[str, Any]:  # noqa: A002
        check_token(x_ts_token)
        if kind not in ACTIONS:
            raise HTTPException(404, "Ação desconhecida")
        if kind in ("approve", "reject"):
            if not id or not all(ch.isalnum() or ch == "-" for ch in id):
                raise HTTPException(400, "Candidata inválida")
        job = start_job(ctx, kind, id)
        return {"job": job.id}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> JSONResponse:
        j = ctx.jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "Job não encontrado")
        return JSONResponse({"id": j.id, "kind": j.kind, "status": j.status, "code": j.code, "message": j.message,
                             "elapsed": round(time.time() - j.started), "output": j.output[-2000:]})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
