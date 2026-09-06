ALTER TABLE project_config ADD COLUMN auto_chinese INTEGER NOT NULL DEFAULT 1 CHECK (auto_chinese IN (0,1));
CREATE TABLE language_outputs (
 id TEXT PRIMARY KEY NOT NULL,
 owner_type TEXT NOT NULL,
 owner_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','complete','partial','interrupted')),
 original_text TEXT NOT NULL,
 final_text TEXT NOT NULL,
 segments_json TEXT NOT NULL DEFAULT '[]',
 run_ids_json TEXT NOT NULL DEFAULT '[]',
 warnings_json TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX language_outputs_owner ON language_outputs(owner_type,owner_id,fingerprint);
ALTER TABLE model_steps ADD COLUMN language_id TEXT REFERENCES language_outputs(id);
ALTER TABLE generation_attempts ADD COLUMN language_id TEXT REFERENCES language_outputs(id);
