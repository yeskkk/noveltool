CREATE TABLE consistency_jobs (
    id TEXT PRIMARY KEY NOT NULL,
    revision_id TEXT NOT NULL REFERENCES revisions(id),
    base_revision_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting_sync','running','pausing','paused','done','partial','stale','interrupted','failed','not_applicable')),
    output_reserve INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    unit_count INTEGER NOT NULL,
    total_chars INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    schema_key TEXT NOT NULL
);
CREATE TABLE consistency_units (
    job_id TEXT NOT NULL REFERENCES consistency_jobs(id),
    ordinal INTEGER NOT NULL,
    messages_json TEXT NOT NULL,
    refs_json TEXT NOT NULL,
    input_estimate INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','done','failed','interrupted','stale')),
    result_json TEXT,
    run_ids_json TEXT NOT NULL DEFAULT '[]',
    requires_review INTEGER NOT NULL DEFAULT 0 CHECK(requires_review IN (0,1)),
    error TEXT,
    PRIMARY KEY(job_id,ordinal)
);
CREATE TABLE consistency_issues (
    id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL,
    unit_ordinal INTEGER NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('low','medium','high')),
    finding_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved','ignored')),
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id,unit_ordinal) REFERENCES consistency_units(job_id,ordinal)
);
