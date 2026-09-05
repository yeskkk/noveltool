-- Immutable task selection, independent of mutable draft text.
CREATE TABLE generation_rewrite_targets (
    task_id TEXT PRIMARY KEY NOT NULL REFERENCES generation_tasks(id),
    target_json TEXT NOT NULL
);
