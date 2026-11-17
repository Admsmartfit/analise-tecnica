"""Daily routines (PRD 1.3.3): preopen 08:45, watchdog every 15 min, EOD 19:30, dead-man 20:30, backup 23:00.

Every routine:
* exits with SKIPPED_HOLIDAY on days without a session (the Windows scheduler does not know holidays);
* holds the single-instance lock (the MetaTrader5 package is not thread-safe);
* records a ``runs`` row and notifies Telegram (falls back to a log file).

Paper mode (default): the EOD routine runs the production :class:`SessionEngine` for the closed session —
fills yesterday's plan against today's real bar, manages exits, marks to market and plans tomorrow's
orders — and persists the full book. Preopen reconciles and moves the plan to SUBMITTED/ACCEPTED (paper
broker) or sends it to MT5 when ``execution.mode == 'live'`` (only after the formal Go).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import ts
from ts.app.autoheal import spawn_reoptimization
from ts.core.calendar import B3Calendar
from ts.core.config import Settings, load_costs_config, load_settings, load_strategy_config
from ts.core.errors import ConfigError, ConnectivityError, TSError
from ts.core.lock import LockBusyError, ProcessLock
from ts.core.logging import set_run_id, setup_logging
from ts.data.corporate_actions import CorporateActionsStore
from ts.data.loader import load_signal_bars
from ts.data.mt5_gateway import MT5Gateway
from ts.data.ohlcv_store import OhlcvStore
from ts.data.sync import sync_universe
from ts.data.universe import universe_on, update_universe_file
from ts.data.yahoo_gateway import make_gateway
from ts.execution.reconcile import reconcile_positions
from ts.execution.session_engine import BookState, SessionEngine
from ts.persistence.db import DatabaseManager, backup_database
from ts.persistence.repository import PaperRepository
from ts.strategy.regime import realized_vol_percentile_series
from ts.strategy.signal_engine import prepare_ticker

logger = logging.getLogger("ts.app.routines")

Notifier = Callable[[str], Any]


def _default_notify(message: str) -> None:
    try:
        from ts.app.notify import send_telegram

        send_telegram(message)
    except Exception as exc:  # noqa: BLE001 - notification is best effort
        logger.warning("Telegram notification failed: %s", exc)


def _register_run(db: DatabaseManager, routine: str, d: date, config_hash: str) -> str:
    run_id = f"{routine}-{d.isoformat()}-{uuid.uuid4().hex[:8]}"
    db.record_run(run_id, routine, d.isoformat(), config_hash, ts.__version__)
    set_run_id(run_id)
    return run_id


def _snapshot_config(db: DatabaseManager, config_hash: str, path: str | Path = "config/strategy.yaml") -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    conn = db.get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO config_snapshots (config_hash, created_at, yaml) VALUES (?,?,?)",
        (config_hash, datetime.now(UTC).isoformat(), text),
    )
    conn.commit()


def _lock(settings: Settings, timeout_s: float = 600.0) -> ProcessLock:
    return ProcessLock(settings.system.lock_path, timeout_s=timeout_s)


def _load_reference_r(settings: Settings, config_hash: str | None = None) -> dict[str, list[float]] | None:
    """Backtest R distribution per setup (written by `ts backtest`) for the live setup monitor.

    Only used when it was produced with the same strategy configuration; a broken or foreign file never
    stops the EOD (the monitor falls back to its no-reference rule).
    """
    p = Path(settings.system.data_dir) / "reference" / "setup_r.json"
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable %s: %s", p, e)
        return None
    if config_hash and raw.get("config_hash") not in (None, config_hash):
        logger.info("Ignoring %s: produced with config %s (current %s)", p, raw.get("config_hash"), config_hash)
        return None
    out: dict[str, list[float]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            try:
                out[k] = [float(x) for x in v]
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric reference for setup %s", k)
    return out or None


# --------------------------------------------------------------------------------------------- EOD
def eod_routine(
    session_date: date | None = None,
    *,
    settings: Settings | None = None,
    gateway: MT5Gateway | None = None,
    calendar: B3Calendar | None = None,
    notify: Notifier = _default_notify,
    do_sync: bool = True,
    force: bool = False,
) -> int:
    """Exit codes: 0 OK/holiday, 2 config, 3 connectivity, 4 data quality, 5 lock busy."""
    settings = settings or load_settings()
    setup_logging(settings.system.logs_dir, settings.logging.level, settings.logging.json_lines)
    cal = calendar or B3Calendar()
    today = session_date or date.today()
    db = DatabaseManager(settings.system.db_path)

    if not cal.is_session(today):
        run_id = _register_run(db, "eod", today, "none")
        db.update_run(run_id, "SKIPPED_HOLIDAY")
        return 0

    try:
        cfg, config_hash = load_strategy_config()
        costs = load_costs_config()
    except ConfigError as e:
        run_id = _register_run(db, "eod", today, "invalid")
        db.update_run(run_id, "FAILED", str(e))
        notify(f"❌ EOD {today}: configuração inválida — {e}")
        return 2

    try:
        with _lock(settings):
            run_id = _register_run(db, "eod", today, config_hash)
            _snapshot_config(db, config_hash)
            try:
                return _eod_locked(today, run_id, settings, cfg, config_hash, costs, db, cal, gateway, notify, do_sync, force)
            except ConnectivityError as e:
                db.update_run(run_id, "FAILED", str(e))
                notify(f"❌ EOD {today}: conectividade — {e}")
                return 3
            except TSError as e:
                db.update_run(run_id, "FAILED", str(e))
                notify(f"❌ EOD {today}: {type(e).__name__} — {e}")
                return 4
            except Exception as e:  # noqa: BLE001 - never die silently
                logger.exception("eod: unexpected error")
                db.update_run(run_id, "FAILED", repr(e))
                notify(f"❌ EOD {today} FAILED: {e!r}")
                return 3
    except LockBusyError as e:
        notify(f"⚠️ EOD {today}: {e}")
        return 5


def _eod_locked(
    today: date, run_id: str, settings: Settings, cfg: Any, config_hash: str, costs: Any, db: DatabaseManager,
    cal: B3Calendar, gateway: MT5Gateway | None, notify: Notifier, do_sync: bool, force: bool,
) -> int:
    conn = db.get_conn()
    repo = PaperRepository(conn, settings.execution.mode)
    last = repo.last_snapshot_date()
    if last is not None and last >= today and not force:
        logger.info("eod: session %s already processed (snapshot %s) — idempotent no-op", today, last)
        db.update_run(run_id, "OK", f"already processed (snapshot {last})")
        return 0

    sync_info = ""
    # Universe of the current quarter: fetched from the official B3 portfolio when the file is missing.
    # Best effort — on failure the most recent composition on disk is used (and we alert).
    try:
        new_file = update_universe_file(today)
        if new_file is not None:
            notify(f"ℹ️ Universo atualizado pela carteira oficial da B3: {new_file.name} (versione no git)")
    except Exception as e:  # noqa: BLE001
        notify(f"⚠️ EOD {today}: não foi possível baixar a carteira do IBX-100 da B3 ({e}); usando o último arquivo")

    if do_sync:
        rep = sync_universe(asof=today, gateway=gateway, calendar=cal, settings=settings, db=db)
        sync_info = f"{len(rep.successful_tickers)}/{rep.total_tickers} sync"
        if rep.exit_code != 0:
            db.update_run(run_id, "FAILED", f"sync {rep.status}: failed={rep.failed_tickers} blocked={rep.blocked_tickers}")
            notify(f"❌ EOD {today}: sync abaixo do mínimo ({sync_info}); nenhuma decisão tomada (falha segura)")
            return 4

    # Sessions to process: catch up any session missed since the last snapshot (never skip a day's fills).
    if last is None or force:
        sessions = [today]
    else:
        sessions = [d for d in cal.sessions_between(last, today) if d > last] or [today]

    store = OhlcvStore(Path(settings.system.data_dir) / "ohlcv" / "d1")
    corp = CorporateActionsStore(settings.system.corporate_actions_path)
    engine = SessionEngine(cfg, costs, config_hash, cal, settings.execution.fractional_shares,
                           settings.execution.client_id_prefix)
    reference_r = _load_reference_r(settings, config_hash)
    state = repo.load_state(cfg, settings.execution.reference_capital, cal, before=sessions[0], reference_r=reference_r)

    summary: list[str] = []
    quarantine: tuple[date, str, str] | None = None
    for d in sessions:
        universe = set(universe_on(d))
        held = set(state.positions) | set(state.shadow_positions) | {o.ticker for o in state.pending}
        tickers = universe | held
        bars = load_signal_bars(store, tickers, corp, until=d)
        ibov = load_signal_bars(store, [settings.mt5.ibov_symbol], None, until=d).get(settings.mt5.ibov_symbol)
        if ibov is None or ibov.empty or ibov["session_date"].iloc[-1] != d:
            raise TSError(f"IBOV bar for {d} missing — no decision (fail safe)")
        prepared = {t: prepare_ticker(t, df, cfg) for t, df in bars.items() if not df.empty}
        missing_held = [t for t in held if t not in prepared or prepared[t].index_of(d) is None]
        if missing_held:
            notify(f"⚠️ EOD {d}: sem barra do dia para posições/ordens {missing_held} (mantidas, sem gestão hoje)")
        rp_series = realized_vol_percentile_series(ibov, cfg.regime)
        rp = float(rp_series.iloc[-1]) if len(rp_series) and rp_series.iloc[-1] == rp_series.iloc[-1] else None
        idx_ret = ibov.set_index("session_date")["close"].pct_change()
        res = engine.run_session(state, d, prepared, index_bars=ibov, regime_percentile=rp, index_returns=idx_ret,
                                 universe=universe)
        repo.record_session(run_id, res, state, config_hash)
        conn.commit()
        if state.breaker.just_halted:
            from ts.strategy.regime import market_state_series

            mstate = market_state_series(ibov, cfg.market_filter).iloc[-1]
            regime = f"IBOV {mstate}, volatilidade P{rp:.0f}" if rp is not None else f"IBOV {mstate}"
            quarantine = (d, state.breaker.halt_reason or "quarentena", regime)
        e = res.equity
        assert e is not None
        accepted = sum(1 for s in res.signals if s.disposition == "ACCEPTED")
        summary.append(
            f"{d}: PL R$ {e.equity:,.0f} | DD {e.drawdown_pct:.1%} | heat {e.heat_pct:.1%} | "
            f"{len(res.fills)} fills, {len(res.closed_trades)} saídas, {len(res.signals)} sinais ({accepted} aceitos)"
        )
        for note in res.notes:
            notify(f"⚠️ {d}: {note}")
        if e.drawdown_pct >= 0.10:
            notify(f"🚨 KILL SWITCH G2: drawdown {e.drawdown_pct:.1%} ≥ 10% em {d} — interromper o paper e revisar")

    db.update_run(run_id, "OK")
    notify(f"✅ EOD concluído {today} ({sync_info}). " + " || ".join(summary))
    if quarantine is not None:
        qd, why, regime = quarantine
        notify(f"🛑 QUARENTENA desde {qd}: {why} ({regime}). Nenhuma entrada nova; saídas das posições abertas "
               "seguem normalmente. Reotimização iniciada em segundo plano — o resultado chega para sua aprovação.")
        try:
            spawn_reoptimization(Path.cwd(), f"{why} em {qd}", regime)
        except Exception as e:  # noqa: BLE001 - quarantine itself is already in force
            notify(f"⚠️ Não foi possível iniciar a reotimização: {e!r} (rode `python -m ts reoptimize`)")
    return 0


# ------------------------------------------------------------------------------------------ PREOPEN
def preopen_routine(
    session_date: date | None = None,
    *,
    settings: Settings | None = None,
    gateway: MT5Gateway | None = None,
    broker: Any = None,
    calendar: B3Calendar | None = None,
    notify: Notifier = _default_notify,
    check_terminal: bool = True,
) -> int:
    """Health check, reconciliation and submission of today's plan. Exit: 0 OK, 3 connectivity, 4 divergence, 5 lock."""
    settings = settings or load_settings()
    setup_logging(settings.system.logs_dir, settings.logging.level, settings.logging.json_lines)
    cal = calendar or B3Calendar()
    today = session_date or date.today()
    db = DatabaseManager(settings.system.db_path)
    if not cal.is_session(today):
        run_id = _register_run(db, "preopen", today, "none")
        db.update_run(run_id, "SKIPPED_HOLIDAY")
        return 0
    cfg, config_hash = load_strategy_config()
    live = settings.execution.mode == "live"
    try:
        with _lock(settings, timeout_s=300):
            run_id = _register_run(db, "preopen", today, config_hash)
            conn = db.get_conn()
            repo = PaperRepository(conn, settings.execution.mode)

            if check_terminal:
                gw = gateway or make_gateway(settings)
                try:
                    gw.connect()
                    h = gw.health()
                    if live and not h.trade_allowed:
                        raise ConnectivityError("AutoTrading disabled")
                except ConnectivityError as e:
                    db.update_run(run_id, "FAILED", str(e))
                    notify(f"❌ PREOPEN {today}: terminal indisponível — {e}")
                    if live:
                        return 3
                finally:
                    if gateway is None:
                        gw.shutdown()

            state = repo.load_state(cfg, settings.execution.reference_capital, cal)
            # Reconcile the system's positions table with the broker book (paper: snapshot; live: MT5).
            rows = conn.execute(
                "SELECT ticker, qty_open FROM positions WHERE status IN ('OPEN','PARTIAL') AND is_shadow=0"
            ).fetchall()
            internal = {r[0]: {"qty": r[1]} for r in rows}
            if live:
                from ts.execution.mt5_broker import MT5Broker

                broker = broker or MT5Broker(settings=settings)
                broker_positions = list(broker.open_positions())
            else:
                from ts.core.models import BrokerPosition

                broker_positions = [
                    BrokerPosition(p.position_id, t, p.qty_open, Decimal(str(p.entry_price)), Decimal(str(p.entry_price)),
                                   Decimal(str(p.current_stop)), None, datetime.now(UTC))
                    for t, p in state.positions.items()
                ]
            rec = reconcile_positions(internal, broker_positions)
            if not rec.ok:
                detail = "; ".join(f"{d.kind}:{d.ticker}" for d in rec.divergences)
                notify(f"⚠️ PREOPEN {today}: divergências de reconciliação — {detail}. Novas ordens bloqueadas.")
                db.update_run(run_id, "FAILED", f"reconciliation: {detail}")
                return 4

            from ts.execution.bracket import build_preopen_package

            pkg = build_preopen_package(state, today, cfg, settings.execution.client_id_prefix,
                                        settings.execution.fractional_shares)
            sent = 0
            if live:
                tickets = {p.ticker: p for p in broker_positions}
                for su in pkg.stop_updates:  # trailing / break-even decided at the last EOD
                    bp = tickets.get(su.ticker)
                    if bp is not None and abs(float(bp.current_stop or 0) - su.stop) > cfg.risk.tick_size / 2:
                        try:
                            broker.set_stop(su.ticker, int(bp.position_id), su.stop)
                        except TSError as e:
                            notify(f"❌ PREOPEN {today}: falha ao mover o stop de {su.ticker} para {su.stop:.2f} — {e}")
                for intent in pkg.all_orders:
                    repo.record_intent(intent, config_hash)
                    try:
                        r = broker.submit(intent)
                        repo.mark_submitted(intent.client_order_id, r.message, r.broker_order_id, r.retcode, r.state.value)
                        sent += 1
                    except TSError as e:
                        repo.mark_submitted(intent.client_order_id, str(e),
                                            state="UNKNOWN" if "UNKNOWN" in str(e) else "REJECTED")
                        notify(f"❌ PREOPEN {today}: ordem {intent.client_order_id} recusada — {e}")
            else:
                for o in [o for o in state.pending if not o.is_shadow and o.valid_until >= today]:
                    repo.mark_submitted(o.client_order_id, "paper: submitted to PaperBroker")
                    repo.mark_submitted(o.client_order_id, "paper: accepted (fill simulated at EOD with the real bar)",
                                        state="ACCEPTED")
                    sent += 1
            conn.commit()
            db.update_run(run_id, "OK")
            notify(f"✅ PREOPEN {today} ({settings.execution.mode}): {sent} ordens enviadas; reconciliação OK "
                   f"({rec.matched_count} posições)")
            return 0
    except LockBusyError as e:
        notify(f"⚠️ PREOPEN {today}: {e}")
        return 5


