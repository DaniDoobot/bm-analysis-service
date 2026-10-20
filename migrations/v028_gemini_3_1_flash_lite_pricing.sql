-- v028_gemini_3_1_flash_lite_pricing.sql
-- Seed official pricing for Gemini 3.1 Flash-Lite (model: gemini-3.1-flash-lite).
-- Non-destructive, idempotent.

INSERT INTO public.bm_ai_model_pricing (
    provider, model, effective_from, effective_to, currency,
    input_text_price_per_million, input_audio_price_per_million,
    output_text_price_per_million, output_audio_price_per_million,
    cached_input_price_per_million, thoughts_price_per_million,
    audio_price_per_minute, pricing_version, is_active, description
)
SELECT 'gemini', 'gemini-3.1-flash-lite', '2025-06-01 00:00:00+00', '2027-05-08 00:00:00+00', 'USD',
       0.2500, 0.5000, 1.5000, 1.5000, 0.0250, NULL, NULL, 'gemini-3.1-flash-lite-v1', TRUE,
       'Gemini 3.1 Flash-Lite official rates: $0.25/1M in text, $0.50/1M in audio, $0.025/1M cached, $1.50/1M out'
WHERE NOT EXISTS (
    SELECT 1 FROM public.bm_ai_model_pricing
    WHERE provider = 'gemini' AND model = 'gemini-3.1-flash-lite' AND pricing_version = 'gemini-3.1-flash-lite-v1'
);
