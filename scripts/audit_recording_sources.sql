-- ==============================================================================
-- scripts/audit_recording_sources.sql
--
-- SPEECH BM — READ-ONLY AUDIT SCRIPT: CALL RECORDING ORIGINS & HOSTS
--
-- 100% READ-ONLY DIAGNOSTIC SCRIPT FOR POSTGRESQL (Production & Staging).
--
-- PURPOSE:
-- 1. Inventory call recording sources/domains in Speech BM.
-- 2. Detect transitions from Twilio to new recording servers (e.g., from August 2026 onwards).
-- 3. Attribute recording volume by domain, clinical service, and calendar month.
-- 4. Identify null, empty, or malformed recording URLs.
-- 5. Provide a safe, sanitized sample of calls per domain without exposing query params or tokens.
--
-- SAFETY & PERFORMANCE GUARANTEES:
-- - Strict read-only transaction (BEGIN TRANSACTION READ ONLY; ROLLBACK;).
-- - Zero table modifications (NO INSERT / UPDATE / DELETE / DROP / ALTER).
-- - Enforces statement_timeout = '30s' to protect database resources.
-- - Leverages existing timestamp index (idx_mass_eval_results_call_timestamp) for post-July/August queries.
-- - Zero exposure of sensitive tokens, signature parameters, or full query strings.
--
-- USAGE IN PRODUCTION:
--   psql "$DATABASE_URL" -f scripts/audit_recording_sources.sql > audit_recording_sources_report.txt
-- ==============================================================================

\set ON_ERROR_STOP on

BEGIN TRANSACTION READ ONLY;

SET LOCAL statement_timeout = '30s000';

-- ==============================================================================
-- 0. AUDIT HEADER & METADATA
-- ==============================================================================
SELECT 
    '=== SPEECH BM: RECORDING SOURCES & DOMAIN AUDIT ===' AS report_header,
    current_database() AS database_name,
    current_user AS database_user,
    version() AS postgres_version,
    now() AS executed_at;


-- ==============================================================================
-- A. OVERALL DISTRIBUTION BY RECORDING HOSTNAME / DOMAIN (ALL HISTORICAL DATA)
-- ==============================================================================
SELECT '=== A. OVERALL RECORDING VOLUME BY DOMAIN (ALL TIME) ===' AS section;

WITH parsed_urls AS (
    SELECT 
        CASE 
            WHEN recording_url IS NULL OR trim(recording_url) = '' THEN '(NULL_OR_EMPTY)'
            ELSE COALESCE(substring(trim(recording_url) from '^https?://([^/?#]+)'), '(MALFORMED_URL)')
        END AS recording_host,
        call_timestamp
    FROM bm_mass_evaluation_results
)
SELECT 
    recording_host,
    count(*) AS total_records,
    ROUND((count(*)::numeric / NULLIF(sum(count(*)) OVER (), 0)) * 100, 2) AS percentage_of_total,
    min(call_timestamp) AS earliest_call_timestamp,
    max(call_timestamp) AS latest_call_timestamp
FROM parsed_urls
GROUP BY recording_host
ORDER BY total_records DESC;


-- ==============================================================================
-- B. MONTHLY EVOLUTION BY RECORDING DOMAIN (2026 TO DATE)
-- Detects exactly in which month new recording domains appeared.
-- ==============================================================================
SELECT '=== B. MONTHLY EVOLUTION BY DOMAIN (2026) ===' AS section;

WITH monthly_data AS (
    SELECT 
        to_char(call_timestamp, 'YYYY-MM') AS call_month,
        CASE 
            WHEN recording_url IS NULL OR trim(recording_url) = '' THEN '(NULL_OR_EMPTY)'
            WHEN lower(recording_url) LIKE '%twilio.com%' THEN 'api.twilio.com'
            WHEN lower(recording_url) LIKE '%grabaciones.bostonmedical.info%' OR lower(recording_url) LIKE '%bostonmedical.info%' THEN 'grabaciones.bostonmedical.info'
            WHEN lower(recording_url) LIKE '%cloudtalk.io%' THEN 'cloudtalk.io'
            ELSE COALESCE(substring(trim(recording_url) from '^https?://([^/?#]+)'), '(OTHER_OR_MALFORMED)')
        END AS provider_group
    FROM bm_mass_evaluation_results
    WHERE call_timestamp >= '2026-01-01'
)
SELECT 
    call_month,
    provider_group,
    count(*) AS total_calls,
    ROUND((count(*)::numeric / NULLIF(sum(count(*)) OVER (PARTITION BY call_month), 0)) * 100, 2) AS month_share_pct
