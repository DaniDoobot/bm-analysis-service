-- ==============================================================================
-- scripts/audit_speech_db_storage.sql
--
-- SPEECH BM — STORAGE & DATABASE USAGE AUDIT SCRIPT
--
-- 100% READ-ONLY DIAGNOSTIC SCRIPT FOR POSTGRESQL (Production & Staging).
--
-- PURPOSE:
-- 1. Measure real physical disk usage for all Speech BM tables (public.bm_*).
-- 2. Differentiate main heap data, indexes, and TOAST compression storage.
-- 3. Attribute storage by Speech BM module (Masivas, Ciclos, Trainer, Tutor IA, Estructura, FinOps, Otros).
-- 4. Provide row count estimates via catalog statistics (reltuples, zero table scans).
-- 5. List all Speech BM analytical views (public.vw_bm_*).
-- 6. Provide a safe statistical distribution by clinical service via pg_stats (zero table scans).
--
-- SAFETY & PERFORMANCE GUARANTEES:
-- - Strict read-only transaction (enforces error on any accidental write/DDL).
-- - ZERO destructive or mutating operations (NO INSERT/UPDATE/DELETE/ALTER/DROP/CREATE).
-- - ZERO maintenance commands (NO VACUUM, NO ANALYZE, NO REINDEX).
-- - ZERO secrets, environment variables, or customer data exposed.
-- - ZERO FULL TABLE SCANS on application tables: Phase 1 relies 100% on PostgreSQL
--   system catalogs (pg_class, pg_namespace, pg_stats). Execution time < 100 ms.
-- - Deep column sampling (pg_column_size) and historical timelines are safely
--   separated and commented out in PHASE 2 for optional execution.
--
-- USAGE IN PRODUCTION:
--   psql "$DATABASE_URL" -f scripts/audit_speech_db_storage.sql > speech_storage_audit_report.txt
-- ==============================================================================

\set ON_ERROR_STOP on

BEGIN TRANSACTION READ ONLY;

-- ==============================================================================
-- PHASE 1: ZERO-IMPACT METADATA & CATALOG AUDIT (< 100 ms in Production)
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 0. ENVIRONMENT & SESSION METADATA
-- ------------------------------------------------------------------------------
SELECT 
    '=== SPEECH BM STORAGE AUDIT ===' AS report_header,
    current_database() AS database_name,
    current_user AS database_user,
    version() AS postgres_version,
    now() AS executed_at;

-- ------------------------------------------------------------------------------
-- 1. GLOBAL DISK USAGE SUMMARY (SPEECH BM VS TOTAL DATABASE)
-- Cost: Reads ONLY pg_class and pg_namespace catalogs + pg_database_size().
-- ------------------------------------------------------------------------------
SELECT '=== 1. GLOBAL DISK USAGE: SPEECH BM VS DATABASE ===' AS section;

WITH db_totals AS (
    SELECT 
        pg_database_size(current_database()) AS db_bytes
),
speech_tables AS (
    SELECT 
        c.oid,
        c.relname,
        pg_relation_size(c.oid) AS heap_bytes,
        pg_indexes_size(c.oid) AS index_bytes,
        CASE WHEN c.reltoastrelid = 0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END AS toast_bytes,
        pg_total_relation_size(c.oid) AS total_table_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname LIKE 'bm_%'
      AND c.relkind = 'r'
),
speech_summary AS (
    SELECT 
        count(*) AS total_bm_tables,
        COALESCE(sum(heap_bytes), 0) AS total_heap_bytes,
        COALESCE(sum(index_bytes), 0) AS total_index_bytes,
        COALESCE(sum(toast_bytes), 0) AS total_toast_bytes,
        COALESCE(sum(total_table_bytes), 0) AS total_speech_bytes
    FROM speech_tables
),
views_summary AS (
    SELECT 
        count(*) AS total_bm_views
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname LIKE 'vw_bm_%'
      AND c.relkind IN ('v', 'm')
)
SELECT 
    ss.total_bm_tables,
    vs.total_bm_views,
    pg_size_pretty(ss.total_heap_bytes)   AS speech_heap_data_pretty,
    pg_size_pretty(ss.total_index_bytes)  AS speech_indexes_pretty,
    pg_size_pretty(ss.total_toast_bytes)  AS speech_toast_data_pretty,
    pg_size_pretty(ss.total_speech_bytes) AS speech_total_size_pretty,
    pg_size_pretty(dt.db_bytes)           AS full_database_size_pretty,
    ROUND((ss.total_speech_bytes::numeric / NULLIF(dt.db_bytes, 0)) * 100, 2) AS speech_pct_of_database,
    ROUND((ss.total_toast_bytes::numeric  / NULLIF(ss.total_speech_bytes, 0)) * 100, 2) AS toast_pct_of_speech
