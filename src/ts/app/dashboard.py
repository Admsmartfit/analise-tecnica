"""Painel do Trading System B3 (Streamlit).

Abrir: atalho "Trading System B3" ou ``streamlit run src/ts/app/dashboard.py`` na pasta do sistema.

O painel só LÊ o banco (``mode=ro``) e os Parquet; as ações (rodar EOD, sync, backtest, atualizar) são
executadas como processos separados da CLI, que usam o mesmo lock das rotinas agendadas.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st
    from plotly.subplots import make_subplots

    HAS_STREAMLIT = True
except ImportError:  # pragma: no cover
    HAS_STREAMLIT = False

ROOT = Path(os.environ.get("TS_ROOT") or Path(__file__).resolve().parents[3])
SETUP_NAMES = {"A": "A · Pivô de alta", "B": "B · Pullback", "C": "C · Donchian 20/10",
               "D": "D · Bollinger squeeze", "E": "E · Divergência"}
DISPOSITION_PT = {
    "ACCEPTED": "✅ Aceito", "SHADOW": "👻 Sombra (circuit breaker)", "REJECTED_HEAT": "Heat da carteira",
    "REJECTED_CLUSTER": "Cluster cheio", "REJECTED_CORR": "Risco correlacionado", "REJECTED_BETA": "Beta da carteira",
    "REJECTED_REGIME": "Regime de volatilidade", "REJECTED_COOLDOWN": "Ativo em cooldown",
    "REJECTED_SETUP": "Setup suspenso", "REJECTED_SIZE": "Tamanho/stop inválido", "REJECTED_RR": "Retorno/risco < 3",
    "REJECTED_DUPLICATE": "Já posicionado",
}


# =============================================================================================== dados
def _db_path() -> Path:
    from ts.core.config import load_settings

    p = Path(load_settings().system.db_path)
    return p if p.is_absolute() else ROOT / p


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    p = _db_path()
    if not p.exists():
        return pd.DataFrame()
    with closing(sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True, check_same_thread=False)) as conn:
        try:
            return pd.read_sql_query(sql, conn, params=params)
        except Exception:  # noqa: BLE001 - tabela ainda não existe
            return pd.DataFrame()


def _store():  # type: ignore[no-untyped-def]
    from ts.core.config import load_settings
    from ts.data.ohlcv_store import OhlcvStore

    s = load_settings()
    d = Path(s.system.data_dir)
    return OhlcvStore((d if d.is_absolute() else ROOT / d) / "ohlcv" / "d1"), s


@st.cache_data(ttl=600, show_spinner=False)
def _available_tickers() -> list[str]:
    store, s = _store()
    return sorted(p.stem for p in store.data_dir.glob("*.parquet"))


@st.cache_data(ttl=600, show_spinner=False)
def _features(ticker: str, mtime: float) -> tuple[pd.DataFrame, list[dict]]:
    """Série ajustada + indicadores + pivôs confirmados (mesmo código usado pelos sinais)."""
    from ts.core.config import load_strategy_config
    from ts.data.corporate_actions import CorporateActionsStore
    from ts.data.loader import load_signal_bars
    from ts.indicators.pivots import detect_pivots
    from ts.strategy.features import compute_features

    store, s = _store()
    cfg, _ = load_strategy_config()
    corp_path = Path(s.system.corporate_actions_path)
    corp = CorporateActionsStore(corp_path if corp_path.is_absolute() else ROOT / corp_path)
    bars = load_signal_bars(store, [ticker], corp).get(ticker)
    if bars is None or bars.empty:
        return pd.DataFrame(), []
    feat = compute_features(bars, cfg)
    piv = detect_pivots(feat, ticker, cfg.pivot.window_n, cfg.pivot.magnitude_atr_k, cfg.pivot.atr_period)
    return feat, [{"date": p.pivot_date, "price": p.price, "kind": p.kind} for p in piv]


def _run_cli(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}  # filho escreve UTF-8 (sem "�")
    proc = subprocess.run([sys.executable, "-m", "ts", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]


def _last_closed_session() -> date:
    """Última sessão com barra D1 fechada (decidir com barra em formação violaria o PRD 1.1.1)."""
    from ts.core.calendar import B3Calendar

    cal = B3Calendar()
    now = datetime.now()
    today = now.date()
    if cal.is_session(today):
        close = datetime.combine(today, cal.close_time(today)) + timedelta(minutes=45)
        if now >= close:
            return today
    return cal.previous_session(today)


# =============================================================================================== UI
def _sidebar() -> None:
    from ts.core.config import load_settings

    s = load_settings()
    st.sidebar.title("📈 Trading System B3")
    st.sidebar.caption(f"Modo **{s.execution.mode.upper()}** · dados: **{s.data.provider}**")
    runs = _query("SELECT routine, session_date, status, finished_at FROM runs WHERE routine='eod' "
                  "ORDER BY started_at DESC LIMIT 1")
    if runs.empty:
        st.sidebar.info("Nenhum EOD rodou ainda. Ele roda sozinho às 19:30 em dias úteis.")
    else:
        r = runs.iloc[0]
        icon = {"OK": "🟢", "FAILED": "🔴", "RUNNING": "🟡", "SKIPPED_HOLIDAY": "⚪"}.get(r["status"], "⚪")
        st.sidebar.caption(f"Último EOD: {icon} {r['session_date']} · {r['status']}")
    if st.sidebar.button("🔄 Recarregar dados"):
        st.cache_data.clear()
        st.rerun()
    _render_updates()


def _tab_overview() -> None:
    eq = _query("SELECT session_date, equity, cash, heat_pct, drawdown_pct FROM equity_daily "
                "WHERE curve='PAPER' ORDER BY session_date")
    sh = _query("SELECT session_date, equity FROM equity_daily WHERE curve='SHADOW' ORDER BY session_date")
    if eq.empty:
        st.info(
            "**O paper trading ainda não começou.** A rotina de fim de dia (EOD) roda sozinha às 19:30 em dias úteis: "
            "baixa as cotações, gera os sinais, simula as ordens e preenche esta tela.\n\n"
            "Enquanto isso, veja os preços e indicadores na aba **Gráficos**, ou rode o primeiro EOD agora "
            "(usa o último pregão fechado)."
        )
        _eod_button()
        return
    from ts.core.config import load_settings

    cap = load_settings().execution.reference_capital
    last = eq.iloc[-1]
    c = st.columns(5)
    c[0].metric("Patrimônio", f"R$ {last['equity']:,.0f}", f"{last['equity'] / cap - 1:+.2%}")
    c[1].metric("Caixa", f"R$ {last['cash']:,.0f}")
    c[2].metric("Drawdown atual", f"{last['drawdown_pct']:.1%}")
    c[3].metric("Heat (risco aberto)", f"{last['heat_pct']:.2%}")
    n_open = _query("SELECT COUNT(*) n FROM positions WHERE status IN ('OPEN','PARTIAL') AND is_shadow=0")
    c[4].metric("Posições abertas", int(n_open["n"].iloc[0]) if not n_open.empty else 0)

    if len(eq) < 2:
        st.info(f"Paper iniciado em {pd.to_datetime(eq['session_date'].iloc[0]):%d/%m/%Y} com "
                f"R$ {eq['equity'].iloc[0]:,.0f}. A curva de patrimônio aparece a partir do 2º pregão processado."
                .replace(",", "."))
        _eod_button()
        return
    eq["session_date"] = pd.to_datetime(eq["session_date"])
    if not sh.empty:
        sh["session_date"] = pd.to_datetime(sh["session_date"])
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28], vertical_spacing=0.04)
    fig.add_trace(go.Scatter(x=eq["session_date"], y=eq["equity"], name="Patrimônio (paper)", line={"width": 2}),
                  row=1, col=1)
    fig.update_yaxes(tickformat=",.0f", row=1, col=1)
    fig.update_xaxes(tickformat="%d/%m/%Y", rangebreaks=[{"bounds": ["sat", "mon"]}])
    if not sh.empty:
        fig.add_trace(go.Scatter(x=sh["session_date"], y=sh["equity"], name="Curva sombra",
                                 line={"width": 1, "dash": "dot"}), row=1, col=1)
    fig.add_trace(go.Scatter(x=eq["session_date"], y=-eq["drawdown_pct"] * 100, name="Drawdown %",
                             fill="tozeroy", line={"color": "#e45756"}), row=2, col=1)
    fig.update_layout(height=520, margin={"t": 20, "b": 10}, legend={"orientation": "h", "y": 1.05})
    st.plotly_chart(fig, width="stretch")
    _eod_button()


def _eod_button() -> None:
    d = _last_closed_session()
    done = _query("SELECT 1 FROM book_snapshots WHERE session_date >= ? LIMIT 1", (d.isoformat(),))
    if not done.empty:
        st.caption(f"O pregão {d:%d/%m/%Y} já foi processado. O próximo EOD roda sozinho às 19:30 do próximo dia útil.")
        return
    if st.button(f"▶️ Rodar EOD agora (pregão {d:%d/%m/%Y})"):
        with st.spinner("Rodando EOD: sync, sinais, simulação paper..."):
            code, out = _run_cli(["eod", "--asof", d.isoformat()])
        st.cache_data.clear()
        (st.success if code == 0 else st.error)(f"EOD terminou com código {code}.")
        with st.expander("Saída"):
            st.code(out)


def _tab_charts() -> None:
    tickers = _available_tickers()
    if not tickers:
        st.warning("Sem cotações gravadas. Rode a carga inicial:")
        if st.button("⬇️ Baixar cotações desde 2019"):
            with st.spinner("Baixando (1–3 min)..."):
                code, out = _run_cli(["sync", "--full"])
            st.cache_data.clear()
            (st.success if code == 0 else st.error)(f"Sync terminou com código {code}.")
            st.code(out)
        return
    from ts.core.config import load_settings

    ibov = load_settings().mt5.ibov_symbol.upper()
    c1, c2, c3 = st.columns([2, 2, 3])
    default = tickers.index("PETR4") if "PETR4" in tickers else 0
    ticker = c1.selectbox("Ativo", tickers, index=default)
    period = c2.selectbox("Período", ["3 meses", "6 meses", "1 ano", "2 anos", "5 anos", "Tudo"], index=2)
    overlays = c3.multiselect("Indicadores", ["SMA 200", "SMA 50", "EMA 20", "Bollinger", "Pivôs", "Sinais"],
                              default=["SMA 200", "SMA 50", "EMA 20", "Pivôs", "Sinais"])

    store, _ = _store()
    fpath = store.data_dir / f"{ticker}.parquet"
    feat, pivots = _features(ticker, fpath.stat().st_mtime if fpath.exists() else 0.0)
    if feat.empty:
        st.warning("Sem dados para este ativo.")
        return
    days = {"3 meses": 63, "6 meses": 126, "1 ano": 252, "2 anos": 504, "5 anos": 1260}.get(period, len(feat))
    v = feat.tail(days)
    x0 = v["session_date"].iloc[0]

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.62, 0.16, 0.22], vertical_spacing=0.025,
                        subplot_titles=("", "Volume", "IFR 14"))
    fig.add_trace(go.Candlestick(x=v["session_date"], open=v["open"], high=v["high"], low=v["low"], close=v["close"],
                                 name=ticker, increasing_line_color="#26a69a", decreasing_line_color="#ef5350"),
                  row=1, col=1)
    lines = {"SMA 200": ("sma_trend", "#7f7f7f"), "SMA 50": ("sma_b", "#1f77b4"), "EMA 20": ("ema_b", "#ff7f0e")}
    for name, (col, color) in lines.items():
        if name in overlays:
            fig.add_trace(go.Scatter(x=v["session_date"], y=v[col], name=name, line={"width": 1.3, "color": color}),
                          row=1, col=1)
    if "Bollinger" in overlays:
        lower = 2 * v["bb_mid"] - v["bb_upper"]
        fig.add_trace(go.Scatter(x=v["session_date"], y=v["bb_upper"], name="Bollinger sup.",
                                 line={"width": 0.8, "color": "#9467bd"}), row=1, col=1)
        fig.add_trace(go.Scatter(x=v["session_date"], y=lower, name="Bollinger inf.", fill="tonexty",
                                 fillcolor="rgba(148,103,189,0.08)", line={"width": 0.8, "color": "#9467bd"}), row=1, col=1)
    if "Pivôs" in overlays and pivots:
        pv = pd.DataFrame(pivots)
        pv = pv[pv["date"] >= x0]
        for kind, sym, color in (("HIGH", "triangle-down", "#d62728"), ("LOW", "triangle-up", "#2ca02c")):
            sub = pv[pv["kind"] == kind]
            fig.add_trace(go.Scatter(x=sub["date"], y=sub["price"], mode="markers", name=f"Pivô {'topo' if kind == 'HIGH' else 'fundo'}",
                                     marker={"symbol": sym, "size": 9, "color": color}), row=1, col=1)
    if "Sinais" in overlays:
        sig = _query("SELECT decision_date, setup, entry_ref, disposition FROM signals WHERE ticker=? ORDER BY decision_date",
                     (ticker,))
        if not sig.empty:
            sig["decision_date"] = pd.to_datetime(sig["decision_date"]).dt.date
            sig = sig[sig["decision_date"] >= x0]
            fig.add_trace(go.Scatter(
                x=sig["decision_date"], y=sig["entry_ref"], mode="markers+text", name="Sinais",
                text=sig["setup"], textposition="top center",
                marker={"symbol": "star", "size": 13, "color": ["#ffd700" if d == "ACCEPTED" else "#aaaaaa" for d in sig["disposition"]]},
            ), row=1, col=1)
    colors = ["#26a69a" if c >= o else "#ef5350" for o, c in zip(v["open"], v["close"], strict=True)]
    fig.add_trace(go.Bar(x=v["session_date"], y=v["real_volume"], name="Volume", marker_color=colors, showlegend=False),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=v["session_date"], y=v["rsi_b"], name="IFR 14", line={"color": "#8c564b"}), row=3, col=1)
    for lvl in (30, 70):
        fig.add_hline(y=lvl, line={"width": 0.6, "dash": "dot", "color": "#888"}, row=3, col=1)
    fig.update_layout(height=780, xaxis_rangeslider_visible=False, margin={"t": 30, "b": 10},
                      legend={"orientation": "h", "y": 1.03})
    fig.update_xaxes(rangebreaks=[{"bounds": ["sat", "mon"]}])
    st.plotly_chart(fig, width="stretch")

    last = v.iloc[-1]
    c = st.columns(6)
    c[0].metric("Fechamento", f"{last['raw_close']:.2f}", f"{last['ret1']:+.2%}")
    c[1].metric("Tendência (C > SMA200)", "Alta" if last["close"] > last["sma_trend"] else "Baixa")
    c[2].metric("ADX", f"{last['adx']:.1f}")
    c[3].metric("IFR 14", f"{last['rsi_b']:.1f}")
    c[4].metric("Volume relativo", f"{last['rvol']:.2f}x")
    c[5].metric("ATR 14", f"{last['atr14']:.2f}")
    if ticker != ibov:
        st.caption(f"Última barra: {last['session_date']}. Preços ajustados por proventos (série usada pelos sinais).")


def _tab_signals() -> None:
    sig = _query("SELECT decision_date AS data, ticker AS ativo, setup, entry_type AS entrada, entry_ref AS preco_ref, "
                 "stop_price AS stop, target_price AS alvo, disposition, reason AS motivo "
                 "FROM signals ORDER BY decision_date DESC, ticker LIMIT 500")
    st.subheader("Ordens planejadas para o próximo pregão")
    orders = _query(
        "SELECT oi.valid_until AS valido_ate, oi.ticker AS ativo, oi.side AS lado, oi.qty AS qtd, oi.entry_kind AS tipo, "
        "oi.price AS preco, oi.stop_loss AS stop, oi.reason AS motivo, "
        "(SELECT state FROM order_events e WHERE e.client_order_id = oi.client_order_id ORDER BY event_id DESC LIMIT 1) AS estado "
        "FROM order_intents oi WHERE oi.is_shadow = 0 ORDER BY oi.created_at DESC LIMIT 100")
    if orders.empty:
        st.info("Nenhuma ordem planejada ainda.")
    else:
        active = orders[orders["estado"].isin(["PLANNED", "SUBMITTED", "ACCEPTED"])]
        st.dataframe(active if not active.empty else orders.head(20), width="stretch", hide_index=True)
    st.subheader("Sinais gerados")
    if sig.empty:
        st.info("Nenhum sinal ainda. Os sinais aparecem depois do primeiro EOD.")
        return
    sig["setup"] = sig["setup"].map(SETUP_NAMES).fillna(sig["setup"])
    sig["resultado"] = sig.pop("disposition").map(DISPOSITION_PT).fillna("")
    only = st.checkbox("Só aceitos", value=False)
    st.dataframe(sig[sig["resultado"].str.startswith("✅")] if only else sig, width="stretch", hide_index=True)


def _tab_positions() -> None:
    st.subheader("Posições abertas")
    pos = _query("SELECT ticker AS ativo, setup, opened_on AS entrada_em, entry_price AS preco_entrada, "
                 "current_stop AS stop_atual, target_price AS alvo, qty_open AS qtd, risk_brl AS risco_inicial, "
                 "bars_held AS pregoes, mfe_r AS mfe_R FROM positions WHERE status IN ('OPEN','PARTIAL') AND is_shadow=0 "
                 "ORDER BY opened_on DESC")
    if pos.empty:
        st.info("Sem posições abertas.")
    else:
        st.dataframe(pos, width="stretch", hide_index=True)
    st.subheader("Operações encerradas")
    tr = _query("SELECT ticker AS ativo, setup, opened_on AS entrada, closed_on AS saida, entry_price AS preco_entrada, "
                "pnl_brl AS resultado_liq, r_multiple AS R, exit_reason AS motivo_saida, bars_held AS pregoes "
                "FROM positions WHERE status='CLOSED' AND is_shadow=0 ORDER BY closed_on DESC")
    if tr.empty:
        st.info("Nenhuma operação encerrada ainda.")
        return
    c = st.columns(4)
    c[0].metric("Operações", len(tr))
    c[1].metric("Taxa de acerto", f"{(tr['resultado_liq'] > 0).mean():.0%}")
    c[2].metric("Esperança (R)", f"{tr['R'].mean():+.2f}")
    c[3].metric("Resultado líquido", f"R$ {tr['resultado_liq'].sum():,.0f}")
    st.plotly_chart(go.Figure(go.Histogram(x=tr["R"], nbinsx=30, marker_color="#4c78a8")).update_layout(
        height=260, title="Distribuição dos resultados em R", margin={"t": 40, "b": 10}), width="stretch")
    st.dataframe(tr, width="stretch", hide_index=True)


def _tab_backtest() -> None:
    reports = ROOT / "reports"
    files = sorted(reports.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if reports.exists() else []
    c1, c2 = st.columns(2)
    if c1.button("▶️ Rodar backtest (5+ anos, ~2–5 min)"):
        with st.spinner("Rodando backtest com o mesmo código do paper..."):
            code, out = _run_cli(["backtest"], timeout=3600)
        (st.success if code == 0 else st.error)(f"Backtest terminou com código {code}.")
        st.rerun()
    if c2.button("🧪 Portão G0 completo (walk-forward, PBO, platô — demorado)"):
        with st.spinner("Rodando G0 (pode levar bastante tempo)..."):
            code, out = _run_cli(["backtest", "--g0"], timeout=6 * 3600)
        (st.success if code == 0 else st.error)(f"G0 terminou com código {code}.")
        st.rerun()
    if not files:
        st.info("Nenhum relatório ainda. Rode um backtest acima.")
        return
    pick = st.selectbox("Relatório", files, format_func=lambda p: p.name)
    st.markdown(pick.read_text(encoding="utf-8"))


def _tab_health() -> None:
    st.subheader("Execuções das rotinas")
    runs = _query("SELECT routine AS rotina, session_date AS pregao, started_at AS inicio, status, error AS erro "
                  "FROM runs ORDER BY started_at DESC LIMIT 50")
    st.dataframe(runs, width="stretch", hide_index=True) if not runs.empty else st.info("Sem execuções.")
    st.subheader("Qualidade dos dados (avisos e bloqueios)")
    dq = _query("SELECT session_date AS data, ticker AS ativo, check_name AS checagem, severity AS severidade, detail AS detalhe "
                "FROM data_quality WHERE severity IN ('WARN','BLOCK') ORDER BY session_date DESC LIMIT 200")
    st.dataframe(dq, width="stretch", hide_index=True) if not dq.empty else st.success("Sem problemas.")
    st.subheader("Cobertura das cotações")
    store, _ = _store()
    rows = []
    for t in _available_tickers():
        df = store.read(t)
        if not df.empty:
            rows.append({"ativo": t, "barras": len(df), "primeira": df["session_date"].min(), "ultima": df["session_date"].max()})
    if rows:
        cov = pd.DataFrame(rows)
        stale = cov[cov["ultima"] < _last_closed_session()]
        st.caption(f"{len(cov)} ativos · {len(stale)} sem a barra do último pregão fechado")
        st.dataframe(cov, width="stretch", hide_index=True)


def _render_updates() -> None:
    """Barra lateral: versão instalada x disponível e botão de atualizar."""
    from ts.app.version import check_version, run_update

    st.sidebar.divider()
    st.sidebar.subheader("Atualizações")

    @st.cache_data(ttl=300, show_spinner=False)
    def _cached_check() -> dict:
        v = check_version()
        return {"installed": v.installed, "date": v.installed_date, "available": v.available,
                "pending": v.pending, "error": v.error, "update": v.update_available}

    if st.sidebar.button("Verificar agora"):
        _cached_check.clear()
    info = _cached_check()
    if info["error"]:
        st.sidebar.warning(f"Não foi possível verificar: {info['error']}")
        return
    st.sidebar.caption(f"Instalada: `{(info['installed'] or '')[:7]}` ({info['date']})")
    if not info["update"]:
        st.sidebar.success("Você está na última versão.")
        return
    st.sidebar.info(f"Nova versão disponível: `{(info['available'] or '')[:7]}`")
    for line in info["pending"][:10]:
        st.sidebar.markdown(f"- {line}")
    if st.sidebar.button("⬇️ Atualizar agora", type="primary"):
        with st.spinner("Atualizando e rodando os testes (1–3 min)..."):
            code, msg = run_update()
        _cached_check.clear()
        if code == 0:
            st.sidebar.success(msg + " Recarregue a página (F5).")
        else:
            st.sidebar.error(msg)


def main() -> None:
    if not HAS_STREAMLIT:  # pragma: no cover
        print("Streamlit não está instalado. Rode: pip install streamlit plotly")
        return
    st.set_page_config(page_title="Trading System B3", page_icon="📈", layout="wide")
    _sidebar()
    tabs = st.tabs(["🏠 Visão geral", "📊 Gráficos", "🎯 Sinais e ordens", "💼 Posições e operações",
                    "🧪 Backtest", "🩺 Saúde do sistema"])
    with tabs[0]:
        _tab_overview()
    with tabs[1]:
        _tab_charts()
    with tabs[2]:
        _tab_signals()
    with tabs[3]:
        _tab_positions()
    with tabs[4]:
        _tab_backtest()
    with tabs[5]:
        _tab_health()


if __name__ == "__main__":
    main()

