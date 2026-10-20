-- v025_laboratory_models_and_pricing.sql
-- Migration for Laboratory Multi-Provider AI Model Selector & FinOps Audio-Minute Pricing.
-- Non-destructive, idempotent.

-- 1. Extend bm_ai_usage_events with optional model_key for Laboratory benchmark traceability
ALTER TABLE public.bm_ai_usage_events
    ADD COLUMN IF NOT EXISTS model_key TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_bm_ai_usage_events_model_key
    ON public.bm_ai_usage_events(model_key, created_at DESC);

-- 2. Extend bm_ai_model_pricing with per-minute audio pricing for STT models (Groq, Mistral, Deepgram, Voxtral)
ALTER TABLE public.bm_ai_model_pricing
    ADD COLUMN IF NOT EXISTS audio_price_per_minute NUMERIC(10, 6) NULL;

-- 3. Seed verified official pricing entries

-- 3.1. Gemini 2.5 Flash-Lite (Official Google Cloud rates)
-- Input Text: $0.10 / 1M | Input Audio: $0.30 / 1M | Output: $0.40 / 1M | Context Caching: $0.025 / 1M
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

-- 3.2. Gemini 3.5 Flash-Lite (Official Google Cloud rates)
-- Input: $0.30 / 1M | Cached Input: $0.03 / 1M | Output including thinking: $2.50 / 1M
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-3.5-flash-lite', '2025-06-01 00:00:00+00', NULL, 'USD',
       0.3000, 0.3000, 2.5000, 2.5000, 0.0300, NULL, NULL, 'gemini-3.5-flash-lite-v1', TRUE,
       'Gemini 3.5 Flash-Lite official rates: $0.30/1M in, $0.03/1M cached, $2.50/1M out'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'gemini' AND model = 'gemini-3.5-flash-lite' AND pricing_version = 'gemini-3.5-flash-lite-v1'
);

-- 3.3. Voxtral Small (Official Mistral rates)
-- Audio: $0.004 / min | Input text: $0.10 / 1M | Output: $0.40 / 1M
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'mistral', 'voxtral-small-latest', '2025-06-01 00:00:00+00', NULL, 'USD',
       0.1000, 0.0000, 0.4000, 0.0000, NULL, NULL, 0.004000, 'mistral-voxtral-small-v1', TRUE,
       'Mistral Voxtral Small official rates: $0.004/min audio + $0.10/1M in + $0.40/1M out'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'mistral' AND model = 'voxtral-small-latest' AND pricing_version = 'mistral-voxtral-small-v1'
);

-- 3.4. Groq Whisper Large v3 Turbo ($0.04 / hour = $0.000667 / minute)
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

-- 3.5. Mistral Voxtral Mini Transcribe ($0.003 / minute official rate)
UPDATE public.bm_ai_model_pricing
SET audio_price_per_minute = 0.003000,
    pricing_version = 'mistral-voxtral-mini-stt-v2',
    description = 'Mistral Voxtral Mini Transcribe official STT rate ($0.003/min)'
WHERE provider = 'mistral' AND model = 'voxtral-mini-latest';

INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'mistral', 'voxtral-mini-latest', '2025-07-01 00:00:00+00', NULL, 'USD',
       0.0000, 0.0000, 0.0000, 0.0000, NULL, NULL, 0.003000, 'mistral-voxtral-mini-stt-v2', TRUE,
       'Mistral Voxtral Mini Transcribe official STT rate ($0.003/min)'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'mistral' AND model = 'voxtral-mini-latest'
);

-- 3.6. Deepgram Nova-3 Pre-recorded STT ($0.0043 / minute)
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

-- 3.7. OpenAI GPT-5.6 Luna (Official OpenAI rates for text evaluation)
-- Input: $0.20 / 1M | Cached Input: $0.02 / 1M | Output: $1.20 / 1M
INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'openai', 'gpt-5.6-luna', '2025-06-01 00:00:00+00', NULL, 'USD',
       0.2000, 0.0000, 1.2000, 0.0000, 0.0200, NULL, NULL, 'openai-gpt-5.6-luna-v1', TRUE,
       'OpenAI GPT-5.6 Luna official rates: $0.20/1M in, $0.02/1M cached, $1.20/1M out'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'openai' AND model = 'gpt-5.6-luna' AND pricing_version = 'openai-gpt-5.6-luna-v1'
);
