-- ==============================================================================
-- scripts/neutralize_staging_db.sql
--
-- Neutralization script for newly cloned STAGING / PRUEBAS PostgreSQL database.
-- Disarms all background schedulers, automated recurring jobs, and active runs 
-- copied from Production to prevent any accidental side effects or API calls.
--
-- STRICT SAFETY GUARANTEE:
-- This script contains an immediate execution guard checking current_database().
-- It WILL ABORT IMMEDIATELY if executed against any production database.
-- ==============================================================================

\set ON_ERROR_STOP on

BEGIN;

-- ------------------------------------------------------------------------------
-- 1. ABSOLUTE SAFETY GUARD AGAINST PRODUCTION EXECUTION
-- ------------------------------------------------------------------------------
DO $$
DECLARE
    db_name TEXT := current_database();
BEGIN
    -- 1. Must contain an explicit staging/test indicator (case-insensitive)
    IF lower(db_name) !~ '(staging|pruebas|test|dev)' THEN
        RAISE EXCEPTION 'CRITICAL SAFETY VIOLATION: Current database "%" does NOT contain a permitted staging/pruebas marker (staging, pruebas, test, dev). Execution aborted immediately with zero changes!', db_name;
    END IF;

    -- 2. Explicitly blacklisted production database names
    IF lower(db_name) IN ('n8n', 'speechbm', 'production', 'prod', 'bm_analysis', 'postgres') THEN
        RAISE EXCEPTION 'CRITICAL SAFETY VIOLATION: Current database "%" matches a protected production database name. Execution aborted immediately with zero changes!', db_name;
    END IF;

    RAISE NOTICE 'Safety check passed: Connected to designated staging/test database "%"', db_name;
END $$;

-- ------------------------------------------------------------------------------
-- 2. NEUTRALIZE MASS EVALUATION JOBS & SCHEDULERS
-- ------------------------------------------------------------------------------
-- Disable automated schedules for all mass evaluation jobs while preserving
-- job configurations, filters, prompt mappings, and metadata for manual testing.
UPDATE bm_mass_evaluation_jobs
SET schedule_enabled = false;

-- ------------------------------------------------------------------------------
-- 3. NEUTRALIZE MASS ANALYSIS AUTOMATIONS
-- ------------------------------------------------------------------------------
-- Disable active recurring window automations while preserving automation definitions.
UPDATE bm_mass_analysis_automations
SET is_active = false;

-- ------------------------------------------------------------------------------
-- 4. NEUTRALIZE ACTIVE / INCOMPLETE MASS EVALUATION RUNS
-- ------------------------------------------------------------------------------
-- Transition any runs that were in-flight during the snapshot to terminal 'cancelled'.
-- Completed, failed, or previously cancelled runs are left completely intact.
UPDATE bm_mass_evaluation_runs
SET status = 'cancelled',
    error_message = COALESCE(error_message || ' | ', '') || 'Neutralizado para entorno Staging'
WHERE status IN ('running', 'queued', 'pending', 'cancelling');

-- ------------------------------------------------------------------------------
-- 5. NEUTRALIZE ACTIVE / INCOMPLETE MASS AUTOMATION RUNS
-- ------------------------------------------------------------------------------
-- Transition any automation runs in progress during the snapshot to terminal 'failed'.
UPDATE bm_mass_analysis_automation_runs
SET status = 'failed',
    error_message = COALESCE(error_message || ' | ', '') || 'Neutralizado para entorno Staging'
WHERE status IN ('running', 'queued', 'pending');

-- ------------------------------------------------------------------------------
-- 6. NEUTRALIZE PERSONALIZED TRAINING SCHEDULERS & SETTINGS
-- ------------------------------------------------------------------------------
-- Disable automatic training generation crons across all services and teams.
UPDATE bm_training_schedulers
SET is_active = false;

UPDATE bm_training_scheduler_settings
SET is_enabled = false;

-- ------------------------------------------------------------------------------
-- 7. NEUTRALIZE ACTIVE / IN-PROGRESS TRAINING RUNS
-- ------------------------------------------------------------------------------
-- Mark any in-flight training generation runs as cancelled.
UPDATE bm_training_runs
SET status = 'cancelled',
    error_message = COALESCE(error_message || ' | ', '') || 'Neutralizado para entorno Staging'
WHERE status IN ('running', 'in_progress', 'pending');

-- ------------------------------------------------------------------------------
-- 8. NEUTRALIZE RESUMABLE CLOUDTALK JOBS
-- ------------------------------------------------------------------------------
-- Jobs waiting for recording or in retry loop are set to terminal 'skipped'
-- to prevent the background CloudTalk worker from fetching audio from telephony.
UPDATE bm_cloudtalk_evaluation_jobs
SET status = 'skipped',
    last_error_message = COALESCE(last_error_message || ' | ', '') || 'Neutralizado para entorno Staging'
WHERE status IN ('pending', 'waiting_recording', 'processing');

-- ------------------------------------------------------------------------------
-- 9. PURGE ACTIVE PASSWORD RESET TOKENS
-- ------------------------------------------------------------------------------
-- Delete active password recovery tokens and clear user token references.
-- NOTE: User accounts, passwords hashes, emails, roles, and permissions are
-- 100% PRESERVED so users can log in to Staging with their existing credentials.
DELETE FROM bm_password_reset_tokens;

UPDATE bm_users
SET reset_token = NULL,
    reset_token_expires_at = NULL;

-- ------------------------------------------------------------------------------
-- 10. NEUTRALIZATION VERIFICATION SUMMARY (SAFE - NO PII)
-- ------------------------------------------------------------------------------
SELECT
    'Neutralization Summary' AS check_type,
    current_database() AS database_name,
    (SELECT count(*) FROM bm_mass_evaluation_jobs WHERE schedule_enabled = false) AS mass_jobs_disabled,
    (SELECT count(*) FROM bm_mass_analysis_automations WHERE is_active = false) AS automations_disabled,
    (SELECT count(*) FROM bm_training_schedulers WHERE is_active = false) AS training_schedulers_disabled,
    (SELECT count(*) FROM bm_training_scheduler_settings WHERE is_enabled = false) AS training_settings_disabled,
    (SELECT count(*) FROM bm_mass_evaluation_runs WHERE status IN ('running', 'queued', 'pending', 'cancelling')) AS remaining_active_mass_runs,
    (SELECT count(*) FROM bm_mass_analysis_automation_runs WHERE status IN ('running', 'queued', 'pending')) AS remaining_active_automation_runs,
    (SELECT count(*) FROM bm_training_runs WHERE status IN ('running', 'in_progress', 'pending')) AS remaining_active_training_runs,
    (SELECT count(*) FROM bm_cloudtalk_evaluation_jobs WHERE status IN ('pending', 'waiting_recording', 'processing')) AS remaining_resumable_cloudtalk_jobs,
    (SELECT count(*) FROM bm_password_reset_tokens) AS remaining_reset_tokens,
    (SELECT count(*) FROM bm_users WHERE reset_token IS NOT NULL) AS remaining_users_with_reset_token;

COMMIT;
