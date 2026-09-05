-- Reuse points to the ORIGINAL run/observations: human decisions keep their IDs.
CREATE TABLE analysis_reuse (
    plan_id TEXT NOT NULL REFERENCES chunk_plans(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    pass_type TEXT NOT NULL CHECK (pass_type IN ('facts','links','narrative')),
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    PRIMARY KEY(plan_id, ordinal, pass_type)
);
