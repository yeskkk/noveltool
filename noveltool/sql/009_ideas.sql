-- Ideas are proposals, never observations about events that already happened.
CREATE TABLE idea_proposals (
    id TEXT PRIMARY KEY NOT NULL,
    idea_text TEXT NOT NULL,
    base_revision_no INTEGER NOT NULL CHECK (base_revision_no >= 0),
    base_setting_version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','ready','accepted','stale','interrupted','failed')),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    result_json TEXT,
    draft_json TEXT,
    llm_run_ids_json TEXT NOT NULL DEFAULT '[]',
    repairs_json TEXT NOT NULL DEFAULT '[]',
    requires_review INTEGER NOT NULL DEFAULT 0 CHECK (requires_review IN (0,1)),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
