-- v023_ai_usage_events.sql
-- Table to capture Gemini usage metadata and technical metrics per call/session.
-- Non-destructive, idempotent, with tenant isolation.

CREATE TABLE IF NOT EXISTS public.bm_ai_usage_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    company_id INTEGER NULL REFERENCES public.bm_companies(company_id) ON DELETE SET NULL,
    service_id INTEGER NULL REFERENCES public.bm_services(service_id) ON DELETE SET NULL,

    module TEXT NULL,
    operation TEXT NULL,
    provider TEXT NOT NULL DEFAULT 'gemini',
    model TEXT NULL,
    credential_alias TEXT NULL,

    request_id TEXT NULL,

    call_id TEXT NULL,
    mass_analysis_id INTEGER NULL,
    cycle_id INTEGER NULL,
    trainer_session_id INTEGER NULL,

    prompt_token_count INTEGER NULL,
    candidates_token_count INTEGER NULL,
    total_token_count INTEGER NULL,
    cached_content_token_count INTEGER NULL,
    thoughts_token_count INTEGER NULL,

    input_text_tokens INTEGER NULL,
    input_audio_tokens INTEGER NULL,
    output_text_tokens INTEGER NULL,
    output_audio_tokens INTEGER NULL,

    audio_duration_seconds NUMERIC(10, 2) NULL,
    execution_latency_ms INTEGER NULL,

    status TEXT NOT NULL DEFAULT 'success',
    attempt_number INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_created_at
    ON public.bm_ai_usage_events(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_company_created
    ON public.bm_ai_usage_events(company_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_comp_serv_created
    ON public.bm_ai_usage_events(company_id, service_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_module_created
    ON public.bm_ai_usage_events(module, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_model_created
    ON public.bm_ai_usage_events(model, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_call_id
    ON public.bm_ai_usage_events(call_id);
