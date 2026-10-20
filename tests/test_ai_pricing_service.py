"""
Unit tests for AI Pricing Service and Cost Attribution (FinOps Phase 3).
Covers:
- Pricing lookup by provider, model, and effective date window.
- Gemini 3.8 Flash before launch (2026-08-31) -> unpriced.
- Gemini 3.8 Flash GA launch (2026-09-02) -> 2026 promotional pricing.
- Gemini 3.8 Flash standard rates from 2027-01-01.
- Thinking tokens pricing: candidates (500) + thoughts (200) = 700 billable output tokens.
- Usage metadata identity verification (total ~= prompt + candidates + thoughts).
- Cached tokens: prompt (1000) with cached (300) -> 700 net regular + 300 cached.
- Gemini 3.1 Flash Live Preview multimodal rates.
- Handling of 0 values vs None (nulls).
- Idempotent backfill utility for unpriced historical events.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.ai_usage import AIModelPricing, AIUsageEvent
from app.services.ai_pricing_service import (
    calculate_event_cost,
    get_model_pricing,
    normalize_model_name,
    backfill_unpriced_events,
    CostCalculationResult,
)


class TestAIPricingService(unittest.IsolatedAsyncioTestCase):

    def test_normalize_model_name(self):
        self.assertEqual(normalize_model_name("models/gemini-3.8-flash"), "gemini-3.8-flash")
        self.assertEqual(normalize_model_name("gemini-3.8-flash"), "gemini-3.8-flash")
        self.assertEqual(normalize_model_name("  models/gemini-3.1-flash-live-preview  "), "gemini-3.1-flash-live-preview")
        self.assertIsNone(normalize_model_name(None))
        self.assertIsNone(normalize_model_name(""))

    async def test_gemini_38_flash_before_launch_is_unpriced(self):
        """Events before GA launch date 2026-09-02 must return None (unpriced)."""
        session = AsyncMock()

        # Database returns no pricing record matching target_date 2026-08-31
        mock_execute = MagicMock()
        mock_execute.scalars.return_value.first.return_value = None
        session.execute = AsyncMock(return_value=mock_execute)

        target_dt = datetime(2026, 8, 31, tzinfo=timezone.utc)
        found = await get_model_pricing(
            session=session,
            model="models/gemini-3.8-flash",
            provider="gemini",
            target_date=target_dt,
        )

        self.assertIsNone(found)

    async def test_gemini_38_flash_at_launch_2026_09_02(self):
        """Events on or after 2026-09-02 match the 2026 promotional pricing window."""
        session = AsyncMock()

        pricing_2026 = AIModelPricing(
            id=1,
            provider="gemini",
            model="gemini-3.8-flash",
            effective_from=datetime(2026, 9, 2, tzinfo=timezone.utc),
            effective_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
            pricing_version="gemini-3.8-flash-2026",
            input_text_price_per_million=Decimal("0.75"),
            input_audio_price_per_million=Decimal("0.75"),
            output_text_price_per_million=Decimal("3.75"),
            output_audio_price_per_million=Decimal("3.75"),
            cached_input_price_per_million=Decimal("0.075"),
            is_active=True,
        )

        mock_execute = MagicMock()
        mock_execute.scalars.return_value.first.return_value = pricing_2026
        session.execute = AsyncMock(return_value=mock_execute)

        target_dt = datetime(2026, 9, 2, tzinfo=timezone.utc)
        found = await get_model_pricing(
            session=session,
            model="models/gemini-3.8-flash",
            provider="gemini",
            target_date=target_dt,
        )

        self.assertEqual(found, pricing_2026)
        self.assertEqual(found.pricing_version, "gemini-3.8-flash-2026")
        self.assertEqual(found.effective_from, datetime(2026, 9, 2, tzinfo=timezone.utc))

    def test_gemini_38_flash_2026_calculation(self):
        """
        Gemini 3.8 Flash during 2026:
        - Input rate = $0.75 / 1M
        - Output rate = $3.75 / 1M
        - Cached rate = $0.075 / 1M
        - 100,000 prompt text tokens @ $0.75/M = $0.075
        - 20,000 output text tokens @ $3.75/M = $0.075
        Total = $0.150000
        """
        pricing = AIModelPricing(
            pricing_version="gemini-3.8-flash-2026",
            currency="USD",
            input_text_price_per_million=Decimal("0.7500"),
            input_audio_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
            output_audio_price_per_million=Decimal("3.7500"),
            cached_input_price_per_million=Decimal("0.0750"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=100_000,
            output_text_tokens=20_000,
        )

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.pricing_version, "gemini-3.8-flash-2026")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.150000"))

    def test_gemini_38_flash_2027_calculation(self):
        """
        Gemini 3.8 Flash from 2027 onward:
        - Input rate = $1.50 / 1M
        - Output rate = $7.50 / 1M
        - Cached rate = $0.15 / 1M
        - 100,000 prompt text tokens @ $1.50/M = $0.15
        - 20,000 output text tokens @ $7.50/M = $0.15
        Total = $0.300000
        """
        pricing = AIModelPricing(
            pricing_version="gemini-3.8-flash-2027",
            currency="USD",
            input_text_price_per_million=Decimal("1.5000"),
            input_audio_price_per_million=Decimal("1.5000"),
            output_text_price_per_million=Decimal("7.5000"),
            output_audio_price_per_million=Decimal("7.5000"),
            cached_input_price_per_million=Decimal("0.1500"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=100_000,
            output_text_tokens=20_000,
        )

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.pricing_version, "gemini-3.8-flash-2027")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.300000"))

    def test_thinking_tokens_billed_at_output_rate(self):
        """
        Verify: candidates (500) + thoughts (200) bills output over exactly 700 tokens.
        Google pricing: "Output price (including thinking tokens)".
        candidates = 500 @ $3.75/M = $0.001875
        thoughts = 200 @ $3.75/M = $0.000750
        Total output cost = 700 / 1M * 3.75 = $0.002625
        """
        pricing = AIModelPricing(
            pricing_version="gemini-3.8-flash-2026",
            currency="USD",
            input_text_price_per_million=Decimal("0.7500"),
            input_audio_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
            output_audio_price_per_million=Decimal("3.7500"),
            cached_input_price_per_million=Decimal("0.0750"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=0,
            candidates_token_count=500,
            thoughts_token_count=200,
        )

        self.assertEqual(res.pricing_status, "priced")
        # 500 * 3.75/1M + 200 * 3.75/1M = 700 * 3.75/1M = $0.002625
        self.assertEqual(res.estimated_cost_usd, Decimal("0.002625"))
        self.assertEqual(res.details.get("candidates_tokens"), 500)
        self.assertEqual(res.details.get("thoughts_tokens"), 200)

    def test_usage_metadata_token_identity(self):
        """
        Verify the identity of Google UsageMetadata:
        totalTokenCount ~= promptTokenCount + candidatesTokenCount + thoughtsTokenCount
        """
        prompt = 1000
        candidates = 500
        thoughts = 200
        total = prompt + candidates + thoughts
        self.assertEqual(total, 1700)

        pricing = AIModelPricing(
            pricing_version="gemini-3.8-flash-2026",
            currency="USD",
            input_text_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            prompt_token_count=prompt,
            candidates_token_count=candidates,
            thoughts_token_count=thoughts,
        )

        # Input: 1000 * 0.75/1M = $0.000750
        # Output: (500 + 200) * 3.75/1M = 700 * 3.75/1M = $0.002625
        # Total = $0.003375
        self.assertEqual(res.estimated_cost_usd, Decimal("0.003375"))

    def test_cached_tokens_split(self):
        """
        Verify prompt = 1000, cached = 300:
        700 net regular input @ $0.75/M = $0.000525
        300 cached input @ $0.075/M = $0.0000225 -> rounded = $0.000023
        Total = 0.000525 + 0.0000225 = $0.0005475 -> rounded to 6 dec: $0.000548
        """
        pricing = AIModelPricing(
            pricing_version="gemini-3.8-flash-2026",
            currency="USD",
            input_text_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
            cached_input_price_per_million=Decimal("0.0750"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            prompt_token_count=1000,
            cached_content_token_count=300,
            candidates_token_count=0,
        )

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.details.get("net_prompt_tokens"), 700)
        self.assertEqual(res.details.get("cached_tokens"), 300)
        self.assertEqual(res.estimated_cost_usd, Decimal("0.000548"))

    def test_gemini_31_flash_live_preview_calculation(self):
        """
        Gemini 3.1 Flash Live Preview:
        - Input Text: $0.75 / 1M
        - Input Audio: $3.00 / 1M
        - Output Text: $4.50 / 1M
        - Output Audio: $12.00 / 1M
        Input: 10,000 text ($0.0075) + 50,000 audio ($0.1500) = $0.1575
        Output: 2,000 text ($0.0090) + 10,000 audio ($0.1200) = $0.1290
        Total = 0.1575 + 0.1290 = $0.286500
        """
        pricing = AIModelPricing(
            pricing_version="gemini-live-v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.7500"),
            input_audio_price_per_million=Decimal("3.0000"),
            output_text_price_per_million=Decimal("4.5000"),
            output_audio_price_per_million=Decimal("12.0000"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=10_000,
            input_audio_tokens=50_000,
            output_text_tokens=2_000,
            output_audio_tokens=10_000,
        )

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.pricing_version, "gemini-live-v1")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.286500"))

    def test_calculate_cost_fallback_when_modality_breakdown_missing(self):
        """When explicit input_text_tokens / input_audio_tokens are None, fall back to prompt_token_count."""
        pricing = AIModelPricing(
            pricing_version="gemini-3.8-flash-2026",
            currency="USD",
            input_text_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            prompt_token_count=10_000,
            candidates_token_count=2_000,
            input_text_tokens=None,
            input_audio_tokens=None,
            output_text_tokens=None,
            output_audio_tokens=None,
        )

        # 10k @ 0.75/M = 0.0075
        # 2k @ 3.75/M = 0.0075
        # Total = 0.015000
        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.015000"))

    def test_calculate_cost_unpriced_when_pricing_is_none(self):
        """When pricing is None, returns status 'unpriced' with None cost."""
        res = calculate_event_cost(
            pricing=None,
            input_text_tokens=10_000,
            output_text_tokens=2_000,
        )
        self.assertEqual(res.pricing_status, "unpriced")
        self.assertIsNone(res.estimated_cost_usd)
        self.assertIsNone(res.pricing_version)

    def test_calculate_cost_zero_vs_none_semantics(self):
        """Ensure explicit 0 tokens yields 0.000000 cost, whereas None yields partial."""
        pricing = AIModelPricing(
            pricing_version="gemini-3.8-flash-2026",
            currency="USD",
            input_text_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
        )

        # Both 0
        res_zero = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=0,
            output_text_tokens=0,
        )
        self.assertEqual(res_zero.pricing_status, "priced")
        self.assertEqual(res_zero.estimated_cost_usd, Decimal("0.000000"))

        # Missing output (None)
        res_none = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=10_000,
            output_text_tokens=None,
        )
        self.assertEqual(res_none.pricing_status, "partial")
        self.assertEqual(res_none.estimated_cost_usd, Decimal("0.007500"))

    def test_gemini_35_flash_lite_pricing(self):
        """Gemini 3.5 Flash-Lite official rates: $0.30/1M in, $0.03/1M cached, $2.50/1M out (including thoughts)."""
        pricing = AIModelPricing(
            provider="gemini",
            model="gemini-3.5-flash-lite",
            effective_from=datetime(2025, 6, 1, tzinfo=timezone.utc),
            pricing_version="gemini-3.5-flash-lite-v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.3000"),
            input_audio_price_per_million=Decimal("0.3000"),
            output_text_price_per_million=Decimal("2.5000"),
            output_audio_price_per_million=Decimal("2.5000"),
            cached_input_price_per_million=Decimal("0.0300"),
            is_active=True,
        )
        res = calculate_event_cost(
            pricing=pricing,
            prompt_token_count=10_000,
            cached_content_token_count=2_000,
            candidates_token_count=1_000,
            thoughts_token_count=500,
        )
        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.pricing_version, "gemini-3.5-flash-lite-v1")
        # 8,000 net prompt @ 0.30/M = 0.002400
        # 2,000 cached @ 0.03/M = 0.000060
        # 1,000 candidates @ 2.50/M = 0.002500
        # 500 thoughts @ 2.50/M = 0.001250
        # Total = 0.006210
        self.assertEqual(res.estimated_cost_usd, Decimal("0.006210"))

    def test_voxtral_small_audio_plus_tokens_pricing(self):
        """Voxtral Small: audio ($0.004/min) + input text ($0.10/1M) + output text ($0.40/1M)."""
        pricing = AIModelPricing(
            provider="mistral",
            model="voxtral-small-latest",
            effective_from=datetime(2025, 6, 1, tzinfo=timezone.utc),
            pricing_version="mistral-voxtral-small-v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.1000"),
            output_text_price_per_million=Decimal("0.4000"),
            audio_price_per_minute=Decimal("0.004000"),
            is_active=True,
        )
        res = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=5_000,
            output_text_tokens=1_000,
            audio_duration_seconds=90.0,
        )
        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.pricing_version, "mistral-voxtral-small-v1")
        # Audio: (90/60) * 0.004 = 0.006000
        # Input text: 5,000 @ 0.10/M = 0.000500
        # Output text: 1,000 @ 0.40/M = 0.000400
        # Total = 0.006900
        self.assertEqual(res.estimated_cost_usd, Decimal("0.006900"))

    def test_mistral_voxtral_mini_transcribe_rate_0003(self):
        """Mistral Voxtral Mini Transcribe: corrected official STT rate $0.003/minute."""
        pricing = AIModelPricing(
            provider="mistral",
            model="voxtral-mini-latest",
            effective_from=datetime(2025, 7, 1, tzinfo=timezone.utc),
            pricing_version="mistral-voxtral-mini-stt-v2",
            currency="USD",
            audio_price_per_minute=Decimal("0.003000"),
            is_active=True,
        )
        res = calculate_event_cost(
            pricing=pricing,
            audio_duration_seconds=120.0,
        )
        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.pricing_version, "mistral-voxtral-mini-stt-v2")
        # (120/60) * 0.003 = 0.006000
        self.assertEqual(res.estimated_cost_usd, Decimal("0.006000"))

    def test_openai_gpt_5_6_luna_pricing(self):
        """OpenAI GPT-5.6 Luna: $0.20/1M input, $0.02/1M cached, $1.20/1M output."""
        pricing = AIModelPricing(
            provider="openai",
            model="gpt-5.6-luna",
            effective_from=datetime(2025, 6, 1, tzinfo=timezone.utc),
            pricing_version="openai-gpt-5.6-luna-v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.2000"),
            output_text_price_per_million=Decimal("1.2000"),
            cached_input_price_per_million=Decimal("0.0200"),
            is_active=True,
        )
        res = calculate_event_cost(
            pricing=pricing,
            prompt_token_count=20_000,
            cached_content_token_count=5_000,
            candidates_token_count=2_000,
        )
        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.pricing_version, "openai-gpt-5.6-luna-v1")
        # 15,000 net prompt @ 0.20/M = 0.003000
        # 5,000 cached @ 0.02/M = 0.000100
        # 2,000 output @ 1.20/M = 0.002400
        # Total = 0.005500
        self.assertEqual(res.estimated_cost_usd, Decimal("0.005500"))

    async def test_backfill_unpriced_events_idempotent(self):
        """Verify that backfill processes unpriced events and updates their cost."""
        session = AsyncMock()

        pricing = AIModelPricing(
            id=1,
            provider="gemini",
            model="gemini-3.8-flash",
            effective_from=datetime(2026, 9, 2, tzinfo=timezone.utc),
            effective_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
            pricing_version="gemini-3.8-flash-2026",
            input_text_price_per_million=Decimal("0.7500"),
            input_audio_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
            output_audio_price_per_million=Decimal("3.7500"),
            cached_input_price_per_million=Decimal("0.0750"),
            is_active=True,
        )

        ev1 = AIUsageEvent(
            id=1,
            provider="gemini",
            model="gemini-3.8-flash",
            input_text_tokens=20_000,
            output_text_tokens=5_000,
            thoughts_token_count=1_000,
            estimated_cost_usd=None,
            pricing_status="unpriced",
            created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )

        # First call returns ev1, second call returns empty list
        mock_res1 = MagicMock()
        mock_res1.scalars.return_value.all.return_value = [ev1]
        mock_res2 = MagicMock()
        mock_res2.scalars.return_value.all.return_value = []

        session.execute.side_effect = [mock_res1, mock_res2]

        with patch("app.services.ai_pricing_service.get_model_pricing", AsyncMock(return_value=pricing)):
            stats = await backfill_unpriced_events(batch_size=10, session=session)

        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["priced"], 1)
        # Input: 20k @ 0.75/M = 0.015
        # Output: 5k @ 3.75/M = 0.01875
        # Thoughts: 1k @ 3.75/M = 0.00375
        # Total = 0.015 + 0.01875 + 0.00375 = 0.037500
        self.assertEqual(ev1.estimated_cost_usd, Decimal("0.037500"))
        self.assertEqual(ev1.pricing_status, "priced")
        self.assertEqual(ev1.pricing_version, "gemini-3.8-flash-2026")
        session.commit.assert_awaited()


if __name__ == "__main__":
    unittest.main()
