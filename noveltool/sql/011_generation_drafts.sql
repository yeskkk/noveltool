-- Drafts are not manuscript. Only explicit commit links a task to a revision.
ALTER TABLE generation_tasks ADD COLUMN draft_text TEXT NOT NULL DEFAULT '';
ALTER TABLE generation_tasks ADD COLUMN draft_version INTEGER NOT NULL DEFAULT 0 CHECK(draft_version >= 0);
ALTER TABLE generation_tasks ADD COLUMN committed_revision_id TEXT REFERENCES revisions(id);
ALTER TABLE generation_tasks ADD COLUMN committed_text_hash TEXT;