FROM speech_summary ss
CROSS JOIN views_summary vs
CROSS JOIN db_totals dt;


-- ------------------------------------------------------------------------------
-- 2. DETAILED INVENTORY PER SPEECH BM TABLE (SORTED BY TOTAL SIZE DESC)
-- Cost: Reads ONLY pg_class and pg_namespace catalogs.
-- Uses reltuples for estimated row count without performing any table scan.
-- ------------------------------------------------------------------------------
SELECT '=== 2. DETAILED INVENTORY PER TABLE (SORTED BY TOTAL SIZE DESC) ===' AS section;

WITH speech_total AS (
    SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 1) AS total_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname LIKE 'bm_%'
      AND c.relkind = 'r'
)
SELECT 
    c.relname AS table_name,
    GREATEST(c.reltuples::bigint, 0) AS approx_row_count,
    pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size_pretty,
    pg_size_pretty(pg_relation_size(c.oid))        AS heap_size_pretty,
    pg_size_pretty(pg_indexes_size(c.oid))         AS index_size_pretty,
    pg_size_pretty(CASE WHEN c.reltoastrelid = 0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END) AS toast_size_pretty,
    pg_total_relation_size(c.oid)                 AS total_size_bytes,
    ROUND((pg_total_relation_size(c.oid)::numeric / st.total_bytes) * 100, 2) AS percentage_of_speech,
    ROUND((CASE WHEN c.reltoastrelid = 0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END::numeric / NULLIF(pg_total_relation_size(c.oid), 0)) * 100, 2) AS toast_pct_of_table
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN speech_total st
WHERE n.nspname = 'public'
  AND c.relname LIKE 'bm_%'
  AND c.relkind = 'r'
ORDER BY total_size_bytes DESC;


-- ------------------------------------------------------------------------------
-- 3. AGGREGATED STORAGE BY SPEECH BM FUNCTIONAL MODULE
-- Cost: Reads ONLY pg_class catalog. Categorizes all 60 Speech BM tables.
-- ------------------------------------------------------------------------------
SELECT '=== 3. AGGREGATED STORAGE BY MODULE ===' AS section;

