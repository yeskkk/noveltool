-- Human edits are separate from model observations; they are never overwritten by analysis.
CREATE TABLE setting_entities (
    id TEXT PRIMARY KEY NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE setting_entries (
    id TEXT PRIMARY KEY NOT NULL,
    slot TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0,1)),
    record_json TEXT NOT NULL
);
CREATE UNIQUE INDEX setting_active_slot ON setting_entries(slot) WHERE active=1;
CREATE TABLE setting_changes (
    version INTEGER PRIMARY KEY NOT NULL CHECK (version >= 1),
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
