-- Migration: v014_add_is_evaluable_column.sql
-- Adds tri-state `is_evaluable` column and `non_evaluable_reason` to bm_mass_evaluation_results.
-- Default is NULL for historical records (undetermined state until explicit backfill).

ALTER TABLE public.bm_mass_evaluation_results 
ADD COLUMN IF NOT EXISTS is_evaluable BOOLEAN NULL;

ALTER TABLE public.bm_mass_evaluation_results 
ADD COLUMN IF NOT EXISTS non_evaluable_reason TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_mass_eval_results_is_evaluable 
ON public.bm_mass_evaluation_results (is_evaluable);
