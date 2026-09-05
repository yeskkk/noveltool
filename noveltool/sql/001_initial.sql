-- Only implemented M1 data is created. Future milestones add real migrations.
-- All statements below are executed inside the creation transaction.
CREATE TABLE project_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    id TEXT NOT NULL UNIQUE CHECK (length(id) = 32),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    data_version INTEGER NOT NULL DEFAULT 0 CHECK (data_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    saved_at TEXT NOT NULL
);

CREATE TABLE project_config (
    project_id TEXT PRIMARY KEY NOT NULL
        REFERENCES project_meta(id) ON DELETE CASCADE,
    api_base_url TEXT NOT NULL,
    api_key_env TEXT NOT NULL,
    writer_model TEXT NOT NULL DEFAULT '',
    analysis_model TEXT NOT NULL DEFAULT '',
    context_window INTEGER NOT NULL CHECK (context_window BETWEEN 2048 AND 1000000),
    context_safety_ratio REAL NOT NULL CHECK (context_safety_ratio > 0 AND context_safety_ratio <= 1),
    min_chars INTEGER NOT NULL CHECK (min_chars BETWEEN 1 AND 1000000),
    max_chars INTEGER NOT NULL CHECK (max_chars >= min_chars AND max_chars <= 1000000),
    candidate_count INTEGER NOT NULL CHECK (candidate_count BETWEEN 1 AND 32),
    writer_temperature REAL NOT NULL CHECK (writer_temperature BETWEEN 0 AND 10),
    analysis_temperature REAL NOT NULL CHECK (analysis_temperature BETWEEN 0 AND 10),
    api_timeout_seconds INTEGER NOT NULL CHECK (api_timeout_seconds BETWEEN 1 AND 86400),
    autosave_seconds INTEGER NOT NULL CHECK (autosave_seconds BETWEEN 10 AND 3600),
    retain_llm_logs INTEGER NOT NULL CHECK (retain_llm_logs IN (0, 1))
);
