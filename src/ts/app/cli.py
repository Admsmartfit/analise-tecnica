"""Command Line Interface for Trading System B3."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import struct
import sys
from datetime import date
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from ts.core.config import load_settings
from ts.core.errors import ConnectivityError
from ts.core.logging import setup_logging
from ts.data.ohlcv_store import OhlcvStore
from ts.data.sync import sync_universe
from ts.data.universe import universe_on
from ts.persistence.db import DatabaseManager

console = Console()


def doctor_command(args: argparse.Namespace) -> int:
    """Run comprehensive health checks on environment, dependencies, terminal and permissions."""
    console.print("\n[bold cyan]Trading System B3 — Doctor Diagnostic Check[/bold cyan]\n")

    table = Table(title="Diagnostic Results", show_lines=True)
    table.add_column("Check Item", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Details")

    # 1. Python Architecture
    bit_size = struct.calcsize("P") * 8
    py_ver = platform.python_version()
    if bit_size == 64:
        table.add_row("Python 64-bit", "[green]PASS[/green]", f"Python {py_ver} (64-bit)")
    else:
        table.add_row("Python 64-bit", "[red]FAIL[/red]", f"Python {py_ver} ({bit_size}-bit, 64-bit required)")

    # 2. Settings Config
    settings = None
    try:
        settings = load_settings()
        table.add_row("Configuration", "[green]PASS[/green]", f"Mode: {settings.execution.mode}")
    except Exception as e:
        table.add_row("Configuration", "[red]FAIL[/red]", str(e))
        console.print(table)
        return 2

    # 3. Disk Space & Data Directory Permissions
    try:
        data_p = Path(settings.system.data_dir)
        data_p.mkdir(parents=True, exist_ok=True)
        test_file = data_p / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()

        total, used, free = shutil.disk_usage(data_p)
        free_gb = free / (1024**3)
        table.add_row(
            "Disk & Write Permissions",
            "[green]PASS[/green]",
            f"Write OK | Free space: {free_gb:.1f} GB",
        )
    except Exception as e:
        table.add_row("Disk & Write Permissions", "[red]FAIL[/red]", str(e))

    # 4. MetaTrader 5 Connectivity
    try:
        # If in paper mode or no MT5 terminal available, gateway will inform
        from ts.data.yahoo_gateway import make_gateway

        gateway = make_gateway(settings)
        try:
            gateway.connect()
            health = gateway.health()
            if health.ok and (health.trade_allowed or settings.execution.mode == "paper"):
                conn_status = "[green]PASS[/green]"
                age = f"{health.tick_age_seconds:.0f}s" if health.tick_age_seconds is not None else "n/d"
                detail_str = (
                    f"Terminal: {health.terminal_version} | Connected: {health.connected} | "
                    f"AlgoTrading: {health.trade_allowed} | idade da cotação: {age}"
                )
                if settings.data.provider == "mt5":
                    detail_str += (
                        f" | ping servidor: {health.details.get('server_ping_ms')} ms ({health.details.get('company')})"
                    )
                else:
                    detail_str += " (Yahoo tem ~15 min de atraso; irrelevante para decisão D1)"
            else:
                conn_status = "[yellow]WARN[/yellow]"
                detail_str = f"Terminal reachable but status not fully OK: {health.details}"
            table.add_row(f"Market data ({settings.data.provider})", conn_status, detail_str)
            gateway.shutdown()
        except ConnectivityError as ce:
            table.add_row(
                "MetaTrader 5 Terminal",
                "[yellow]WARN[/yellow]",
                f"MT5 terminal not reachable: {ce}",
            )
    except Exception as e:
        table.add_row("MetaTrader 5 Terminal", "[red]FAIL[/red]", str(e))

    # 5. Database & Migrations
    try:
        db = DatabaseManager(settings.system.db_path)
        conn = db.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM schema_migrations;")
        mig_count = cur.fetchone()[0]
        table.add_row("SQLite & Migrations", "[green]PASS[/green]", f"{mig_count} migrations applied")
    except Exception as e:
        table.add_row("SQLite & Migrations", "[red]FAIL[/red]", str(e))

    # 6. Calendar coverage and strategy config
    try:
        from ts.core.calendar import B3Calendar
        from ts.core.config import load_strategy_config

        cal = B3Calendar()
        last_holiday_year = max(h.year for h in cal.holidays)
        _, cfg_hash = load_strategy_config()
        status = "[green]PASS[/green]" if last_holiday_year > date.today().year else "[yellow]WARN[/yellow]"
        table.add_row("Calendar / strategy.yaml", status,
                      f"Holidays until {last_holiday_year} | strategy hash {cfg_hash} | mode {settings.execution.mode}")
    except Exception as e:
        table.add_row("Calendar / strategy.yaml", "[red]FAIL[/red]", str(e))

    # 7. MetaTrader5 package (Windows only)
    try:
        import importlib.metadata as md

        table.add_row("MetaTrader5 package", "[green]PASS[/green]", md.version("MetaTrader5"))
    except Exception:
        table.add_row("MetaTrader5 package", "[yellow]WARN[/yellow]", "not installed (required on the trading machine)")

    console.print(table)
    return 0


def backtest_command(args: argparse.Namespace) -> int:
    """Run a backtest (or the full G0 walk-forward) on the Parquet store and write the report."""
    import json

    from ts.backtest.engine import BacktestEngine
    from ts.backtest.walkforward import WalkForwardEngine
    from ts.core.config import load_costs_config, load_strategy_config
    from ts.data.corporate_actions import CorporateActionsStore
    from ts.data.loader import load_signal_bars

    settings = load_settings()
    setup_logging(settings.system.logs_dir, settings.logging.level, settings.logging.json_lines)
    cfg, cfg_hash = load_strategy_config()
    costs = load_costs_config()
    store = OhlcvStore(f"{settings.system.data_dir}/ohlcv/d1")
    tickers = sorted(p.stem for p in store.data_dir.glob("*.parquet") if p.stem != settings.mt5.ibov_symbol.upper())
    bars = load_signal_bars(store, tickers, CorporateActionsStore(settings.system.corporate_actions_path))
    ibov = load_signal_bars(store, [settings.mt5.ibov_symbol]).get(settings.mt5.ibov_symbol.upper())
    if ibov is None:
        console.print("[red]IBOV not found in the store — run sync first[/red]")
        return 4
    universe_fn = universe_on if not args.no_universe else None
    out_dir = Path(settings.system.reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    capital = settings.execution.reference_capital

    if getattr(args, "regimes", False):
        from ts.backtest.regime_report import regime_report

        text, _ = regime_report(bars, ibov, cfg, costs, capital, universe_fn)
        path = out_dir / f"regimes_{date.today().isoformat()}_{cfg_hash}.md"
        path.write_text(text, encoding="utf-8")
        console.print(text)
        console.print(f"Relatório: {path}")
        return 0

    if args.g0:
        wf = WalkForwardEngine(cfg, costs, capital)
        rep = wf.run_full_g0(bars, ibov, universe_fn=universe_fn)
        path = out_dir / f"G0_{date.today().isoformat()}_{cfg_hash}.md"
        path.write_text(rep.summary_markdown, encoding="utf-8")
        console.print(rep.summary_markdown)
        console.print(f"[bold]{'APROVADO' if rep.approved else 'REPROVADO'}[/bold] — {path}")
        return 0

    eng = BacktestEngine(cfg, costs, capital)
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    res = eng.run(bars, ibov, start, end, universe_fn=universe_fn)
    wf = WalkForwardEngine(cfg, costs, capital)
    rep = wf.generate_g0_report(res)
    path = out_dir / f"backtest_{date.today().isoformat()}_{cfg_hash}.md"
    path.write_text(rep.summary_markdown + f"\n\nIR estimado no período: R$ {res.total_tax:,.2f}\n", encoding="utf-8")
    # Reference distribution of R per setup for the live setup monitor (PRD 1.2.5)
    ref_dir = Path(settings.system.data_dir) / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)
    by_setup: dict[str, list[float]] = {}
    for t in res.trades:
        by_setup.setdefault(t.setup, []).append(t.r_multiple)
    (ref_dir / "setup_r.json").write_text(json.dumps({"config_hash": cfg_hash, **by_setup}), encoding="utf-8")
    console.print(rep.summary_markdown)
    console.print(f"Relatório: {path}")
    return 0


def corp_actions_command(args: argparse.Namespace) -> int:
    """Import cash dividends from Yahoo into config/corporate_actions.csv (merge, no duplicates)."""
    import pandas as pd

    from ts.data.yahoo_gateway import YahooGateway

    settings = load_settings()
    path = Path(settings.system.corporate_actions_path)
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else universe_on(date.today())
    start = date.fromisoformat(args.start)
    rows: list[dict[str, object]] = []
    with YahooGateway(settings) as gw:
        for t in tickers:
            try:
                rows += gw.dividend_actions(t, start, date.today())
            except Exception as e:  # noqa: BLE001 - keep going with the other tickers
                console.print(f"[yellow]{t}: {e}[/yellow]")
    new = pd.DataFrame(rows, columns=["ticker", "ex_date", "action_type", "value", "factor"])
    old = pd.read_csv(path, comment="#") if path.exists() else new.iloc[0:0]
    merged = pd.concat([old, new]).drop_duplicates(subset=["ticker", "ex_date", "action_type"], keep="first")
    merged = merged.sort_values(["ticker", "ex_date"])
    header = "# Gerado/mesclado por `python -m ts corp-actions`. Yahoo ja entrega precos ajustados por desdobramento:\n" \
             "# NAO inclua SPLIT/REVERSE_SPLIT aqui quando data.provider = yahoo.\n"
    # LF line endings, same as the repository: a CRLF copy shows up as "modified" and blocks the auto-update
    path.write_text(header + merged.to_csv(index=False, lineterminator="\n"), encoding="utf-8", newline="\n")
    console.print(f"{len(merged) - len(old)} novos eventos; total {len(merged)} em {path}")
    return 0


def universe_update_command(args: argparse.Namespace) -> int:
    """Download the current IBX-100 portfolio from B3 into config/universe/ibx100_<quarter>.csv."""
    from ts.data.universe import update_universe_file

    d = date.fromisoformat(args.asof) if args.asof else date.today()
    path = update_universe_file(d, overwrite=True)
    console.print(f"Universo gravado: {path}  ({len(universe_on(d))} ativos)")
    return 0


def version_command(args: argparse.Namespace) -> int:
    from ts.app.version import check_version

    v = check_version()
    if v.error:
        console.print(f"[red]Não foi possível verificar a versão: {v.error}[/red]")
        return 3
    console.print(f"Instalada: {(v.installed or '')[:7]} ({v.installed_date})")
    if v.update_available:
        console.print(f"[yellow]Nova versão disponível: {(v.available or '')[:7]}[/yellow]")
        for line in v.pending:
            console.print(f"  - {line}")
        console.print("Atualize com: python -m ts update")
    else:
        console.print("[green]Última versão instalada.[/green]")
    return 0


def update_command(args: argparse.Namespace) -> int:
    from ts.app.version import run_update

    console.print("Atualizando (git + testes; rollback automático se falhar)...")
    code, msg = run_update()
    console.print(msg)
    return code


def reoptimize_command(args: argparse.Namespace) -> int:
    """Quarantine re-optimization (started by the EOD; can be run by hand)."""
    from ts.app.autoheal import run_reoptimization

    settings = load_settings()
    setup_logging(settings.system.logs_dir, settings.logging.level, settings.logging.json_lines, console=False)
    cid = run_reoptimization(settings, args.reason or "execução manual", args.regime or "")
    console.print(f"Reotimização {cid} concluída. Veja `python -m ts config status`.")
    return 0


def config_command(args: argparse.Namespace) -> int:
    """Quarantine candidates: status / approve / reject."""
    import json

    from ts.app.autoheal import approve, reject
    from ts.core.errors import ConfigError

    settings = load_settings()
    setup_logging(settings.system.logs_dir, settings.logging.level, settings.logging.json_lines)
    try:
        if args.action == "approve":
            for line in approve(settings, args.id, args.note or ""):
                console.print(line)
            return 0
        if args.action == "reject":
            reject(settings, args.id, args.note or "")
            console.print(f"Candidata {args.id} rejeitada.")
            return 0
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        return 2
    db = DatabaseManager(settings.system.db_path)
    rows = db.get_conn().execute(
        "SELECT candidate_id, created_at, status, trigger_reason, params_json, report_path FROM config_candidates "
        "ORDER BY created_at DESC LIMIT 10").fetchall()
    if not rows:
        console.print("Nenhuma reotimização registrada.")
    for r in rows:
        console.print(f"{r[0]}  {r[2]:<10} {r[1][:16]}  {r[3]}\n    parâmetros: {json.loads(r[4] or '{}')}  relatório: {r[5]}")
    return 0


def ui_command(args: argparse.Namespace) -> int:
    """Open the visual terminal (local API + desktop window)."""
    from ts.app.desktop import run_ui

    return run_ui(port=args.port, window=not args.browser)


def cerebro_command(args: argparse.Namespace) -> int:
    """Integração com o TradingAgents B3 (Cérebro): serve | status | demand | flush | retry | config."""
    from ts.app import cerebro as cb

    if args.action == "config":
        updates: dict[str, Any] = {"base_url": args.url, "mode": args.mode, "webhook_port": args.port,
                                   "webhook_host": args.host, "min_conviccao": args.min_conviccao}
        if args.enable:
            updates["enabled"] = True
        if args.disable:
            updates["enabled"] = False
        p = cb.write_local_config(Path("config"), updates)
        console.print(f"Configuração gravada em {p} (fora do git; sobrevive às atualizações).")
        console.print("Reinicie o serviço do webhook para aplicar: tarefa agendada TS_Cerebro "
                      "(ou `python -m ts cerebro serve`).")
        return 0
    settings = load_settings()
    if args.action == "serve":
        return cb.serve(settings, Path.cwd())
    db = DatabaseManager(settings.system.db_path)
    conn = db.get_conn()
    cfg = settings.cerebro
    if args.action == "demand":
        if not args.tickers:
            console.print("[red]Informe ao menos um ativo: python -m ts cerebro demand PETR4 VALE3[/red]")
            return 2
        ativos = cb.enqueue_demand(conn, args.tickers, args.prioridade or cfg.demand_priority)
        res = cb.flush_outbox(conn, cfg, budget_s=30)
        console.print(f"Pedido de análise para {', '.join(ativos)}: {res}")
        return 0 if not res["failed"] else 3
    if args.action == "flush":
        console.print(cb.flush_outbox(conn, cfg, budget_s=600))
        return 0
    if args.action == "retry":
        console.print(f"{cb.retry_failed(conn)} envio(s) devolvido(s) à fila; enviando…")
        console.print(cb.flush_outbox(conn, cfg, budget_s=600))
        return 0
    st = cb.status(conn, cfg)
    console.print(json.dumps(st, ensure_ascii=False, indent=2))
    rows = conn.execute(
        "SELECT ativo, status, conviccao, data_expiracao, origem_decisao FROM cerebro_decisions d WHERE id_analise = "
        "(SELECT MAX(id_analise) FROM cerebro_decisions WHERE ativo=d.ativo) ORDER BY ativo").fetchall()
    for r in rows:
        console.print(f"{r[0]:<7} {r[1]:<14} {r[2]:>4.0%}  até {r[3][:16]} UTC  {r[4] or ''}")
    return 0


def notify_command(args: argparse.Namespace) -> int:
    from ts.app.notify import send_telegram

    send_telegram(args.message)
    return 0


def deadman_command(args: argparse.Namespace) -> int:
    from ts.app.routines import deadman_routine

    return deadman_routine(date.fromisoformat(args.asof) if args.asof else None)


def weekly_report_command(args: argparse.Namespace) -> int:
    from ts.app.weekly_report import generate_weekly_report

    settings = load_settings()
    week_end = date.fromisoformat(args.asof) if args.asof else None
    text = generate_weekly_report(settings.system.db_path, week_end, send_telegram=not args.no_telegram)
    out = Path(settings.system.reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"weekly_{(week_end or date.today()).isoformat()}.md").write_text(text, encoding="utf-8")
    console.print(text)
    return 0


def sync_command(args: argparse.Namespace) -> int:
    """Synchronize market data bars."""
    from ts.core.lock import LockBusyError, ProcessLock

    settings = load_settings()
    setup_logging(settings.system.logs_dir, settings.logging.level, settings.logging.json_lines)
    asof_date = date.fromisoformat(args.asof) if args.asof else date.today()
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None

    console.print(f"[bold green]Starting sync for asof={asof_date} (full={args.full})...[/bold green]")
    try:
        with ProcessLock(settings.system.lock_path, timeout_s=600):
            report = sync_universe(asof=asof_date, tickers=tickers, full=args.full, settings=settings)
    except LockBusyError as e:
        console.print(f"[red]{e}[/red]")
        return 5
    except ConnectivityError as e:
        console.print(f"[red]Connectivity error: {e}[/red]")
        return 3

    console.print(
        f"[bold]Sync completed with status: {report.status}[/bold] (Success: {len(report.successful_tickers)}/{report.total_tickers})"
    )
    if report.failed_tickers:
        console.print(f"[yellow]Failed tickers: {report.failed_tickers}[/yellow]")
    if report.blocked_tickers:
        console.print(f"[red]Blocked tickers: {report.blocked_tickers}[/red]")

    return report.exit_code


def data_report_command(args: argparse.Namespace) -> int:
    """Display data coverage and quality report in a Rich table."""
    settings = load_settings()
    store = OhlcvStore(f"{settings.system.data_dir}/ohlcv/d1")

    today = date.today()
    active_tickers = universe_on(today)
    active_tickers.append(settings.mt5.ibov_symbol)

    table = Table(title="Data Quality & Store Coverage", show_lines=True)
    table.add_column("Ticker", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Bars Count", justify="right")
    table.add_column("First Date")
    table.add_column("Latest Date")

    for ticker in sorted(active_tickers):
        if store.exists(ticker):
            df = store.read(ticker)
            if not df.empty:
                cnt = len(df)
                min_d = str(df["session_date"].min())
                max_d = str(df["session_date"].max())
                table.add_row(ticker, "[green]OK[/green]", str(cnt), min_d, max_d)
            else:
                table.add_row(ticker, "[yellow]EMPTY[/yellow]", "0", "-", "-")
        else:
            table.add_row(ticker, "[red]MISSING[/red]", "0", "-", "-")

    console.print(table)
    return 0


def preopen_command(args: argparse.Namespace) -> int:
    """Run pre-market routine: reconcile positions and submit opening-auction orders."""
    from ts.app.routines import preopen_routine

    asof_date = date.fromisoformat(args.asof) if args.asof else None
    return preopen_routine(session_date=asof_date)


def eod_command(args: argparse.Namespace) -> int:
    """Run end-of-day routine: sync data, generate signals, plan orders, send report."""
    from ts.app.routines import eod_routine

    asof_date = date.fromisoformat(args.asof) if args.asof else None
    return eod_routine(session_date=asof_date)


def watchdog_command(args: argparse.Namespace) -> int:
    """Run watchdog: heartbeat, tick-age check, dead-man switch."""
    from ts.app.routines import watchdog_routine

    asof_date = date.fromisoformat(args.asof) if args.asof else None
    return watchdog_routine(session_date=asof_date)


def backup_command(args: argparse.Namespace) -> int:
    """Run backup routine: SQLite backup + Parquet integrity check."""
    from ts.app.routines import backup_routine

    asof_date = date.fromisoformat(args.asof) if args.asof else None
    return backup_routine(session_date=asof_date)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading System B3 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Doctor
    subparsers.add_parser("doctor", help="Run environment diagnostic check")

    # Sync
    sync_p = subparsers.add_parser("sync", help="Synchronize OHLCV daily data")
    sync_p.add_argument("--asof", type=str, help="Target session date (YYYY-MM-DD)")
    sync_p.add_argument("--tickers", type=str, help="Comma-separated tickers list")
    sync_p.add_argument("--full", action="store_true", help="Force full download from 2019")

    # Data Report
    subparsers.add_parser("data-report", help="Show data coverage report")

    # Pre-open routine
    preopen_p = subparsers.add_parser("preopen", help="Pre-market: reconcile and submit opening-auction orders")
    preopen_p.add_argument("--asof", type=str, help="Session date (YYYY-MM-DD, default: today)")

    # EOD routine
    eod_p = subparsers.add_parser("eod", help="End-of-day: sync, signals, risk, plan orders, Telegram report")
    eod_p.add_argument("--asof", type=str, help="Session date (YYYY-MM-DD, default: today)")

    # Watchdog
    watchdog_p = subparsers.add_parser("watchdog", help="Watchdog: heartbeat and dead-man switch")
    watchdog_p.add_argument("--asof", type=str, help="Session date (YYYY-MM-DD, default: today)")

    # Backup
    backup_p = subparsers.add_parser("backup", help="Backup SQLite and verify Parquet files")
    backup_p.add_argument("--asof", type=str, help="Session date (YYYY-MM-DD, default: today)")

    bt_p = subparsers.add_parser("backtest", help="Backtest on the Parquet store (use --g0 for the full G0 gate)")
    bt_p.add_argument("--start", type=str)
    bt_p.add_argument("--end", type=str)
    bt_p.add_argument("--g0", action="store_true", help="Walk-forward + PBO + DSR + plateau + regimes")
    bt_p.add_argument("--no-universe", action="store_true", help="Ignore historical IBX-100 composition")
    bt_p.add_argument("--regimes", action="store_true", help="Laboratório: o roteador de regime ajuda fora da amostra?")

    dm_p = subparsers.add_parser("deadman", help="20:30 dead-man switch: alert if EOD did not finish")
    dm_p.add_argument("--asof", type=str)

    wr_p = subparsers.add_parser("weekly-report", help="Weekly G1/G2 report")
    wr_p.add_argument("--asof", type=str, help="Week end (Friday)")
    wr_p.add_argument("--no-telegram", action="store_true")

    ca_p = subparsers.add_parser("corp-actions", help="Import dividends from Yahoo into corporate_actions.csv")
    ca_p.add_argument("--tickers", type=str)
    ca_p.add_argument("--start", type=str, default="2019-01-01")

    uu_p = subparsers.add_parser("universe-update", help="Baixa a carteira atual do IBX-100 da B3")
    uu_p.add_argument("--asof", type=str)

    subparsers.add_parser("version", help="Mostra a versão instalada e se há atualização")
    subparsers.add_parser("update", help="Atualiza agora a partir do GitHub (com testes e rollback)")

    ro_p = subparsers.add_parser("reoptimize", help="Reotimização da quarentena (dentro das travas)")
    ro_p.add_argument("--reason", type=str, default="")
    ro_p.add_argument("--regime", type=str, default="")

    cf_p = subparsers.add_parser("config", help="Candidatas da quarentena: status | approve ID | reject ID")
    cf_p.add_argument("action", choices=["status", "approve", "reject"])
    cf_p.add_argument("id", nargs="?", default="")
    cf_p.add_argument("--note", type=str, default="")

    ui_p = subparsers.add_parser("ui", help="Abre o terminal visual (janela desktop)")
    ui_p.add_argument("--port", type=int, default=8765)
    ui_p.add_argument("--browser", action="store_true", help="Abrir no navegador em vez da janela")

    nt_p = subparsers.add_parser("notify", help="Send a Telegram message (used by the updater)")
    nt_p.add_argument("message", type=str)

    cb_p = subparsers.add_parser("cerebro", help="Integração com o TradingAgents B3 (Cérebro fundamentalista)")
    cb_p.add_argument("action", choices=["status", "serve", "demand", "flush", "retry", "config"], nargs="?",
                      default="status")
    cb_p.add_argument("tickers", nargs="*", help="demand: ativos a analisar")
    cb_p.add_argument("--prioridade", choices=["normal", "alta", "urgente"])
    cb_p.add_argument("--url", help="config: endereço do Cérebro, ex. http://localhost:8000")
    cb_p.add_argument("--mode", choices=["required", "advisory"], help="config: required bloqueia; advisory só avisa")
    cb_p.add_argument("--port", type=int, help="config: porta do webhook (padrão 5000)")
    cb_p.add_argument("--host", help="config: 127.0.0.1 (só este PC) ou 0.0.0.0 (rede local)")
    cb_p.add_argument("--min-conviccao", type=float, dest="min_conviccao")
    cb_p.add_argument("--enable", action="store_true")
    cb_p.add_argument("--disable", action="store_true")

    args = parser.parse_args()

    if args.command == "cerebro":
        sys.exit(cerebro_command(args))
    if args.command == "corp-actions":
        sys.exit(corp_actions_command(args))
    if args.command == "universe-update":
        sys.exit(universe_update_command(args))
    if args.command == "version":
        sys.exit(version_command(args))
    if args.command == "update":
        sys.exit(update_command(args))
    if args.command == "reoptimize":
        sys.exit(reoptimize_command(args))
    if args.command == "config":
        sys.exit(config_command(args))
    if args.command == "ui":
        sys.exit(ui_command(args))
    if args.command == "notify":
        sys.exit(notify_command(args))
    if args.command == "backtest":
        sys.exit(backtest_command(args))
    if args.command == "deadman":
        sys.exit(deadman_command(args))
    if args.command == "weekly-report":
        sys.exit(weekly_report_command(args))
    if args.command == "doctor":
        sys.exit(doctor_command(args))
    elif args.command == "sync":
        sys.exit(sync_command(args))
    elif args.command == "data-report":
        sys.exit(data_report_command(args))
    elif args.command == "preopen":
        sys.exit(preopen_command(args))
    elif args.command == "eod":
        sys.exit(eod_command(args))
    elif args.command == "watchdog":
        sys.exit(watchdog_command(args))
    elif args.command == "backup":
        sys.exit(backup_command(args))


if __name__ == "__main__":
    main()
