-- Text is immutable; only an active block's position may change.
CREATE TABLE revisions (
    id TEXT PRIMARY KEY NOT NULL,
    revision_no INTEGER NOT NULL UNIQUE CHECK (revision_no >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('import','append','rewrite','manual_edit','undo')),
    undo_target_id TEXT REFERENCES revisions(id),
    splice_start INTEGER NOT NULL CHECK (splice_start >= 0),
    old_ids_json TEXT NOT NULL,
    new_ids_json TEXT NOT NULL,
    before_hash TEXT NOT NULL CHECK (length(before_hash)=64),
    after_hash TEXT NOT NULL CHECK (length(after_hash)=64),
    selection_start INTEGER,
    selection_end INTEGER,
    instruction TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK ((kind='undo') = (undo_target_id IS NOT NULL))
);
CREATE UNIQUE INDEX idx_undo_target ON revisions(undo_target_id) WHERE undo_target_id IS NOT NULL;
CREATE TABLE manuscript_blocks (
    id TEXT PRIMARY KEY NOT NULL,
    text TEXT NOT NULL CHECK (length(text)>0),
    content_hash TEXT NOT NULL CHECK (length(content_hash)=64),
    seq INTEGER CHECK (seq >= 0),
    created_revision_id TEXT NOT NULL REFERENCES revisions(id)
);
CREATE UNIQUE INDEX idx_active_block_seq ON manuscript_blocks(seq) WHERE seq IS NOT NULL;
CREATE TRIGGER block_content_immutable
BEFORE UPDATE OF id, text, content_hash, created_revision_id ON manuscript_blocks
BEGIN SELECT RAISE(ABORT, 'immutable block content'); END;
CREATE TRIGGER block_history_retained
BEFORE DELETE ON manuscript_blocks
BEGIN SELECT RAISE(ABORT, 'block history must be retained'); END;
CREATE TRIGGER revision_immutable
BEFORE UPDATE ON revisions
BEGIN SELECT RAISE(ABORT, 'immutable revision'); END;
CREATE TRIGGER revision_history_retained
BEFORE DELETE ON revisions
BEGIN SELECT RAISE(ABORT, 'revision history must be retained'); END;
