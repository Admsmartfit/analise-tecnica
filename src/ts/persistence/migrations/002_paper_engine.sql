-- 002: paper engine persistence + wider CHECK constraints.
-- SQLite cannot ALTER a CHECK constraint, so `runs` and `signals` are rebuilt (12-step procedure).
PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE runs_new (
    run_id TEXT PRIMARY KEY,
    routine TEXT NOT NULL CHECK (routine IN ('sync', 'preopen', 'eod', 'watchdog', 'backtest', 'backup', 'deadman', 'weekly')),
    session_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'OK', 'FAILED', 'SKIPPED_HOLIDAY')),
    config_hash TEXT NOT NULL,
    code_version TEXT NOT NULL,
    error TEXT
);
INSERT INTO runs_new SELECT run_id, routine, session_date, started_at, finished_at, status, config_hash, code_version, error FROM runs;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;

CREATE TABLE signals_new (
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
    disposition TEXT NOT NULL CHECK (disposition IN ('ACCEPTED', 'REJECTED_HEAT', 'REJECTED_CLUSTER', 'REJECTED_REGIME',
        'REJECTED_COOLDOWN', 'REJECTED_SIZE', 'REJECTED_RR', 'SHADOW', 'REJECTED_CORR', 'REJECTED_BETA',
        'REJECTED_SETUP', 'REJECTED_DUPLICATE')),
    reason TEXT,
    config_hash TEXT,
    UNIQUE (decision_date, ticker, setup)
);
INSERT INTO signals_new (signal_id, run_id, decision_date, ticker, setup, trade_class, entry_type, entry_ref, stop_price,
                         target_price, features_json, disposition)
    SELECT signal_id, run_id, decision_date, ticker, setup, trade_class, entry_type, entry_ref, stop_price,
           target_price, features_json, disposition FROM signals;
DROP TABLE signals;
ALTER TABLE signals_new RENAME TO signals;

ALTER TABLE order_intents ADD COLUMN setup TEXT;
ALTER TABLE order_intents ADD COLUMN entry_kind TEXT;
ALTER TABLE order_intents ADD COLUMN is_shadow INTEGER NOT NULL DEFAULT 0;
ALTER TABLE order_intents ADD COLUMN config_hash TEXT;

ALTER TABLE positions ADD COLUMN highest_price REAL;
ALTER TABLE positions ADD COLUMN lowest_price REAL;
ALTER TABLE positions ADD COLUMN fees_brl REAL;
ALTER TABLE positions ADD COLUMN risk_per_share REAL;
ALTER TABLE positions ADD COLUMN is_shadow INTEGER NOT NULL DEFAULT 0;
ALTER TABLE positions ADD COLUMN signal_id TEXT;

-- Full engine state after each paper session (source of truth for the PaperBroker book).
CREATE TABLE IF NOT EXISTS book_snapshots (
    session_date TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    config_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_date, mode)
);

CREATE INDEX IF NOT EXISTS idx_order_events_cid ON order_events(client_order_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

COMMIT;
PRAGMA foreign_keys = ON;