FROM monthly_data
GROUP BY call_month, provider_group
ORDER BY call_month DESC, total_calls DESC;


-- ==============================================================================
-- C. DISTRIBUTION BY CLINICAL SERVICE & DOMAIN (CALLS FROM 2026-07-01 ONWARDS)
-- Leverages idx_mass_eval_results_call_timestamp index.
-- Detects if the recording migration is service-specific (e.g. Front vs EXPAC vs Comerciales).
-- ==============================================================================
SELECT '=== C. DISTRIBUTION BY CLINICAL SERVICE (SINCE 2026-07-01) ===' AS section;

WITH recent_calls AS (
    SELECT 
        r.company_id,
        r.service_id,
        COALESCE(r.service_name, s.service_name, 'Service #' || r.service_id::text, '(NO_SERVICE)') AS service_label,
        CASE 
            WHEN r.recording_url IS NULL OR trim(r.recording_url) = '' THEN '(NULL_OR_EMPTY)'
            WHEN lower(r.recording_url) LIKE '%twilio.com%' THEN 'api.twilio.com'
            WHEN lower(r.recording_url) LIKE '%grabaciones.bostonmedical.info%' OR lower(r.recording_url) LIKE '%bostonmedical.info%' THEN 'grabaciones.bostonmedical.info'
            WHEN lower(r.recording_url) LIKE '%cloudtalk.io%' THEN 'cloudtalk.io'
            ELSE COALESCE(substring(trim(r.recording_url) from '^https?://([^/?#]+)'), '(OTHER)')
        END AS provider_domain,
        r.call_timestamp
    FROM bm_mass_evaluation_results r
    LEFT JOIN bm_services s ON s.service_id = r.service_id
    WHERE r.call_timestamp >= '2026-07-01'
)
SELECT 
    service_label,
    provider_domain,
    count(*) AS call_count,
    ROUND((count(*)::numeric / NULLIF(sum(count(*)) OVER (PARTITION BY service_label), 0)) * 100, 2) AS service_share_pct,
    min(call_timestamp) AS first_call_seen,
    max(call_timestamp) AS last_call_seen
FROM recent_calls
GROUP BY service_label, provider_domain
ORDER BY service_label, call_count DESC;


-- ==============================================================================
-- D. FIRST AND LAST RECORDING TIMESTAMP OBSERVED PER DOMAIN
-- Pinpoints the exact transition date for each host.
-- ==============================================================================
SELECT '=== D. TRANSITION TIMELINE PER DOMAIN (FIRST & LAST SEEN) ===' AS section;

WITH domain_timeline AS (
    SELECT 
        CASE 
            WHEN recording_url IS NULL OR trim(recording_url) = '' THEN '(NULL_OR_EMPTY)'
            ELSE COALESCE(substring(trim(recording_url) from '^https?://([^/?#]+)'), '(MALFORMED_URL)')
        END AS recording_host,
        call_timestamp
    FROM bm_mass_evaluation_results
    WHERE call_timestamp IS NOT NULL
)
SELECT 
    recording_host,
    min(call_timestamp) AS first_seen_at,
    max(call_timestamp) AS last_seen_at,
    count(*) AS total_volume,
    CASE 
        WHEN max(call_timestamp) >= now() - interval '7 days' THEN 'ACTIVE_RECENTLY'
        ELSE 'INACTIVE_OR_LEGACY'
    END AS activity_status
FROM domain_timeline
GROUP BY recording_host
ORDER BY last_seen_at DESC;