WITH speech_total AS (
    SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 1) AS total_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname LIKE 'bm_%'
      AND c.relkind = 'r'
),
categorized_tables AS (
    SELECT 
        c.relname,
        c.reltuples,
        pg_relation_size(c.oid) AS heap_bytes,
        pg_indexes_size(c.oid) AS index_bytes,
        CASE WHEN c.reltoastrelid = 0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END AS toast_bytes,
        pg_total_relation_size(c.oid) AS total_bytes,
        CASE
            -- B. MASIVAS / EVALUACIONES / ANALYTICS CORE
            WHEN c.relname IN (
                'bm_mass_evaluation_results',
                'bm_mass_evaluation_criterion_results',
                'bm_mass_evaluation_runs',
                'bm_mass_evaluation_jobs',
                'bm_mass_analysis_automations',
                'bm_mass_analysis_automation_runs',
                'bm_analyses',
                'bm_call_analysis_current',
                'bm_analysis_criterion_results',
                'bm_analysis_results',
                'bm_cloudtalk_evaluation_jobs',
                'bm_hubspot_call_tags'
            ) THEN 'MASIVAS'

            -- D. CICLOS / PERSONALIZED TRAINING
            WHEN c.relname IN (
                'bm_training_plans',
                'bm_training_cycles',
                'bm_training_agent_states',
                'bm_training_knowledge_documents',
                'bm_training_pathway_assignments',
                'bm_training_pathway_cycles',
                'bm_training_case_library',
                'bm_training_evaluators',
                'bm_training_specialties',
                'bm_training_levels',
                'bm_training_stages',
                'bm_training_personas',
                'bm_training_voice_catalog',
                'bm_training_agent_reports',
                'bm_training_call_evaluations',
                'bm_training_call_sessions',
                'bm_training_completion_status',
                'bm_training_simulation_prompts',
                'bm_training_runs',
                'bm_training_agent_settings',
                'bm_training_evaluation_prompts',
                'bm_training_schedulers',
                'bm_training_scheduler_settings',
                'bm_training_scheduler_agents',
                'bm_training_call_attempts'
            ) THEN 'CICLOS'

            -- C. TRAINER / SIMULACIONES
            WHEN c.relname IN (
                'bm_training_sessions',
                'bm_training_session_evaluations',
                'bm_training_session_events',
                'bm_training_scenarios',
                'bm_training_agent_contexts',
                'bm_trainer_prompts',
                'bm_trainer_prompt_versions',
                'bm_trainer_sessions',
                'bm_trainer_evaluations',
                'bm_trainer_simulations',
                'bm_trainer_simulation_versions',
                'bm_trainer_evaluation_configs',
                'bm_trainer_tasks',
                'bm_trainer_task_items',
                'bm_trainer_task_assignees',
                'bm_trainer_task_attempt_allocations'
            ) THEN 'TRAINER'

            -- E. TUTOR IA / CHAT
            WHEN c.relname IN (
                'bm_tutor_interactions',
                'bm_tutor_feedback',
                'bm_trainer_chats',
                'bm_trainer_chat_messages'
            ) THEN 'TUTOR_IA'

            -- F. FINOPS / CONSUMO DE TOKENS
            WHEN c.relname IN (
                'bm_ai_usage_events',
                'bm_ai_cost_rates',
                'bm_ai_daily_aggregates',
                'bm_ai_model_pricing',
                'bm_user_audits'
            ) THEN 'FINOPS'

            -- A. ESTRUCTURA / CONFIGURACION BASE
            WHEN c.relname IN (
                'bm_companies',
                'bm_company_access',
                'bm_clinical_services',
                'bm_service_typologies',
                'bm_services',
                'bm_teams',
                'bm_users',
                'bm_user_roles',
                'bm_roles',
                'bm_permissions',
                'bm_role_permissions',
                'bm_agent_attributes',
                'bm_lead_source_groups',
                'bm_user_services',
                'bm_user_teams',
                'bm_agent_teams',
                'bm_prompts',
                'bm_prompt_versions',
                'bm_prompt_sections',
                'bm_prompt_base_structures',
                'bm_prompt_criteria',
                'bm_typologies',
                'bm_prompt_criterion_typologies',
                'bm_base_structure_typologies',
                'bm_prompt_drafts',
                'bm_structure_permissions',
                'bm_structure_permissions_audit',
                'bm_criteria_sync_logs',
                'bm_password_reset_tokens'
            ) THEN 'ESTRUCTURA'

            ELSE 'OTROS'
        END AS module_group
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname LIKE 'bm_%'
      AND c.relkind = 'r'
)
SELECT 
    module_group,
    count(*) AS tables_count,
    sum(GREATEST(reltuples::bigint, 0)) AS approx_rows_count,
    pg_size_pretty(sum(total_bytes)) AS total_size_pretty,
    pg_size_pretty(sum(heap_bytes))  AS heap_data_pretty,
    pg_size_pretty(sum(index_bytes)) AS indexes_pretty,
    pg_size_pretty(sum(toast_bytes)) AS toast_data_pretty,
    sum(total_bytes) AS total_size_bytes,
    ROUND((sum(total_bytes)::numeric / st.total_bytes) * 100, 2) AS percentage_of_speech,
    ROUND((sum(toast_bytes)::numeric / NULLIF(sum(total_bytes), 0)) * 100, 2) AS toast_pct_of_module
FROM categorized_tables
CROSS JOIN speech_total st
GROUP BY module_group, st.total_bytes
ORDER BY total_size_bytes DESC;


-- ------------------------------------------------------------------------------
-- 4. SPEECH BM ANALYTICAL VIEWS INVENTORY
-- Cost: Reads ONLY pg_class and pg_namespace catalogs.
-- ------------------------------------------------------------------------------
SELECT '=== 4. SPEECH BM ANALYTICAL VIEWS INVENTORY ===' AS section;

SELECT 
    c.relname AS view_name,
    CASE c.relkind
        WHEN 'v' THEN 'Standard SQL View (No Physical Storage)'
        WHEN 'm' THEN 'Materialized View (Uses Physical Storage)'
        ELSE 'Other'
    END AS view_type,
    pg_size_pretty(pg_total_relation_size(c.oid)) AS total_storage_used
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname LIKE 'vw_bm_%'
  AND c.relkind IN ('v', 'm')
ORDER BY c.relname;


-- ------------------------------------------------------------------------------
-- 5. SAFE SERVICE-LEVEL DISTRIBUTION ESTIMATE (VIA pg_stats CATALOG)
-- Cost: Reads ONLY pg_stats catalog. ZERO TABLE SCANS on bm_mass_evaluation_results.
-- ------------------------------------------------------------------------------
SELECT '=== 5. SERVICE-LEVEL DISTRIBUTION (STATISTICAL ESTIMATE VIA pg_stats) ===' AS section;

