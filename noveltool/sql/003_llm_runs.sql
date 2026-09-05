CREATE TABLE llm_runs (
    id TEXT PRIMARY KEY NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed','cancelled')),
    request_json TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    finish_reason TEXT,
    usage_json TEXT NOT NULL,
    retained INTEGER NOT NULL CHECK (retained IN (0,1)),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0)
);
CREATE INDEX idx_llm_runs_time ON llm_runs(finished_at);
