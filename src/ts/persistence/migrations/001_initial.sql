CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    routine TEXT NOT NULL CHECK (routine IN ('sync', 'preopen', 'eod', 'watchdog', 'backtest')),
    session_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'OK', 'FAILED', 'SKIPPED_HOLIDAY')),
    config_hash TEXT NOT NULL,
    code_version TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    config_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    yaml TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    decision_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    setup TEXT NOT NULL,
    trade_class TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    entry_ref REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL,
    features_json TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('ACCEPTED', 'REJECTED_HEAT', 'REJECTED_CLUSTER', 'REJECTED_REGIME', 'REJECTED_COOLDOWN', 'REJECTED_SIZE', 'REJECTED_RR', 'SHADOW')),
    UNIQUE (decision_date, ticker, setup)
);

CREATE TABLE IF NOT EXISTS order_intents (
    client_order_id TEXT PRIMARY KEY,
    signal_id TEXT REFERENCES signals(signal_id),
    position_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    order_type TEXT NOT NULL,
    price REAL,
    stop_loss REAL,
    valid_until TEXT NOT NULL,
    reason TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL REFERENCES order_intents(client_order_id),
    ts_utc TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PLANNED', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED', 'UNKNOWN')),
    broker_order_id TEXT,
    retcode INTEGER,
    filled_qty INTEGER,
    avg_price REAL,
    fees REAL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    setup TEXT NOT NULL,
    trade_class TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'PARTIAL', 'CLOSED')),
    opened_on TEXT NOT NULL,
    closed_on TEXT,
    entry_price REAL NOT NULL,
    initial_stop REAL NOT NULL,
    current_stop REAL NOT NULL,
    target_price REAL,
    qty_initial INTEGER NOT NULL,
    qty_open INTEGER NOT NULL,
    risk_brl REAL NOT NULL,
    exit_reason TEXT CHECK (exit_reason IN ('STOP', 'TARGET', 'TRAILING', 'TIME', 'STAGNATION', 'BREAKEVEN', 'MANUAL', 'CIRCUIT_BREAKER')),
    pnl_brl REAL,
    r_multiple REAL,
    mae_r REAL,
    mfe_r REAL,
    bars_held INTEGER
);

CREATE TABLE IF NOT EXISTS equity_daily (
    session_date TEXT NOT NULL,
    curve TEXT NOT NULL CHECK (curve IN ('REAL', 'PAPER', 'SHADOW')),
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    heat_pct REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    PRIMARY KEY (session_date, curve)
);

CREATE TABLE IF NOT EXISTS protections (
    scope TEXT NOT NULL CHECK (scope IN ('TICKER', 'SETUP', 'PORTFOLIO')),
    key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('COOLDOWN', 'SETUP_SUSPENDED', 'DD_LEVEL1', 'DD_LEVEL2', 'REGIME_BLOCK')),
    active_from TEXT NOT NULL,
    active_until TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY (scope, key, kind, active_from)
);

CREATE TABLE IF NOT EXISTS data_quality (
    session_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    check_name TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARN', 'BLOCK')),
    detail TEXT,
    PRIMARY KEY (session_date, ticker, check_name)
);