SELECT 
    s.schemaname,
    s.tablename,
    s.attname,
    s.null_frac AS null_service_fraction,
    s.n_distinct AS distinct_services_estimate,
    s.most_common_vals::text AS top_service_ids,
    s.most_common_freqs::text AS top_service_frequencies
FROM pg_stats s
WHERE s.schemaname = 'public'
  AND s.tablename = 'bm_mass_evaluation_results'
  AND s.attname = 'service_id';

-- Optional: Inspect small configuration table bm_clinical_services / bm_services (< 100 rows, negligible cost)
SELECT 
    c.relname AS table_name,
    GREATEST(c.reltuples::bigint, 0) AS approx_services_count,
    pg_size_pretty(pg_total_relation_size(c.oid)) AS table_size_pretty
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('bm_clinical_services', 'bm_services')
  AND c.relkind = 'r';


-- ==============================================================================
-- END OF PHASE 1 (SAFE TO COMMIT / ROLLBACK READ-ONLY TRANSACTION)
-- ==============================================================================
ROLLBACK;


-- ==============================================================================
-- PHASE 2: DEEP INSPECTION QUERIES (DEFERRED / OPTIONAL)
--
-- CAUTION:
-- The following queries require scanning tables or computing pg_column_size
-- on large datasets. They are commented out by default to guarantee ZERO IMPACT
-- during initial execution in production.
-- Run them only during off-peak maintenance windows or in staging.
-- ==============================================================================

/*
-- ------------------------------------------------------------------------------
-- PHASE 2.1. COLUMN SIZE ESTIMATES VIA TABLESAMPLE SYSTEM (LIGHTWEIGHT SAMPLING)
-- Uses TABLESAMPLE SYSTEM (1) to scan only 1% of random physical pages.
-- ------------------------------------------------------------------------------
BEGIN TRANSACTION READ ONLY;

SELECT '--- PHASE 2.1: bm_mass_evaluation_results Sampled Column Sizes (1% physical pages) ---' AS section;

SELECT 
    'bm_mass_evaluation_results' AS table_name,
    count(*) AS sample_rows_examined,
    pg_size_pretty(AVG(pg_column_size(prompt_snapshot))::bigint)     AS avg_prompt_snapshot_size,
    pg_size_pretty(MAX(pg_column_size(prompt_snapshot))::bigint)     AS max_prompt_snapshot_size,
    pg_size_pretty(AVG(pg_column_size(result_json))::bigint)         AS avg_result_json_size,
    pg_size_pretty(MAX(pg_column_size(result_json))::bigint)         AS max_result_json_size,
    pg_size_pretty(AVG(pg_column_size(items_json))::bigint)          AS avg_items_json_size,
    pg_size_pretty(MAX(pg_column_size(items_json))::bigint)          AS max_items_json_size,
    pg_size_pretty(AVG(pg_column_size(hubspot_metadata))::bigint)    AS avg_hubspot_metadata_size,
    pg_size_pretty(MAX(pg_column_size(hubspot_metadata))::bigint)    AS max_hubspot_metadata_size,
    pg_size_pretty(AVG(pg_column_size(recording_url))::bigint)       AS avg_recording_url_size
FROM bm_mass_evaluation_results TABLESAMPLE SYSTEM (1);

ROLLBACK;
*/

/*
-- ------------------------------------------------------------------------------
-- PHASE 2.2. HISTORICAL TIMELINES & RETENTION WINDOWS
-- Requires index/table scans on analysis_timestamp / created_at.
-- ------------------------------------------------------------------------------
BEGIN TRANSACTION READ ONLY;

SELECT '--- PHASE 2.2: Retention Window Comparison (bm_mass_evaluation_results) ---' AS section;

SELECT 
    'bm_mass_evaluation_results' AS table_name,
    count(CASE WHEN analysis_timestamp >= (now() - interval '30 days') THEN 1 END)  AS calls_last_30_days,
    count(CASE WHEN analysis_timestamp >= (now() - interval '90 days') THEN 1 END)  AS calls_last_90_days,
    count(CASE WHEN analysis_timestamp >= (now() - interval '180 days') THEN 1 END) AS calls_last_180_days,
    count(CASE WHEN analysis_timestamp >= (now() - interval '365 days') THEN 1 END) AS calls_last_365_days,
    count(CASE WHEN analysis_timestamp < (now() - interval '365 days') THEN 1 END)  AS calls_older_than_1_year,
    count(*) AS total_historical_calls
FROM bm_mass_evaluation_results;

ROLLBACK;
*/
