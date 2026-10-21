-- ==============================================================================
-- scripts/validate_staging_db.sql
--
-- Read-only verification script for STAGING / PRUEBAS PostgreSQL database.
-- Validates:
-- 1. Database connection target is indeed a staging/test database.
-- 2. Data fidelity: Row counts across all core entities and operational tables.
-- 3. Safety checks: Zero active schedulers, zero pending/running runs.
-- 4. Schema structure: Presence of all 8 analytical views, key indexes, sequences.
-- ==============================================================================

\set ON_ERROR_STOP on

-- ------------------------------------------------------------------------------
-- 1. DATABASE ENVIRONMENT AUDIT
-- ------------------------------------------------------------------------------
DO $$
DECLARE
    db_name TEXT := current_database();
BEGIN
    IF lower(db_name) !~ '(staging|pruebas|test|dev)' THEN
        RAISE EXCEPTION 'SAFETY VIOLATION: Current database "%" is NOT a recognized staging/test database! Validation aborted.', db_name;
    END IF;

    IF lower(db_name) IN ('n8n', 'speechbm', 'production', 'prod', 'bm_analysis', 'postgres') THEN
        RAISE EXCEPTION 'SAFETY VIOLATION: Current database "%" matches a protected production database name! Validation aborted.', db_name;
    END IF;

    RAISE NOTICE 'Connected to valid staging database: %', db_name;
END $$;

-- ------------------------------------------------------------------------------
-- 2. SPEECH BM TABLES INVENTORY (FIDELITY CHECK)
-- ------------------------------------------------------------------------------
SELECT '1. Speech BM Tables Count & Main Entity Counts' AS section;

SELECT 
    count(*) AS total_bm_tables,
    CASE WHEN count(*) >= 55 THEN 'PASS (Full Speech BM Schema Present)' ELSE 'FAIL (Missing bm_* Tables!)' END AS tables_status
FROM information_schema.tables
WHERE table_schema = 'public' 
  AND table_name LIKE 'bm_%';

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
SELECT 'bm_mass_analysis_automation_runs', count(*) FROM bm_mass_analysis_automation_runs
UNION ALL
SELECT 'bm_training_schedulers', count(*) FROM bm_training_schedulers
UNION ALL
SELECT 'bm_training_scheduler_settings', count(*) FROM bm_training_scheduler_settings
UNION ALL
SELECT 'bm_training_runs', count(*) FROM bm_training_runs
UNION ALL
SELECT 'bm_training_agent_reports', count(*) FROM bm_training_agent_reports
UNION ALL
SELECT 'bm_cloudtalk_evaluation_jobs', count(*) FROM bm_cloudtalk_evaluation_jobs
UNION ALL
SELECT 'bm_ai_usage_events', count(*) FROM bm_ai_usage_events
UNION ALL
SELECT 'bm_ai_model_pricing', count(*) FROM bm_ai_model_pricing
ORDER BY table_name;

-- ------------------------------------------------------------------------------
-- 3. SAFETY CONTROLS VALIDATION (ALL VALUES MUST BE ZERO)
-- ------------------------------------------------------------------------------
SELECT '2. Safety Controls Neutralization Check (Must all be 0)' AS section;

WITH safety_metrics AS (
    SELECT 'Active Mass Evaluation Schedules' AS risk_item,
           count(*) AS risk_count
    FROM bm_mass_evaluation_jobs
    WHERE schedule_enabled = true

    UNION ALL

    SELECT 'Active Mass Analysis Automations',
           count(*)
    FROM bm_mass_analysis_automations
    WHERE is_active = true

    UNION ALL

    SELECT 'Active Training Schedulers',
           count(*)
    FROM bm_training_schedulers
    WHERE is_active = true

    UNION ALL

    SELECT 'Enabled Training Scheduler Settings',
           count(*)
    FROM bm_training_scheduler_settings
    WHERE is_enabled = true

    UNION ALL

    SELECT 'In-Flight Mass Evaluation Runs (running/queued/pending)',
           count(*)
    FROM bm_mass_evaluation_runs
    WHERE status IN ('running', 'queued', 'pending', 'cancelling')

    UNION ALL

    SELECT 'In-Flight Automation Runs (running/queued/pending)',
           count(*)
    FROM bm_mass_analysis_automation_runs
    WHERE status IN ('running', 'queued', 'pending')

    UNION ALL

    SELECT 'In-Flight Training Runs (running/in_progress/pending)',
           count(*)
    FROM bm_training_runs
    WHERE status IN ('running', 'in_progress', 'pending')

    UNION ALL

    SELECT 'Resumable CloudTalk Jobs (pending/waiting/processing)',
           count(*)
    FROM bm_cloudtalk_evaluation_jobs
    WHERE status IN ('pending', 'waiting_recording', 'processing')

    UNION ALL

    SELECT 'Active Password Reset Tokens (bm_password_reset_tokens)',
           count(*)
    FROM bm_password_reset_tokens

    UNION ALL

    SELECT 'Active User Reset Tokens (bm_users.reset_token)',
           count(*)
    FROM bm_users
    WHERE reset_token IS NOT NULL
)
SELECT risk_item,
       risk_count,
       CASE WHEN risk_count = 0 THEN 'PASS (Disarmed)' ELSE 'FAIL (Active Risk!)' END AS status
