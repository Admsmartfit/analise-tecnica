-- 003: quarantine re-optimization candidates and [AUTO-HEAL] audit trail (PRD v2.1, adjusted version).
CREATE TABLE IF NOT EXISTS config_candidates (
    candidate_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    market_regime TEXT,
    base_hash TEXT NOT NULL,
    current_hash TEXT NOT NULL,
    candidate_hash TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PENDING', 'APPROVED', 'REJECTED', 'NONE_FOUND', 'FAILED')),
    params_json TEXT,
    summary_json TEXT,
    report_path TEXT,
    decided_at TEXT,
    decision_note TEXT
);

-- Somente INSERT: toda mudança de parâmetro aplicada, com o motivo.
CREATE TABLE IF NOT EXISTS param_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    candidate_id TEXT REFERENCES config_candidates(candidate_id),
    param TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT NOT NULL
);
