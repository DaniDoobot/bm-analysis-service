-- v025_laboratory_models_and_pricing.sql
-- Migration for Laboratory Multi-Provider AI Model Selector & FinOps Audio-Minute Pricing.
-- Non-destructive, idempotent.

-- 1. Extend bm_ai_usage_events with optional model_key for Laboratory benchmark traceability
ALTER TABLE public.bm_ai_usage_events
    ADD COLUMN IF NOT EXISTS model_key TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_model_key
    ON public.bm_ai_usage_events(model_key, created_at DESC);

-- 2. Extend bm_ai_model_pricing with per-minute audio pricing for STT models (Groq, Mistral, Deepgram)
ALTER TABLE public.bm_ai_model_pricing
    ADD COLUMN IF NOT EXISTS audio_price_per_minute NUMERIC(10, 6) NULL;

-- 3. Seed verified official pricing entries (models without confirmed rates remain unseeded -> pricing_status='unpriced')

-- 3.1. Gemini 2.5 Flash-Lite (Official Google Cloud / Gemini API rates)
-- Input Text: $0.10 / 1M | Input Audio: $0.30 / 1M | Output Text: $0.40 / 1M | Context Caching: $0.025 / 1M
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-2.5-flash-lite', '2025-06-01 00:00:00+00', NULL, 'USD',
       0.1000, 0.3000, 0.4000, 0.4000, 0.0250, NULL, NULL, 'gemini-2.5-flash-lite-v1', TRUE,
       'Gemini 2.5 Flash-Lite official multimodal rates'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'gemini' AND model = 'gemini-2.5-flash-lite' AND pricing_version = 'gemini-2.5-flash-lite-v1'
);

-- 3.2. Groq Whisper Large v3 Turbo ($0.04 / hour = $0.000667 / minute)
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'groq', 'whisper-large-v3-turbo', '2025-01-01 00:00:00+00', NULL, 'USD',
       0.0000, 0.0000, 0.0000, 0.0000, NULL, NULL, 0.000667, 'groq-whisper-v3-turbo-v1', TRUE,
       'Groq Whisper Large v3 Turbo official STT rate ($0.04/hour = $0.000667/min)'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'groq' AND model = 'whisper-large-v3-turbo' AND pricing_version = 'groq-whisper-v3-turbo-v1'
);

-- 3.3. Mistral Voxtral Mini Transcribe ($0.002 / minute)
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'mistral', 'voxtral-mini-latest', '2025-07-01 00:00:00+00', NULL, 'USD',
       0.0000, 0.0000, 0.0000, 0.0000, NULL, NULL, 0.002000, 'mistral-voxtral-mini-stt-v1', TRUE,
       'Mistral Voxtral Mini Transcribe official STT rate ($0.002/min)'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'mistral' AND model = 'voxtral-mini-latest' AND pricing_version = 'mistral-voxtral-mini-stt-v1'
);

-- 3.4. Deepgram Nova-3 Pre-recorded STT ($0.0043 / minute)
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'deepgram', 'nova-3', '2025-01-01 00:00:00+00', NULL, 'USD',
       0.0000, 0.0000, 0.0000, 0.0000, NULL, NULL, 0.004300, 'deepgram-nova-3-v1', TRUE,
       'Deepgram Nova-3 Pre-recorded official STT rate ($0.0043/min)'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'deepgram' AND model = 'nova-3' AND pricing_version = 'deepgram-nova-3-v1'
);