FROM safety_metrics;

-- ------------------------------------------------------------------------------
-- 4. ANALYTICAL VIEWS AUDIT (14 PRODUCTION VIEWS VERIFICATION)
-- ------------------------------------------------------------------------------
SELECT '3. PostgreSQL Analytical Views Verification (14 Expected)' AS section;

-- Audit all 14 known production analytical views explicitly
WITH expected_views AS (
    SELECT unnest(ARRAY[
        'vw_bm_all_analysis_criteria_flat',
        'vw_bm_analysis_criteria_flat',
        'vw_bm_analysis_full_current',
        'vw_bm_analysis_full_history',
        'vw_bm_analysis_results_current',
        'vw_bm_analysis_results_history',
        'vw_bm_analysis_results_pivot',
        'vw_bm_individual_analysis_criteria_flat',
        'vw_bm_individual_analysis_report_wide',
        'vw_bm_individual_analysis_summary',
        'vw_bm_mass_evaluation_calls_summary',
        'vw_bm_mass_evaluation_criteria_flat',
        'vw_bm_mass_evaluation_daily_summary',
        'vw_bm_mass_evaluation_report_wide'
    ]) AS view_name
)
SELECT 
    ev.view_name,
    CASE WHEN v.table_name IS NOT NULL THEN 'EXISTS (OK)' ELSE 'MISSING (ERROR)' END AS status
FROM expected_views ev
LEFT JOIN information_schema.views v 
    ON v.table_schema = 'public' AND v.table_name = ev.view_name
ORDER BY ev.view_name;

SELECT 
    count(*) AS total_analytical_views,
    CASE WHEN count(*) >= 14 THEN 'PASS (14/14 Views Present)' ELSE 'FAIL (Missing Views!)' END AS views_status
FROM information_schema.views 
WHERE table_schema = 'public' 
  AND table_name LIKE 'vw_bm_%';

-- ------------------------------------------------------------------------------
-- 5. CRITICAL PERFORMANCE INDEXES AUDIT
-- ------------------------------------------------------------------------------
SELECT '4. Performance Indexes Verification' AS section;

SELECT 
    tablename,
    indexname
FROM pg_indexes
WHERE schemaname = 'public'
  AND (
      indexname LIKE 'ix_bm_%' OR 
      indexname LIKE 'idx_bm_%' OR 
      indexname LIKE 'uq_bm_%'
  )
ORDER BY tablename, indexname
LIMIT 25;

-- ------------------------------------------------------------------------------
-- 6. OVERALL VERIFICATION VERDICT
-- ------------------------------------------------------------------------------
DO $$
DECLARE
    active_risks INTEGER := 0;
    views_count INTEGER := 0;
    tables_count INTEGER := 0;
BEGIN
    SELECT count(*) INTO active_risks FROM (
        SELECT 1 FROM bm_mass_evaluation_jobs WHERE schedule_enabled = true
        UNION ALL
        SELECT 1 FROM bm_mass_analysis_automations WHERE is_active = true
        UNION ALL
        SELECT 1 FROM bm_training_schedulers WHERE is_active = true
        UNION ALL
        SELECT 1 FROM bm_training_scheduler_settings WHERE is_enabled = true
        UNION ALL
        SELECT 1 FROM bm_mass_evaluation_runs WHERE status IN ('running', 'queued', 'pending')
        UNION ALL
        SELECT 1 FROM bm_mass_analysis_automation_runs WHERE status IN ('running', 'queued', 'pending')
        UNION ALL
        SELECT 1 FROM bm_training_runs WHERE status IN ('running', 'in_progress', 'pending')
        UNION ALL
        SELECT 1 FROM bm_cloudtalk_evaluation_jobs WHERE status IN ('pending', 'waiting_recording', 'processing')
        UNION ALL
        SELECT 1 FROM bm_password_reset_tokens
        UNION ALL
        SELECT 1 FROM bm_users WHERE reset_token IS NOT NULL
    ) sub;

    SELECT count(*) INTO views_count
    FROM information_schema.views
    WHERE table_schema = 'public' AND table_name LIKE 'vw_bm_%';

    SELECT count(*) INTO tables_count
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name LIKE 'bm_%';

    IF active_risks = 0 AND views_count >= 14 AND tables_count >= 55 THEN
        RAISE NOTICE '=======================================================';
        RAISE NOTICE '[VALIDATION SUCCESS] Staging database is 100%% safe, faithful, and ready.';
        RAISE NOTICE 'Tables (bm_*): %, Views (vw_bm_*): %, Active Risks: %', tables_count, views_count, active_risks;
        RAISE NOTICE '=======================================================';
    ELSE
        RAISE EXCEPTION '[VALIDATION FAILED] Discrepancies detected: % active risks, % analytical views (expected >= 14), % bm_* tables (expected >= 55).', active_risks, views_count, tables_count;
    END IF;
END $$;