# ----------------------------------------------------------------------------------------- WATCHDOG
def _terminal_process_alive() -> bool | None:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe"], capture_output=True, text=True,
                             timeout=20, check=False).stdout
        return "terminal64.exe" in out
    except (OSError, subprocess.SubprocessError):
        return None


def watchdog_routine(
    session_date: date | None = None,
    *,
    settings: Settings | None = None,
    gateway: MT5Gateway | None = None,
    calendar: B3Calendar | None = None,
    notify: Notifier = _default_notify,
    now_local: datetime | None = None,
) -> int:
    """Heartbeat: terminal process, connection, tick age of a liquid stock (silent disconnect). Always exits 0."""
    settings = settings or load_settings()
    setup_logging(settings.system.logs_dir, settings.logging.level, settings.logging.json_lines)
    cal = calendar or B3Calendar()
    today = session_date or date.today()
    if not cal.is_session(today):
        return 0
    db = DatabaseManager(settings.system.db_path)
    try:
        with _lock(settings, timeout_s=0):
            run_id = _register_run(db, "watchdog", today, "watchdog")
            problems: list[str] = []
            is_mt5 = settings.data.provider == "mt5"
            if gateway is None and is_mt5 and _terminal_process_alive() is False:
                problems.append("terminal64.exe não está rodando")
            gw = gateway or make_gateway(settings)
            # Yahoo quotes are ~15 min delayed: a 120 s threshold would alert all day long.
            max_age = settings.mt5.max_tick_age_seconds if is_mt5 else max(settings.mt5.max_tick_age_seconds, 2400.0)
            try:
                gw.connect()
                h = gw.health()
                if not h.ok:
                    problems.append(f"terminal não OK: {h.details}")
                now = now_local or datetime.now()
                in_session = cal.open_time(today) <= now.time() <= cal.close_time(today)
                if in_session and (h.tick_age_seconds is None or h.tick_age_seconds > max_age):
                    problems.append(f"tick parado há {h.tick_age_seconds}s em {settings.mt5.heartbeat_symbol} (desconexão silenciosa?)")
            except ConnectivityError as e:
                problems.append(str(e))
            finally:
                if gateway is None:
                    gw.shutdown()
            db.update_run(run_id, "FAILED" if problems else "OK", "; ".join(problems) or None)
            if problems:
                notify(f"⚠️ WATCHDOG {today}: " + " | ".join(problems))
    except LockBusyError:
        logger.info("watchdog: another routine is running — skipped")
    return 0


