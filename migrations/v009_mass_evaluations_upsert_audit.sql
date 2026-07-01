-- migrations/v009_mass_evaluations_upsert_audit.sql
-- Migration to prevent duplicate mass evaluations per call_id + prompt_id.
-- Also adds audit columns to track overwrites.

-- 1. Add audit columns
ALTER TABLE bm_mass_evaluation_results
    ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS source_job_id INTEGER NULL,
    ADD COLUMN IF NOT EXISTS source_run_id INTEGER NULL;

-- 2. Clean up historical duplicates (keeping the most recent evaluated row per call_id + prompt_id)
DELETE FROM bm_mass_evaluation_results
WHERE mass_analysis_id IN (
    SELECT mass_analysis_id
    FROM (
        SELECT mass_analysis_id,
               ROW_NUMBER() OVER (PARTITION BY call_id, prompt_id ORDER BY mass_analysis_id DESC) as rn
        FROM bm_mass_evaluation_results
    ) t
    WHERE t.rn > 1
);

-- 3. Add Unique Constraint on (call_id, prompt_id)
ALTER TABLE bm_mass_evaluation_results
    ADD CONSTRAINT uq_mass_eval_call_prompt UNIQUE (call_id, prompt_id);
