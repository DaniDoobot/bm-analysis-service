-- v024_ai_model_pricing.sql
-- Migration for Phase 3 FinOps: AI Model Pricing Catalog and Cost Attribution.
-- Non-destructive, idempotent, supporting date-versioned pricing.

-- 1. Create pricing catalog table
CREATE TABLE IF NOT EXISTS public.bm_ai_model_pricing (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'gemini',
    model TEXT NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
    input_text_price_per_million NUMERIC(10, 4) NOT NULL DEFAULT 0,
    input_audio_price_per_million NUMERIC(10, 4) NOT NULL DEFAULT 0,
    output_text_price_per_million NUMERIC(10, 4) NOT NULL DEFAULT 0,
    output_audio_price_per_million NUMERIC(10, 4) NOT NULL DEFAULT 0,
    cached_input_price_per_million NUMERIC(10, 4) NULL,
    thoughts_price_per_million NUMERIC(10, 4) NULL,
    pricing_version VARCHAR(50) NOT NULL DEFAULT 'v1',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bm_ai_model_pricing_lookup
    ON public.bm_ai_model_pricing(provider, model, effective_from DESC);

CREATE INDEX IF NOT EXISTS idx_bm_ai_model_pricing_active
    ON public.bm_ai_model_pricing(is_active);

-- 2. Extend bm_ai_usage_events with cost tracking columns
ALTER TABLE public.bm_ai_usage_events
    ADD COLUMN IF NOT EXISTS estimated_cost_usd NUMERIC(12, 6) NULL,
    ADD COLUMN IF NOT EXISTS pricing_version VARCHAR(50) NULL,
    ADD COLUMN IF NOT EXISTS pricing_status VARCHAR(20) NOT NULL DEFAULT 'unpriced';

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_pricing_status
    ON public.bm_ai_usage_events(pricing_status, created_at DESC);

-- 3. Seed initial baseline pricing for active models
-- Note: effective_from '2025-01-01 00:00:00+00' covers all historical events
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-3.8-flash', '2025-01-01 00:00:00+00', NULL, 'USD',
       0.1500, 2.0000, 0.6000, 2.0000, 0.0375, 0.6000, 'gemini-flash-v1', TRUE, 'Gemini 3.8 / 2.5 / 2.0 Flash standard rates'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing WHERE provider = 'gemini' AND model = 'gemini-3.8-flash'
);

INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-2.5-flash-lite', '2025-01-01 00:00:00+00', NULL, 'USD',
       0.0750, 1.0000, 0.3000, 1.0000, 0.01875, 0.3000, 'gemini-flash-lite-v1', TRUE, 'Gemini Flash Lite standard rates'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing WHERE provider = 'gemini' AND model = 'gemini-2.5-flash-lite'
);

INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-3.1-flash-live-preview', '2025-01-01 00:00:00+00', NULL, 'USD',
       0.3000, 3.0000, 1.2000, 3.0000, 0.0750, 1.2000, 'gemini-live-v1', TRUE, 'Gemini Live Multimodal standard rates'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing WHERE provider = 'gemini' AND model = 'gemini-3.1-flash-live-preview'
);
