-- Runs are audit records. A retry creates a new run, never overwrites old output.
CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL REFERENCES chunk_plans(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    pass_type TEXT NOT NULL,
    schema_key TEXT NOT NULL,
    base_revision_no INTEGER NOT NULL CHECK (base_revision_no >= 1),
    core_hash TEXT NOT NULL CHECK (length(core_hash)=64),
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','done','failed','interrupted','stale')),
    config_json TEXT NOT NULL,
    refs_json TEXT NOT NULL,
    llm_run_ids_json TEXT NOT NULL DEFAULT '[]',
    repairs_json TEXT NOT NULL DEFAULT '[]',
    requires_review INTEGER NOT NULL DEFAULT 0 CHECK (requires_review IN (0,1)),
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX analysis_runs_plan ON analysis_runs(plan_id, ordinal, pass_type);
CREATE TABLE observations (
    id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
    UNIQUE(run_id, ordinal)
);
