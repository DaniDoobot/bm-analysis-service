-- Migration v026: bm_cloudtalk_evaluation_jobs
-- Idempotent creation of CloudTalk evaluation jobs queue

CREATE TABLE IF NOT EXISTS bm_cloudtalk_evaluation_jobs (
    id BIGSERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES bm_companies(company_id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES bm_services(service_id) ON DELETE CASCADE,
    cloudtalk_call_id TEXT NOT NULL,
    call_uuid TEXT NULL,
    talking_time INTEGER NULL,
    direction TEXT NULL,
    agent_id TEXT NULL,
    agent_name TEXT NULL,
    agent_email TEXT NULL,
    external_number TEXT NULL,
    internal_number TEXT NULL,
    started_at TIMESTAMPTZ NULL,
    ended_at TIMESTAMPTZ NULL,
    webhook_payload JSONB NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TIMESTAMPTZ NULL,
    analysis_id BIGINT NULL REFERENCES bm_analyses(analysis_id) ON DELETE SET NULL,
    last_error_code TEXT NULL,
    last_error_message TEXT NULL,
    webhook_received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_cloudtalk_eval_job_call UNIQUE (company_id, service_id, cloudtalk_call_id)
);

CREATE INDEX IF NOT EXISTS ix_bm_cloudtalk_eval_jobs_status_next
    ON bm_cloudtalk_evaluation_jobs (status, next_attempt_at);

CREATE INDEX IF NOT EXISTS ix_bm_cloudtalk_eval_jobs_call_id
    ON bm_cloudtalk_evaluation_jobs (cloudtalk_call_id);
