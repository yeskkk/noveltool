ALTER TABLE llm_runs ADD COLUMN validation_status TEXT
    CHECK (validation_status IN ('ok','failed','repaired'));
ALTER TABLE llm_runs ADD COLUMN parsed_json TEXT;
ALTER TABLE llm_runs ADD COLUMN validation_error TEXT;