-- ==============================================================================
-- E. FOCUSED WINDOW: CALLS FROM 2026-08-01 ONWARDS (TWILIO VS NON-TWILIO)
-- Highlights whether calls from August onwards are still in Twilio or moved.
-- ==============================================================================
SELECT '=== E. RECENT CALLS BREAKDOWN (SINCE 2026-08-01) ===' AS section;

WITH august_calls AS (
    SELECT 
        CASE 
            WHEN recording_url IS NULL OR trim(recording_url) = '' THEN '(NULL_OR_EMPTY)'
            WHEN lower(recording_url) LIKE '%twilio.com%' THEN 'api.twilio.com'
            ELSE COALESCE(substring(trim(recording_url) from '^https?://([^/?#]+)'), '(OTHER)')
        END AS host_category,
        call_timestamp
    FROM bm_mass_evaluation_results
    WHERE call_timestamp >= '2026-08-01'
)
SELECT 
    host_category,
    count(*) AS calls_since_august,
    ROUND((count(*)::numeric / NULLIF(sum(count(*)) OVER (), 0)) * 100, 2) AS percentage_share,
    min(call_timestamp) AS earliest_call,
    max(call_timestamp) AS latest_call
FROM august_calls
GROUP BY host_category
ORDER BY calls_since_august DESC;


-- ==============================================================================
-- F. AUDIT OF NULL, EMPTY, OR MISSING RECORDING URLS
-- Identifies if recent calls are arriving with empty recording_url but valid cloudtalk_id.
-- ==============================================================================
SELECT '=== F. NULL / EMPTY RECORDING URL AUDIT (SINCE 2026-07-01) ===' AS section;

SELECT 
    to_char(call_timestamp, 'YYYY-MM') AS call_month,
    count(*) AS total_calls,
    count(CASE WHEN recording_url IS NULL OR trim(recording_url) = '' THEN 1 END) AS null_or_empty_recording_urls,
    count(CASE 
        WHEN (recording_url IS NULL OR trim(recording_url) = '') 
         AND (hubspot_metadata->>'cloudtalk_id' IS NOT NULL OR hubspot_metadata->>'hs_call_external_id' IS NOT NULL) 
        THEN 1 
    END) AS empty_url_with_cloudtalk_external_id,
    ROUND(
        (count(CASE WHEN recording_url IS NULL OR trim(recording_url) = '' THEN 1 END)::numeric / NULLIF(count(*), 0)) * 100, 
        2
    ) AS null_url_percentage
FROM bm_mass_evaluation_results
WHERE call_timestamp >= '2026-07-01'
GROUP BY to_char(call_timestamp, 'YYYY-MM')
ORDER BY call_month DESC;


-- ==============================================================================
-- G. TWILIO VS NON-TWILIO RATIO: BEFORE VS AFTER 2026-08-01
-- Clear comparative diagnostic to verify the reported August storage switch.
-- ==============================================================================
SELECT '=== G. TWILIO VS NON-TWILIO RATIO (BEFORE VS AFTER AUGUST 2026) ===' AS section;

WITH comparative_periods AS (
    SELECT 
        CASE 
            WHEN call_timestamp < '2026-08-01' THEN '1. BEFORE_AUGUST_2026'
            ELSE '2. FROM_AUGUST_2026_ONWARDS'
        END AS time_period,
        CASE 
            WHEN recording_url IS NULL OR trim(recording_url) = '' THEN 'NULL_OR_EMPTY'
            WHEN lower(recording_url) LIKE '%twilio.com%' THEN 'TWILIO'
            ELSE 'NON_TWILIO'
        END AS source_type
    FROM bm_mass_evaluation_results
    WHERE call_timestamp >= '2026-05-01'  -- Comparable 3-month lookback prior to August
)
SELECT 
    time_period,
    source_type,
    count(*) AS call_count,
    ROUND((count(*)::numeric / NULLIF(sum(count(*)) OVER (PARTITION BY time_period), 0)) * 100, 2) AS period_share_pct
FROM comparative_periods
GROUP BY time_period, source_type
ORDER BY time_period, call_count DESC;


