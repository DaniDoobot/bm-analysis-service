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

-- 3. Seed official baseline pricing

-- 3.1. Gemini 3.8 Flash - Window 1: Launch / Promotional rates up to 2026-12-31 23:59:59 UTC
-- Input: $0.75 / 1M (general input: text & audio)
-- Output: $3.75 / 1M (including thinking tokens)
-- Context Caching: $0.075 / 1M
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-3.8-flash', '2026-06-01 00:00:00+00', '2027-01-01 00:00:00+00', 'USD',
       0.7500, 0.7500, 3.7500, 3.7500, 0.0750, NULL, 'gemini-3.8-flash-2026', TRUE, 'Gemini 3.8 Flash promotional rates until 2026-12-31'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing WHERE provider = 'gemini' AND model = 'gemini-3.8-flash' AND pricing_version = 'gemini-3.8-flash-2026'
);

-- 3.2. Gemini 3.8 Flash - Window 2: Standard rates from 2027-01-01 00:00:00 UTC onward
-- Input: $1.50 / 1M (general input: text & audio)
-- Output: $7.50 / 1M (including thinking tokens)
-- Context Caching: $0.15 / 1M
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-3.8-flash', '2027-01-01 00:00:00+00', NULL, 'USD',
       1.5000, 1.5000, 7.5000, 7.5000, 0.1500, NULL, 'gemini-3.8-flash-2027', TRUE, 'Gemini 3.8 Flash standard rates from 2027-01-01'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing WHERE provider = 'gemini' AND model = 'gemini-3.8-flash' AND pricing_version = 'gemini-3.8-flash-2027'
);

-- 3.3. Gemini 3.1 Flash Live Preview - Multimodal WebSocket streaming rates
-- Input Text: $0.75 / 1M
-- Input Audio: $3.00 / 1M
-- Output Text: $4.50 / 1M
-- Output Audio: $12.00 / 1M
-- No context caching
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-3.1-flash-live-preview', '2026-06-01 00:00:00+00', NULL, 'USD',
       0.7500, 3.0000, 4.5000, 12.0000, NULL, NULL, 'gemini-live-v1', TRUE, 'Gemini 3.1 Flash Live Preview multimodal rates'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing WHERE provider = 'gemini' AND model = 'gemini-3.1-flash-live-preview'
);
