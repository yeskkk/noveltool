-- Preserve old projects; retries are bounded, and never concatenate partial JSON.
ALTER TABLE project_config ADD COLUMN structured_output_ceiling INTEGER NOT NULL DEFAULT 8192 CHECK (structured_output_ceiling BETWEEN 512 AND 65536);
ALTER TABLE project_config ADD COLUMN output_retry_limit INTEGER NOT NULL DEFAULT 1 CHECK (output_retry_limit BETWEEN 0 AND 2);
ALTER TABLE llm_runs ADD COLUMN requested_max_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE llm_runs ADD COLUMN input_token_estimate INTEGER NOT NULL DEFAULT 0;
