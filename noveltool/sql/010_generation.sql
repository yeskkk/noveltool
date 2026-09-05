CREATE TABLE generation_tasks (
    id TEXT PRIMARY KEY NOT NULL,
    task_type TEXT NOT NULL CHECK (task_type IN ('continue','rewrite')),
    base_revision_no INTEGER NOT NULL CHECK (base_revision_no >= 0),
    base_setting_version TEXT NOT NULL,
    candidate_count INTEGER NOT NULL CHECK (candidate_count BETWEEN 1 AND 32),
    request_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('generating','pausing','paused','ready','failed','interrupted','stale','committed')),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE generation_attempts (
    id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL REFERENCES generation_tasks(id),
    candidate_index INTEGER NOT NULL CHECK (candidate_index >= 0),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    status TEXT NOT NULL CHECK (status IN ('running','complete','length_mismatch','failed','interrupted')),
    text TEXT NOT NULL DEFAULT '',
    char_count INTEGER NOT NULL DEFAULT 0 CHECK (char_count >= 0),
    error TEXT,
    llm_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id,candidate_index,attempt_no)
);
