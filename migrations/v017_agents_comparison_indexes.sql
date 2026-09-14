-- Migration: v017_agents_comparison_indexes.sql
-- Targeted PostgreSQL performance indexes for GET /bm/analytics/agents-comparison (Results filtering).
--
-- LOCKING BEHAVIOR & CONCURRENCY:
-- - Standard `CREATE INDEX` acquires a SHARE lock on the target table.
-- - Concurrent SELECT queries continue without blocking.
-- - Concurrent INSERT, UPDATE, and DELETE operations are BLOCKED until index creation completes.
-- - If applying manually in production during active traffic without write interruption,
--   run each statement with `CREATE INDEX CONCURRENTLY` outside of a transaction block (psql \set AUTOCOMMIT on).
-- - Note: Criterion results index on bm_mass_evaluation_criterion_results is deferred under Option D
--   pending real production EXPLAIN (ANALYZE, BUFFERS) validation to avoid unnecessary write overhead.

-- 1. Index on bm_mass_evaluation_results for service + status + COALESCE date range filtering (Query A & B)
CREATE INDEX IF NOT EXISTS idx_mass_eval_results_svc_status_coalesce_date
ON public.bm_mass_evaluation_results (
    service_id,
    status,
    (COALESCE(call_timestamp, analysis_timestamp))
);

-- 2. Index on bm_mass_evaluation_results for company-level multi-tenant date filtering
CREATE INDEX IF NOT EXISTS idx_mass_eval_results_company_coalesce_date
ON public.bm_mass_evaluation_results (
    company_id,
    (COALESCE(call_timestamp, analysis_timestamp))
);
