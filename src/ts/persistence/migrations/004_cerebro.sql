-- Integração com o TradingAgents B3 ("Cérebro"): decisões recebidas pelo webhook e fila de saída
-- (pedidos de análise e resultados de trades) com reenvio.

CREATE TABLE IF NOT EXISTS cerebro_decisions (
    id_analise INTEGER PRIMARY KEY,           -- id único do Cérebro (deduplicação de reenvios)
    ativo TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('autorizado', 'rejeitado', 'pausa_analise')),
    conviccao REAL NOT NULL,
    validade_dias INTEGER NOT NULL,
    tese TEXT NOT NULL,
    indicadores_json TEXT NOT NULL,
    data_autorizacao TEXT NOT NULL,           -- UTC (ISO, sem fuso), como enviado pelo Cérebro
    data_expiracao TEXT NOT NULL,             -- UTC
    origem_decisao TEXT,
    fontes TEXT,
    received_at TEXT NOT NULL,                -- UTC
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cerebro_decisions_ativo ON cerebro_decisions (ativo, data_autorizacao);

CREATE TABLE IF NOT EXISTS cerebro_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('demand', 'feedback')),
    dedup_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    last_attempt_at TEXT,
    response_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_cerebro_outbox_due ON cerebro_outbox (status, next_attempt_at);