-- ==============================================================================
-- H. SAFE SANITIZED SAMPLE (10-20 RECORDS PER DOMAIN)
-- STRICT PRIVACY: NEVER outputs query strings, ?f=..., tokens, or authentication signatures.
-- Outputs ONLY: call_timestamp, service_id, call_id, hostname, sanitized_path.
-- ==============================================================================
SELECT '=== H. SANITIZED CALL SAMPLE PER DOMAIN (MAX 15 ROWS PER DOMAIN) ===' AS section;

WITH partitioned_sample AS (
    SELECT 
        call_timestamp,
        company_id,
        service_id,
        call_id,
        CASE 
            WHEN recording_url IS NULL OR trim(recording_url) = '' THEN '(NULL_OR_EMPTY)'
            ELSE COALESCE(substring(trim(recording_url) from '^https?://([^/?#]+)'), '(MALFORMED_URL)')
        END AS domain_name,
        CASE 
            WHEN recording_url IS NULL OR trim(recording_url) = '' THEN '(NO_PATH)'
            ELSE COALESCE(substring(trim(recording_url) from '^https?://[^/?#]+(/[^?#]*)'), '/')
        END AS sanitized_path,
        status,
        row_number() OVER (
            PARTITION BY 
                CASE 
                    WHEN recording_url IS NULL OR trim(recording_url) = '' THEN '(NULL_OR_EMPTY)'
                    ELSE COALESCE(substring(trim(recording_url) from '^https?://([^/?#]+)'), '(MALFORMED_URL)')
                END
            ORDER BY call_timestamp DESC NULLS LAST
        ) AS rank_in_domain
    FROM bm_mass_evaluation_results
    WHERE call_timestamp >= '2026-07-01'
)
SELECT 
    domain_name,
    call_timestamp,
    service_id,
    call_id,
    sanitized_path,
    status
FROM partitioned_sample
WHERE rank_in_domain <= 15
ORDER BY domain_name, call_timestamp DESC;


-- ==============================================================================
-- I. AUXILIARY AUDIT: INDIVIDUAL / LABORATORY ANALYSES (bm_analyses)
-- Check if laboratory test evaluations also exhibit the domain transition.
-- ==============================================================================
SELECT '=== I. LABORATORY ANALYSES RECORDING SOURCES (bm_analyses) ===' AS section;

WITH lab_analyses AS (
    SELECT 
        analysis_id,
        call_id,
        call_timestamp,
        CASE 
            WHEN payload->>'recording_url' IS NOT NULL THEN payload->>'recording_url'
            WHEN payload->'hubspot_data'->>'recording_url' IS NOT NULL THEN payload->'hubspot_data'->>'recording_url'
            ELSE NULL
        END AS effective_recording_url
    FROM bm_analyses
    WHERE created_at >= '2026-07-01'
),
lab_domains AS (
    SELECT 
        CASE 
            WHEN effective_recording_url IS NULL OR trim(effective_recording_url) = '' THEN '(NULL_OR_EMPTY)'
            ELSE COALESCE(substring(trim(effective_recording_url) from '^https?://([^/?#]+)'), '(MALFORMED_URL)')
        END AS lab_domain,
        call_timestamp
    FROM lab_analyses
)
SELECT 
    lab_domain,
    count(*) AS total_lab_calls,
    min(call_timestamp) AS first_seen,
    max(call_timestamp) AS last_seen
FROM lab_domains
GROUP BY lab_domain
ORDER BY total_lab_calls DESC;


-- ==============================================================================
-- J. AUXILIARY AUDIT: CLOUDTALK EVALUATION JOBS (bm_cloudtalk_evaluation_jobs)
-- Verifies status of direct CloudTalk webhook ingestion jobs.
-- ==============================================================================
SELECT '=== J. CLOUDTALK EVALUATION JOBS STATUS ===' AS section;

SELECT 
    status,
    last_error_code,
    count(*) AS job_count,
    min(created_at) AS first_job_created,
    max(created_at) AS last_job_created
FROM bm_cloudtalk_evaluation_jobs
GROUP BY status, last_error_code
ORDER BY job_count DESC;

-- ==============================================================================
-- END OF READ-ONLY AUDIT TRANSACTION (ALWAYS ROLLBACK TO GUARANTEE READ-ONLY)
-- ==============================================================================
ROLLBACK;