# ----------------------------------------------------------------------------------------- DEAD-MAN
def deadman_routine(
    session_date: date | None = None,
    *,
    settings: Settings | None = None,
    calendar: B3Calendar | None = None,
    notify: Notifier = _default_notify,
) -> int:
    """20:30: alert if the EOD did not finish OK for today's session (PRD 1.3.3 dead-man switch)."""
    settings = settings or load_settings()
    cal = calendar or B3Calendar()
    today = session_date or date.today()
    if not cal.is_session(today):
        return 0
    db = DatabaseManager(settings.system.db_path)
    run_id = _register_run(db, "deadman", today, "deadman")
    ok = db.get_conn().execute(
        "SELECT 1 FROM runs WHERE routine='eod' AND session_date=? AND status='OK'", (today.isoformat(),)
    ).fetchone()
    if ok is None:
        notify(f"🚨 DEAD-MAN {today}: EOD NÃO concluiu até as 20:30!")
        db.update_run(run_id, "FAILED", "EOD not completed")
        return 1
    db.update_run(run_id, "OK")
    return 0


# ------------------------------------------------------------------------------------------- BACKUP
def backup_routine(
    session_date: date | None = None,
    *,
    settings: Settings | None = None,
    notify: Notifier = _default_notify,
    keep: int = 14,
) -> int:
    """SQLite online backup (sqlite3 backup API, never a file copy) + Parquet copy. Exit 0 OK, 3 failure."""
    settings = settings or load_settings()
    setup_logging(settings.system.logs_dir, settings.logging.level, settings.logging.json_lines)
    today = session_date or date.today()
    db = DatabaseManager(settings.system.db_path)
    try:
        with _lock(settings):
            run_id = _register_run(db, "backup", today, "backup")
            try:
                root = Path(settings.system.data_dir) / "backups"
                db_backup = root / f"trading_{today.isoformat()}.db"
                backup_database(db.get_conn(), db_backup)
                src = Path(settings.system.data_dir) / "ohlcv"
                dst = root / f"ohlcv_{today.isoformat()}"
                if src.exists():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                for pattern in ("trading_*.db", "ohlcv_*"):
                    old = sorted(root.glob(pattern))[:-keep]
                    for p in old:
                        shutil.rmtree(p) if p.is_dir() else p.unlink()
                n_parquet = len(list(dst.rglob("*.parquet"))) if dst.exists() else 0
                db.update_run(run_id, "OK")
                notify(f"✅ BACKUP {today}: {db_backup.name} + {n_parquet} Parquet")
                return 0
            except Exception as exc:  # noqa: BLE001
                logger.exception("backup failed")
                db.update_run(run_id, "FAILED", repr(exc))
                notify(f"❌ BACKUP {today} FAILED: {exc!r}")
                return 3
    except LockBusyError as e:
        notify(f"⚠️ BACKUP {today}: {e}")
        return 5


def deadman_due(now: datetime, cutoff: time = time(20, 30)) -> bool:
    return now.time() >= cutoff


__all__ = [
    "BookState",
    "backup_routine",
    "deadman_routine",
    "eod_routine",
    "preopen_routine",
    "watchdog_routine",
]
