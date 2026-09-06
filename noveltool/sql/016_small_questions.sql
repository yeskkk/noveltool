-- Plain-language micro tasks keep their own checkpoints; source provenance is assigned by Python.
ALTER TABLE project_config ADD COLUMN analysis_protocol TEXT NOT NULL DEFAULT 'small' CHECK (analysis_protocol IN ('small','strict'));
ALTER TABLE project_config ADD COLUMN small_source_chars INTEGER NOT NULL DEFAULT 400 CHECK (small_source_chars BETWEEN 100 AND 480);
ALTER TABLE project_config ADD COLUMN small_output_tokens INTEGER NOT NULL DEFAULT 1536 CHECK (small_output_tokens BETWEEN 256 AND 8192);
ALTER TABLE project_config ADD COLUMN small_max_entities INTEGER NOT NULL DEFAULT 3 CHECK (small_max_entities BETWEEN 1 AND 6);
ALTER TABLE analysis_runs ADD COLUMN quality_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE idea_proposals ADD COLUMN quality_json TEXT NOT NULL DEFAULT '{}';
CREATE TABLE model_steps (
 id TEXT PRIMARY KEY NOT NULL,
 owner_type TEXT NOT NULL CHECK (owner_type IN ('analysis','idea','diagnostic','generation')),
 owner_id TEXT NOT NULL,
 step_key TEXT NOT NULL,
 cache_key TEXT NOT NULL,
 task_label TEXT NOT NULL,
 status TEXT NOT NULL CHECK (status IN ('running','complete','partial','failed','interrupted')),
 value_json TEXT NOT NULL DEFAULT 'null',
 raw_output TEXT NOT NULL DEFAULT '',
 run_ids_json TEXT NOT NULL DEFAULT '[]',
 warnings_json TEXT NOT NULL DEFAULT '[]',
 error TEXT,
 reused_from TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX model_steps_owner ON model_steps(owner_type,owner_id);
CREATE INDEX model_steps_cache ON model_steps(cache_key,status);
