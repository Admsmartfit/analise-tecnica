"""Quarantine → re-optimization → trader approval (PRD v2.1, adjusted version).

* ``run_reoptimization`` (CLI ``ts reoptimize``): started by the EOD in the background when the quarantine fires.
  Writes ``reports/reopt_<id>.md`` and ``config/strategy.candidate.yaml`` and a ``config_candidates`` row; never
  touches the working configuration.
* ``approve`` (CLI ``ts config approve <id>``, button in the terminal): validates the candidate against the
  guardrails again, writes ``config/strategy.override.yaml`` (git-ignored), logs every change as
  ``[AUTO-HEAL] Param X alterado de Y para Z. Motivo: ...`` (log + ``param_changes`` table), and takes the book out
  of quarantine. The paper count restarts with the new configuration hash (PRD 1.4.4).
* ``reject``: keeps the current configuration; the quarantine ends only through the shadow curve.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from ts.core.config import Settings, load_costs_config, load_effective_strategy, strategy_config_hash
from ts.core.errors import ConfigError
from ts.core.guardrails import OVERRIDE_FILE, apply_override, check_limits, load_limits, load_override
from ts.core.lock import ProcessLock
from ts.persistence.db import DatabaseManager

logger = logging.getLogger("ts.app.autoheal")

CONFIG_DIR = Path("config")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _notify(msg: str) -> None:
    try:
        from ts.app.notify import send_telegram

        send_telegram(msg)
    except Exception as e:  # noqa: BLE001
        logger.warning("notify failed: %s", e)


def spawn_reoptimization(root: Path, reason: str, regime: str) -> None:
    """Start ``ts reoptimize`` detached (the EOD must not wait for it)."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(  # noqa: S603 - fixed argv, our own CLI
        [sys.executable, "-m", "ts", "reoptimize", "--reason", reason, "--regime", regime],
        cwd=root, creationflags=flags, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )


def run_reoptimization(settings: Settings, reason: str, regime: str = "", config_dir: Path = CONFIG_DIR) -> str:
    from ts.backtest.reoptimize import reoptimize
    from ts.data.corporate_actions import CorporateActionsStore
    from ts.data.loader import load_signal_bars
    from ts.data.ohlcv_store import OhlcvStore
    from ts.data.universe import universe_on

    db = DatabaseManager(settings.system.db_path)
    conn = db.get_conn()
    cfg, cur_hash, eff = load_effective_strategy(config_dir / "strategy.yaml")
    limits = load_limits(config_dir)
    if limits is None:
        raise ConfigError(f"{config_dir / 'strategy_default.yaml'} ausente: sem travas, a reotimização não roda")
    cid = f"R{datetime.now():%Y%m%d%H%M}-{uuid.uuid4().hex[:4]}"
    conn.execute("UPDATE config_candidates SET status='REJECTED', decided_at=?, decision_note='substituída por nova reotimização' "
                 "WHERE status='PENDING'", (_now(),))
    conn.execute("INSERT INTO config_candidates (candidate_id, created_at, trigger_reason, market_regime, base_hash, "
                 "current_hash, status) VALUES (?,?,?,?,?,?, 'RUNNING')",
                 (cid, _now(), reason, regime, eff.base_hash, cur_hash))
    conn.commit()
    try:
        store = OhlcvStore(Path(settings.system.data_dir) / "ohlcv" / "d1")
        ibov_sym = settings.mt5.ibov_symbol.upper()
        tickers = sorted(p.stem for p in store.data_dir.glob("*.parquet") if p.stem != ibov_sym)
        bars = load_signal_bars(store, tickers, CorporateActionsStore(settings.system.corporate_actions_path))
        ibov = load_signal_bars(store, [ibov_sym]).get(ibov_sym)
        if ibov is None:
            raise ConfigError("IBOV ausente no store")
        res = reoptimize(bars, ibov, cfg, limits, load_costs_config(), settings.execution.reference_capital,
                         universe_on, f"{reason} | regime: {regime}")
        reports = Path(settings.system.reports_dir)
        reports.mkdir(parents=True, exist_ok=True)
        rpath = reports / f"reopt_{cid}.md"
        rpath.write_text(res.report_markdown, encoding="utf-8")
        params = {k: b for k, (_, b) in res.changes.items()}
        summary: dict[str, Any] = {"trials": len(res.trials), "pbo": res.pbo}
        if res.candidate is not None:
            best = next(t for t in res.trials if strategy_config_hash(t.cfg) == strategy_config_hash(res.candidate))
            summary.update({"expectancy_r": best.metrics.expectancy_r, "ci_lower": best.metrics.bootstrap_ci_lower_r,
                            "dsr": best.metrics.deflated_sharpe_prob, "trades": best.metrics.total_trades})
            (config_dir / "strategy.candidate.yaml").write_text(
                "# Candidata da reotimização — NÃO é usada até você aprovar (terminal ou `ts config approve`).\n"
                + yaml.safe_dump({"candidate_id": cid, "base_hash": eff.base_hash, "params": params},
                                 sort_keys=True, allow_unicode=True), encoding="utf-8")
        conn.execute("UPDATE config_candidates SET status=?, candidate_hash=?, params_json=?, summary_json=?, report_path=? "
                     "WHERE candidate_id=?",
                     (res.verdict, strategy_config_hash(res.candidate) if res.candidate else None,
                      json.dumps(params, sort_keys=True), json.dumps(summary), str(rpath), cid))
        conn.commit()
        if res.candidate is not None:
            _notify(f"🧪 Reotimização {cid}: candidata encontrada ({len(params)} parâmetros). Revise e aprove no terminal.")
        else:
            _notify(f"🧪 Reotimização {cid}: nenhuma configuração passou nos critérios. Quarentena segue pela curva sombra.")
        return cid
    except Exception as e:
        conn.execute("UPDATE config_candidates SET status='FAILED', decision_note=? WHERE candidate_id=?", (repr(e), cid))
        conn.commit()
        _notify(f"❌ Reotimização {cid} falhou: {e!r}")
        raise
    finally:
        db.close()


