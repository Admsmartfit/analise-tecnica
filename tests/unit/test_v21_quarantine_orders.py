"""PRD v2.1 (adjusted): guardrails, quarantine, bracket orders, risk audit, re-optimization and approval."""
from __future__ import annotations

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from helpers import market

from ts.core.calendar import B3Calendar
from ts.core.config import CostsConfig, Settings, StrategyConfig, load_strategy_config, strategy_config_hash
from ts.core.errors import ConfigError, OrderRejectedError
from ts.core.guardrails import load_limits
from ts.core.models import OrderIntent, SetupId, Signal, TradeClass
from ts.data.fake_mt5 import FakeMT5
from ts.execution.bracket import build_preopen_package
from ts.execution.mt5_broker import MT5Broker
from ts.execution.session_engine import BookState, PendingOrder, SessionEngine
from ts.risk.protections import CircuitBreaker
from ts.risk.sizing import calculate_position_size

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def cfgdir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    for f in ("strategy.yaml", "strategy_default.yaml"):
        shutil.copy(REPO / "config" / f, d / f)
    return d


# ------------------------------------------------------------------------------------------- guardrails
def test_strategy_outside_limits_does_not_load(cfgdir: Path) -> None:
    raw = yaml.safe_load((cfgdir / "strategy.yaml").read_text(encoding="utf-8"))
    raw["risk"]["risk_per_trade_pct"] = 0.05  # 5% > max 1%
    (cfgdir / "strategy.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="risk_per_trade_pct"):
        load_strategy_config(cfgdir / "strategy.yaml")


def test_override_applied_stale_and_frozen(cfgdir: Path) -> None:
    base, h0 = load_strategy_config(cfgdir / "strategy.yaml")
    base_hash = h0
    ov = cfgdir / "strategy.override.yaml"
    ov.write_text(yaml.safe_dump({"base_hash": base_hash, "params": {"setups.B.rsi_max": 50}}), encoding="utf-8")
    cfg, h1 = load_strategy_config(cfgdir / "strategy.yaml")
    assert cfg.setups.B.rsi_max == 50 and h1 != h0
    ov.write_text(yaml.safe_dump({"base_hash": base_hash, "params": {"risk.risk_per_trade_pct": 0.005}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="congelado"):
        load_strategy_config(cfgdir / "strategy.yaml")
    ov.write_text(yaml.safe_dump({"base_hash": "outra-base", "params": {"setups.B.rsi_max": 50}}), encoding="utf-8")
    cfg, h2 = load_strategy_config(cfgdir / "strategy.yaml")
    assert cfg.setups.B.rsi_max == base.setups.B.rsi_max and h2 == h0  # stale override ignored


def test_repository_config_respects_its_own_limits() -> None:
    cfg, _ = load_strategy_config(REPO / "config" / "strategy.yaml")
    limits = load_limits(REPO / "config")
    assert limits is not None and limits.grid
    from ts.backtest.reoptimize import _grid_configs

    grid = _grid_configs(cfg, limits)
    assert len(grid) >= 2 and strategy_config_hash(grid[0][1]) == strategy_config_hash(cfg)


# ------------------------------------------------------------------------------------------- quarantine
def test_quarantine_by_loss_streak_and_resume() -> None:
    cb = CircuitBreaker(0.08, 0.12, 5, 100_000, 0.5, consecutive_losses_limit=3)
    for r in (-1.0, 0.5, -1.0, -1.0):
        cb.record_trade(r)
    assert not cb.is_halted
    cb.record_trade(-0.4)
    assert cb.is_halted and cb.just_halted and "3 perdas" in (cb.halt_reason or "")
    cb.resume(95_000)
    assert not cb.is_halted and cb.loss_streak == 0 and cb.peak_equity == 95_000


def test_quarantine_state_survives_serialization() -> None:
    cfg = StrategyConfig()
    st = BookState.new(100_000, cfg)
    st.breaker.record_trade(-1)
    st.breaker._halt("teste")
    back = BookState.from_dict(json.loads(json.dumps(st.to_dict())), cfg)
    assert back.breaker.is_halted and back.breaker.halt_reason == "teste" and back.breaker.loss_streak == 1


# ------------------------------------------------------------------------------------------- orders
def _engine(cfg: StrategyConfig) -> SessionEngine:
    return SessionEngine(cfg, CostsConfig(), strategy_config_hash(cfg), B3Calendar(), order_prefix="T")


def _sig(entry_type: str, setup: SetupId, tc: TradeClass) -> Signal:
    return Signal(f"{setup.value}-X", "PETR4", setup, tc, date(2024, 6, 27), entry_type, 30.0, 28.5, None,
                  {"atr14_raw": 1.0, "gap_p01": -0.02}, "h")


def test_entry_order_carries_risk_cap_and_planned_target() -> None:
    cfg = StrategyConfig()
    eng = _engine(cfg)
    st = BookState.new(100_000, cfg)
    sz = calculate_position_size(30.0, 28.5, 1.0, 100_000, cfg.risk)
    eng._queue_entry(st, _sig("OPEN_AUCTION", SetupId.A_PIVOT, TradeClass.TREND), sz, date(2024, 6, 27), date(2024, 6, 28), False, None)
    eng._queue_entry(st, _sig("BUY_STOP", SetupId.B_PULLBACK, TradeClass.MEAN_REVERSION), sz, date(2024, 6, 27), date(2024, 6, 28), False, None)
    auction, stop = st.pending
    worst = (auction.limit_price - auction.stop_price) * auction.qty
    assert worst <= auction.risk_budget + 1e-6  # RF-02: loss at the stop never above the budget
    assert auction.limit_price <= 30.0 * 1.03 + 1e-9 and auction.target_price is None
    assert stop.max_fill_price is not None and (stop.max_fill_price - stop.stop_price) * stop.qty <= stop.risk_budget + 1e-6
    assert stop.target_price == pytest.approx(30.0 + 3.0 * (30.0 - sz.stop_price), abs=0.011)


def test_gap_above_risk_cap_does_not_fill_and_partial_waits_a_day() -> None:
    bars, ibov, sessions = market(330)
    cfg = StrategyConfig()
    eng = _engine(cfg)
    from ts.strategy.signal_engine import prepare_ticker

    t = "AAAA3"
    prepared = {t: prepare_ticker(t, bars[t], cfg)}
    d = sessions[-5]
    k = prepared[t].index_of(d)
    o = float(prepared[t].view._cols["raw_open"][k])
    st = BookState.new(100_000, cfg)
    st.pending.append(PendingOrder("T1", None, t, "BUY", "BUY_STOP", 100, o * 0.95, None, o * 0.90, 3.0, "B",
                                   "MEAN_REVERSION", d, risk_budget=100.0, max_fill_price=o * 0.97))
    res = eng.run_session(st, d, prepared, market_ok=False)
    assert not res.fills and "teto de risco" in res.expired[0][1]

    st2 = BookState.new(100_000, cfg)
    entry, stop = o, o * 0.995  # tiny R so the entry bar's high easily reaches 1.5R
    st2.pending.append(PendingOrder("T2", None, t, "BUY", "OPEN_AUCTION", 200, o, o * 1.05, stop, 3.0, "B",
                                    "MEAN_REVERSION", d, risk_budget=10_000.0, max_fill_price=o * 1.05,
                                    target_price=entry * 1.5))
    res2 = eng.run_session(st2, d, prepared, market_ok=False)
    assert any(f.reason == "OPEN_AUCTION" for f in res2.fills)
    assert not any(f.reason == "PARTIAL" for f in res2.fills)  # partial order only exists from the next pre-open


def test_auction_open_below_stop_does_not_fill() -> None:
    """Regression (lab GARCH, BRAV3 26/10/2026): a gap down through the stop used to fill with risk ~0 -> R of -3e7."""
    bars, ibov, sessions = market(330)
    cfg = StrategyConfig()
    eng = _engine(cfg)
    from ts.strategy.signal_engine import prepare_ticker

    t = "AAAA3"
    prepared = {t: prepare_ticker(t, bars[t], cfg)}
    d = sessions[-5]
    o = float(prepared[t].view._cols["raw_open"][prepared[t].index_of(d)])
    st = BookState.new(100_000, cfg)
    st.pending.append(PendingOrder("T3", None, t, "BUY", "OPEN_AUCTION", 100, o, o * 1.05, o * 1.01, None, "A",
                                   "TREND", d, risk_budget=1_000.0, max_fill_price=o * 1.05))
    res = eng.run_session(st, d, prepared, market_ok=False)
    assert not res.fills and t not in st.positions and "abaixo do stop" in res.expired[0][1]


def test_preopen_package_bracket() -> None:
    cfg = StrategyConfig()
    eng = _engine(cfg)
    st = BookState.new(100_000, cfg)
    today = date(2024, 6, 28)
    sz = calculate_position_size(30.0, 28.5, 1.0, 100_000, cfg.risk)
    eng._queue_entry(st, _sig("OPEN_AUCTION", SetupId.A_PIVOT, TradeClass.TREND), sz, date(2024, 6, 27), today, False, None)
    eng._queue_entry(st, _sig("BUY_STOP", SetupId.E_DIVERGENCE, TradeClass.MEAN_REVERSION), sz, date(2024, 6, 27), today, False, None)
    pkg = build_preopen_package(st, today, cfg)
    a, b = pkg.entries
    assert a.order_type == "LIMIT" and a.stop_loss is not None and a.take_profit is None
    assert b.order_type == "STOP_LIMIT" and b.limit_price is not None and b.take_profit is not None
    for i in pkg.entries:
        MT5Broker.audit_entry(i)  # both pass the pre-send audit


def test_broker_rejects_naked_or_oversized_entries() -> None:
    base = {"client_order_id": "X1", "signal_id": None, "ticker": "PETR4", "side": "BUY", "qty": 100,
            "order_type": "LIMIT", "price": Decimal("30"), "stop_loss": Decimal("28"),
            "valid_until": date(2026, 1, 2), "reason": "t"}
    with pytest.raises(OrderRejectedError, match="sem stop"):
        MT5Broker.audit_entry(OrderIntent(**{**base, "stop_loss": None}))
    with pytest.raises(OrderRejectedError, match="preço máximo"):
        MT5Broker.audit_entry(OrderIntent(**{**base, "order_type": "MARKET"}))
    with pytest.raises(OrderRejectedError, match="risco"):
        MT5Broker.audit_entry(OrderIntent(**{**base, "risk_budget": Decimal("150")}))  # 2.00 x 100 = 200 > 150
    MT5Broker.audit_entry(OrderIntent(**{**base, "risk_budget": Decimal("200")}))


def test_stop_limit_request_and_sltp_sent_to_mt5() -> None:
    s = Settings()
    s.execution.mode = "live"
    fake = FakeMT5()
    fake.initialize()
    br = MT5Broker(fake, s, sleep=lambda x: None)
    intent = OrderIntent("X2", None, "PETR4", "BUY", 100, "STOP_LIMIT", Decimal("30"), Decimal("28"), date(2026, 1, 2),
                         "t", take_profit=Decimal("36"), limit_price=Decimal("30.5"), risk_budget=Decimal("300"))
    req = br._request(intent, 30.0)
    assert req["type"] == 6 and req["stoplimit"] == 30.5 and req["sl"] == 28.0 and req["tp"] == 36.0
    assert br.submit(intent).success


# ------------------------------------------------------------------------------------------- re-optimization
def test_reoptimize_runs_only_inside_limits(cfgdir: Path) -> None:
    from ts.backtest.reoptimize import reoptimize
    from ts.core.guardrails import Limits

    bars, ibov, _ = market(420)
    cfg = StrategyConfig()
    cfg.min_history_bars = 220
    cfg.market_filter.require_bull_regime = False
    limits = Limits(bounds={"setups.B.rsi_max": (40, 50)}, grid={"setups.B.rsi_max": [45, 50, 70]}, min_trades=5)
    res = reoptimize(bars, ibov, cfg, limits, CostsConfig(), plateau_variation=0.2)
    tested = sorted(t.params["setups.B.rsi_max"] for t in res.trials)
    assert tested == [45, 50]  # 70 is outside the guardrails and was never tested; 45 is the current config
    assert res.verdict in ("PENDING", "NONE_FOUND") and "Reotimização da quarentena" in res.report_markdown
    if res.candidate is not None:
        assert res.candidate.setups.B.rsi_max == 50


def test_approve_and_reject_flow(tmp_path: Path, cfgdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ts.app.autoheal as ah
    from ts.persistence.db import DatabaseManager

    monkeypatch.setattr(ah, "_notify", lambda m: None)
    s = Settings()
    s.system.db_path = str(tmp_path / "t.db")
    s.system.lock_path = str(tmp_path / "ts.lock")
    _, cur_hash = load_strategy_config(cfgdir / "strategy.yaml")
    db = DatabaseManager(s.system.db_path)
    conn = db.get_conn()
    for cid in ("C1", "C2"):
        conn.execute("INSERT INTO config_candidates (candidate_id, created_at, trigger_reason, market_regime, base_hash, "
                     "current_hash, status, params_json) VALUES (?, 'x', 'drawdown 12,5% >= 12%', 'IBOV SIDEWAYS', ?, ?, "
                     "'PENDING', ?)", (cid, cur_hash, cur_hash, json.dumps({"setups.B.rsi_max": 50})))
    conn.commit()
    db.close()
    ah.reject(s, "C2", config_dir=cfgdir)
    lines = ah.approve(s, "C1", config_dir=cfgdir)
    assert lines == ["[AUTO-HEAL] Param setups.B.rsi_max alterado de 45.0 para 50. Motivo: drawdown 12,5% >= 12%. "
                     "Regime identificado: IBOV SIDEWAYS"]
    cfg, new_hash = load_strategy_config(cfgdir / "strategy.yaml")
    assert cfg.setups.B.rsi_max == 50 and new_hash != cur_hash
    db2 = DatabaseManager(s.system.db_path)
    c2 = db2.get_conn()
    assert c2.execute("SELECT status FROM config_candidates WHERE candidate_id='C1'").fetchone()[0] == "APPROVED"
    assert c2.execute("SELECT status FROM config_candidates WHERE candidate_id='C2'").fetchone()[0] == "REJECTED"
    assert c2.execute("SELECT COUNT(*) FROM param_changes").fetchone()[0] == 1
    db2.close()
    with pytest.raises(ConfigError):
        ah.approve(s, "C1", config_dir=cfgdir)  # already decided
