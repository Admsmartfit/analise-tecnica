"""Streamlit read-only dashboard for the Trading System B3.

Launch with:
    streamlit run src/ts/app/dashboard.py

Reads trading.db in read-only mode so it never conflicts with the live trading process.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

try:
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

# ---------------------------------------------------------------------------
# DB helpers (read-only URI)
# ---------------------------------------------------------------------------


def _db_conn(db_path: str) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


def _load_equity(db_path: str, curve: str = "PAPER", days: int = 90) -> pd.DataFrame:
    since = (date.today() - timedelta(days=days)).isoformat()
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT session_date, equity, cash, heat_pct, drawdown_pct FROM equity_daily "
            "WHERE curve=? AND session_date >= ? ORDER BY session_date",
            conn,
            params=(curve, since),
        )
    return df


def _load_positions(db_path: str) -> pd.DataFrame:
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT ticker, setup, trade_class, opened_on, entry_price, current_stop, "
            "qty_open, risk_brl, target_price FROM positions WHERE status IN ('OPEN','PARTIAL') AND is_shadow=0 "
            "ORDER BY opened_on DESC",
            conn,
        )
    return df


def _load_recent_trades(db_path: str, limit: int = 50) -> pd.DataFrame:
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT ticker, setup, trade_class, opened_on, closed_on, entry_price, "
            "pnl_brl, r_multiple, exit_reason, bars_held "
            "FROM positions WHERE status='CLOSED' AND is_shadow=0 ORDER BY closed_on DESC LIMIT ?",
            conn,
            params=(limit,),
        )
    return df


def _load_data_quality(db_path: str, limit: int = 100) -> pd.DataFrame:
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT session_date, ticker, check_name, severity, detail FROM data_quality "
            "WHERE severity IN ('WARN','BLOCK') ORDER BY session_date DESC LIMIT ?",
            conn,
            params=(limit,),
        )
    return df


def _load_last_runs(db_path: str, limit: int = 20) -> pd.DataFrame:
    with _db_conn(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT routine, session_date, started_at, status, error FROM runs "
            "ORDER BY started_at DESC LIMIT ?",
            conn,
            params=(limit,),
        )
    return df


# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------


def main() -> None:
    if not HAS_STREAMLIT:
        print("Streamlit is not installed. Run: pip install streamlit plotly")
        return

    st.set_page_config(
        page_title="Trading System B3 — Dashboard",
        page_icon="📈",
        layout="wide",
    )

    st.title("📈 Trading System B3 — Paper Dashboard")
    st.caption(f"Last refreshed: {date.today().isoformat()} (read-only)")

    # Sidebar — config
    from ts.core.config import load_settings
    settings = load_settings()
    db_path = settings.system.db_path

    st.sidebar.header("Settings")
    curve = st.sidebar.selectbox("Equity Curve", ["PAPER", "SHADOW"], index=0)
    days = st.sidebar.slider("History (days)", 30, 365, 90)

    if st.sidebar.button("🔄 Refresh"):
        st.rerun()

    # Row 1 — KPI cards
    col1, col2, col3, col4 = st.columns(4)

    try:
        eq_df = _load_equity(db_path, curve, days)
        if not eq_df.empty:
            latest = eq_df.iloc[-1]
            col1.metric("Equity", f"R$ {latest['equity']:,.0f}")
            col2.metric("Max Drawdown", f"{eq_df['drawdown_pct'].max():.1%}")
            col3.metric("Heat %", f"{latest['heat_pct']:.1%}")
            col4.metric("Sessions", str(len(eq_df)))
        else:
            col1.info("No equity data yet")
    except Exception as exc:
        col1.error(f"DB error: {exc}")
        eq_df = None

    # Row 2 — Equity curve chart
    st.subheader("Equity Curve")
    if eq_df is not None and not eq_df.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=eq_df["session_date"],
            y=eq_df["equity"],
            mode="lines",
            name="Equity",
            line={"color": "#00cc44", "width": 2},
        ))
        fig.add_trace(go.Scatter(
            x=eq_df["session_date"],
            y=[eq_df["equity"].max()] * len(eq_df),
            mode="lines",
            name="Peak",
            line={"color": "gray", "width": 1, "dash": "dot"},
        ))
        fig.update_layout(height=300, margin={"t": 10, "b": 10})
        st.plotly_chart(fig, use_container_width=True)

        # Drawdown chart
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=eq_df["session_date"],
            y=-eq_df["drawdown_pct"] * 100,
            fill="tozeroy",
            mode="lines",
            name="Drawdown %",
            line={"color": "#ff4444"},
        ))
        fig2.update_layout(height=180, yaxis_title="%", margin={"t": 5, "b": 5})
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No equity data to display")

    # Row 3 — Open positions + recent trades side by side
    col_pos, col_trades = st.columns(2)

    with col_pos:
        st.subheader("Open Positions")
        try:
            pos_df = _load_positions(db_path)
            if pos_df.empty:
                st.info("No open positions")
            else:
                st.dataframe(pos_df, use_container_width=True, height=250)
        except Exception as exc:
            st.error(f"Error loading positions: {exc}")

    with col_trades:
        st.subheader("Recent Trades")
        try:
            trades_df = _load_recent_trades(db_path, 30)
            if trades_df.empty:
                st.info("No closed trades yet")
            else:
                # Colour-code by pnl
                def highlight_pnl(val: float) -> str:
                    if isinstance(val, int | float):
                        return "color: green" if val > 0 else "color: red"
                    return ""

                st.dataframe(
                    trades_df.style.map(highlight_pnl, subset=["pnl_brl", "r_multiple"]),
                    use_container_width=True,
                    height=250,
                )
        except Exception as exc:
            st.error(f"Error loading trades: {exc}")

    # Row 4 — Data quality issues
    st.subheader("Data Quality Issues")
    try:
        dq_df = _load_data_quality(db_path)
        if dq_df.empty:
            st.success("No data quality issues")
        else:
            st.dataframe(dq_df, use_container_width=True)
    except Exception as exc:
        st.error(f"Error loading data quality: {exc}")

    # Row 5 — Routine run history
    st.subheader("Routine Run History")
    try:
        runs_df = _load_last_runs(db_path)
        if runs_df.empty:
            st.info("No routine runs recorded yet")
        else:
            def colour_status(val: str) -> str:
                colours = {"OK": "color: green", "FAILED": "color: red", "RUNNING": "color: orange", "SKIPPED_HOLIDAY": "color: gray"}
                return colours.get(val, "")

            st.dataframe(
                runs_df.style.map(colour_status, subset=["status"]),
                use_container_width=True,
            )
    except Exception as exc:
        st.error(f"Error loading runs: {exc}")


if __name__ == "__main__":
    main()
