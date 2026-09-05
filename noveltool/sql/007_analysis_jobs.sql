-- Each start/resume creates a job record; finished chunk runs are reused.
CREATE TABLE analysis_jobs (
    id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL REFERENCES chunk_plans(id),
    base_revision_no INTEGER NOT NULL CHECK (base_revision_no >= 1),
    passes_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    retry_failed INTEGER NOT NULL CHECK (retry_failed IN (0,1)),
    status TEXT NOT NULL CHECK (status IN ('running','pausing','paused','done','partial','stale','interrupted','failed')),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
