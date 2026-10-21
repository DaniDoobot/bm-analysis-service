-- ==============================================================================
-- scripts/compare_prod_staging.sql
--
-- Safe, read-only diagnostic script to compare schema metrics and key table counts 
-- between Production (n8n DB) and Staging (bm-analysis-service-pruebas DB).
--
-- USAGE:
--   psql "$PROD_DATABASE_URL" -f scripts/compare_prod_staging.sql > /tmp/prod_metrics.txt
--   psql "$STAGING_DATABASE_URL" -f scripts/compare_prod_staging.sql > /tmp/staging_metrics.txt
--   diff -u /tmp/prod_metrics.txt /tmp/staging_metrics.txt
-- ==============================================================================

\set ON_ERROR_STOP on

SELECT 
    '=== ENVIRONMENT AUDIT ===' AS section,
    current_database() AS database_name,
    current_user AS database_user,
    version() AS pg_version,
    now() AS executed_at;

-- 1. Total Speech BM Tables
SELECT 
    'Speech Tables Total (public.bm_*)' AS metric,
    count(*) AS count
FROM information_schema.tables 
WHERE table_schema = 'public' 
  AND table_name LIKE 'bm_%';

-- 2. Total Speech BM Views
SELECT 
    'Speech Analytical Views Total (public.vw_bm_*)' AS metric,
    count(*) AS count
FROM information_schema.views 
WHERE table_schema = 'public' 
  AND table_name LIKE 'vw_bm_%';

-- 3. Core Entities Row Counts (Fidelity Comparison)
SELECT '=== CRITICAL ENTITIES ROW COUNTS ===' AS section;

SELECT 
    'bm_companies' AS table_name, count(*) AS total_rows FROM bm_companies
UNION ALL
SELECT 'bm_users', count(*) FROM bm_users
UNION ALL
SELECT 'bm_services', count(*) FROM bm_services
UNION ALL
SELECT 'bm_teams', count(*) FROM bm_teams
UNION ALL
SELECT 'bm_prompts', count(*) FROM bm_prompts
UNION ALL
SELECT 'bm_prompt_versions', count(*) FROM bm_prompt_versions
UNION ALL
SELECT 'bm_prompt_criteria', count(*) FROM bm_prompt_criteria
UNION ALL
SELECT 'bm_typologies', count(*) FROM bm_typologies
UNION ALL
SELECT 'bm_prompt_base_structures', count(*) FROM bm_prompt_base_structures
UNION ALL
SELECT 'bm_base_structure_typologies', count(*) FROM bm_base_structure_typologies
UNION ALL
SELECT 'bm_analyses', count(*) FROM bm_analyses
UNION ALL
SELECT 'bm_analysis_criterion_results', count(*) FROM bm_analysis_criterion_results
UNION ALL
SELECT 'bm_call_analysis_current', count(*) FROM bm_call_analysis_current
UNION ALL
SELECT 'bm_mass_evaluation_jobs', count(*) FROM bm_mass_evaluation_jobs
UNION ALL
SELECT 'bm_mass_evaluation_runs', count(*) FROM bm_mass_evaluation_runs
UNION ALL
SELECT 'bm_mass_evaluation_results', count(*) FROM bm_mass_evaluation_results
UNION ALL
SELECT 'bm_mass_evaluation_criterion_results', count(*) FROM bm_mass_evaluation_criterion_results
UNION ALL
SELECT 'bm_mass_analysis_automations', count(*) FROM bm_mass_analysis_automations
UNION ALL
SELECT 'bm_training_schedulers', count(*) FROM bm_training_schedulers
UNION ALL
SELECT 'bm_training_runs', count(*) FROM bm_training_runs
UNION ALL
SELECT 'bm_cloudtalk_evaluation_jobs', count(*) FROM bm_cloudtalk_evaluation_jobs
UNION ALL
SELECT 'bm_ai_usage_events', count(*) FROM bm_ai_usage_events
UNION ALL
SELECT 'bm_ai_model_pricing', count(*) FROM bm_ai_model_pricing
ORDER BY table_name;
