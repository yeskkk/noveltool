-- Original bytes stay cold in SQLite. Preview uploads are not saved until commit.
CREATE TABLE source_imports (
    id TEXT PRIMARY KEY NOT NULL,
    filename TEXT NOT NULL,
    encoding TEXT NOT NULL,
    paragraph_mode TEXT NOT NULL CHECK (paragraph_mode IN ('auto','blankline','line')),
    raw_bytes BLOB NOT NULL,
    raw_hash TEXT NOT NULL CHECK (length(raw_hash)=64),
    normalized_hash TEXT NOT NULL CHECK (length(normalized_hash)=64),
    revision_id TEXT NOT NULL REFERENCES revisions(id),
    created_at TEXT NOT NULL
);
CREATE TABLE chunk_plans (
    id TEXT PRIMARY KEY NOT NULL,
    base_revision_no INTEGER NOT NULL CHECK (base_revision_no >= 1),
    manuscript_hash TEXT NOT NULL CHECK (length(manuscript_hash)=64),
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