def _row(conn: Any, cid: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM config_candidates WHERE candidate_id=?", (cid,)).fetchone()
    if r is None:
        raise ConfigError(f"Candidata {cid} não encontrada")
    return dict(r)


def approve(settings: Settings, cid: str, note: str = "", config_dir: Path = CONFIG_DIR) -> list[str]:
    """Apply a PENDING candidate. Returns the [AUTO-HEAL] lines written."""
    from ts.core.calendar import B3Calendar
    from ts.core.config import load_strategy_config
    from ts.persistence.repository import PaperRepository

    with ProcessLock(settings.system.lock_path, timeout_s=600):
        db = DatabaseManager(settings.system.db_path)
        conn = db.get_conn()
        try:
            row = _row(conn, cid)
            if row["status"] != "PENDING":
                raise ConfigError(f"Candidata {cid} está {row['status']}, não PENDING")
            _, cur_hash, eff = load_effective_strategy(config_dir / "strategy.yaml")
            if eff.base_hash != row["base_hash"]:
                raise ConfigError("strategy.yaml mudou desde a reotimização: candidata obsoleta, rode de novo")
            if cur_hash != row["current_hash"]:
                raise ConfigError("A configuração em uso mudou desde a reotimização: candidata obsoleta")
            limits = load_limits(config_dir)
            new_params = dict((load_override(config_dir) or {}).get("params") or {}) if eff.override_status == "applied" else {}
            new_params.update(json.loads(row["params_json"] or "{}"))
            override = {"base_hash": eff.base_hash, "candidate_id": cid, "approved_at": _now(), "params": new_params}
            base_raw = yaml.safe_load((config_dir / "strategy.yaml").read_text(encoding="utf-8")) or {}
            from ts.core.config import StrategyConfig

            base_dump = StrategyConfig.model_validate(base_raw).model_dump(mode="json")
            trial = apply_override(base_dump, eff.base_hash, override, limits)
            problems = trial.violations + (check_limits(trial.raw, limits) if limits else [])
            if problems:
                conn.execute("UPDATE config_candidates SET status='REJECTED', decided_at=?, decision_note=? WHERE candidate_id=?",
                             (_now(), "bloqueada pelas travas: " + "; ".join(problems), cid))
                conn.commit()
                _notify(f"⛔ Candidata {cid} bloqueada pelas travas: {'; '.join(problems)}")
                raise ConfigError("Candidata viola as travas: " + "; ".join(problems))

            hist = config_dir / "history"
            hist.mkdir(exist_ok=True)
            ov = config_dir / OVERRIDE_FILE
            if ov.exists():
                shutil.copy2(ov, hist / f"strategy.override.{datetime.now():%Y%m%d%H%M%S}.yaml")
            ov.write_text("# Parâmetros aprovados pelo trader (reotimização da quarentena). Fora do git.\n"
                          + yaml.safe_dump(override, sort_keys=True, allow_unicode=True), encoding="utf-8")
            new_cfg, new_hash = load_strategy_config(config_dir / "strategy.yaml")

            reason = f"{row['trigger_reason']}. Regime identificado: {row['market_regime'] or 'n/d'}"
            lines: list[str] = []
            for k, v in json.loads(row["params_json"] or "{}").items():
                old = _dotted(base_dump, k) if k not in (eff.override or {}).get("params", {}) else eff.override["params"][k]
                line = f"[AUTO-HEAL] Param {k} alterado de {old} para {v}. Motivo: {reason}"
                logger.warning(line)
                lines.append(line)
                conn.execute("INSERT INTO param_changes (ts_utc, candidate_id, param, old_value, new_value, reason) "
                             "VALUES (?,?,?,?,?,?)", (_now(), cid, k, json.dumps(old), json.dumps(v), reason))
            conn.execute("INSERT OR IGNORE INTO config_snapshots (config_hash, created_at, yaml) VALUES (?,?,?)",
                         (new_hash, _now(), yaml.safe_dump(new_cfg.model_dump(mode="json"), sort_keys=True)))

            # Leave quarantine: the trader approved a configuration validated by G0 criteria.
            repo = PaperRepository(conn, settings.execution.mode)
            last = repo.last_snapshot_date()
            if last is not None:
                state = repo.load_state(new_cfg, settings.execution.reference_capital, B3Calendar())
                state.breaker.resume(state.breaker.current_equity, f"configuração {cid} aprovada")
                repo.save_snapshot(last, state, new_hash)
                conn.execute("UPDATE protections SET active_until=? WHERE scope='PORTFOLIO' AND active_until IS NULL",
                             (date.today().isoformat(),))
            conn.execute("UPDATE config_candidates SET status='APPROVED', decided_at=?, decision_note=? WHERE candidate_id=?",
                         (_now(), note or "aprovada pelo trader", cid))
            conn.commit()
            cand = config_dir / "strategy.candidate.yaml"
            if cand.exists():
                cand.unlink()
            _notify(f"✅ Configuração {cid} aprovada ({len(lines)} parâmetros). Quarentena encerrada; a contagem do "
                    f"paper reinicia com o hash {new_hash}.")
            return lines
        finally:
            db.close()


def reject(settings: Settings, cid: str, note: str = "", config_dir: Path = CONFIG_DIR) -> None:
    db = DatabaseManager(settings.system.db_path)
    conn = db.get_conn()
    try:
        row = _row(conn, cid)
        if row["status"] != "PENDING":
            raise ConfigError(f"Candidata {cid} está {row['status']}, não PENDING")
        conn.execute("UPDATE config_candidates SET status='REJECTED', decided_at=?, decision_note=? WHERE candidate_id=?",
                     (_now(), note or "rejeitada pelo trader", cid))
        conn.commit()
        cand = config_dir / "strategy.candidate.yaml"
        if cand.exists():
            cand.unlink()
        _notify(f"↩️ Candidata {cid} rejeitada. Configuração mantida; a quarentena termina pela curva sombra.")
    finally:
        db.close()


def _dotted(d: dict[str, Any], path: str) -> Any:
    cur: Any = d
    for p in path.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur
