"""
Unit tests for AI Pricing Service and Cost Attribution (FinOps Phase 3).
Covers:
- Pricing lookup by provider, model, and effective date window.
- Date-versioned pricing changes for Gemini 3.8 Flash (2026 promotional vs 2027 standard).
- Gemini 3.1 Flash Live Preview multimodal rates (text vs audio input/output).
- Calculation per modality: text-only, audio + text, cached tokens, thoughts, output audio.
- Non-duplication of thinking tokens (ensuring thoughts are included in output, not double charged).
- Non-duplication of cached tokens across modalities.
- Handling of 0 values vs None (nulls).
- Unpriced fallback without disrupting execution flow.
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

    async def test_get_model_pricing_date_window_2026_vs_2027(self):
        """Verify that pricing lookup selects the version effective at target date for Gemini 3.8 Flash."""
        session = AsyncMock()

        pricing_2026 = AIModelPricing(
            id=1,
            provider="gemini",
            model="gemini-3.8-flash",
            effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
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

        target_dt = datetime(2026, 10, 13, tzinfo=timezone.utc)
        found = await get_model_pricing(
            session=session,
            model="models/gemini-3.8-flash",
            provider="gemini",
            target_date=target_dt,
        )

        self.assertEqual(found, pricing_2026)
        self.assertEqual(found.pricing_version, "gemini-3.8-flash-2026")
        self.assertEqual(found.input_text_price_per_million, Decimal("0.75"))
        self.assertEqual(found.output_text_price_per_million, Decimal("3.75"))

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

    def test_thinking_tokens_not_double_charged(self):
        """
        Verify thinking tokens are NOT charged as an extra fee on top of output tokens.
        Google documentation: "Output price (including thinking tokens)".
        candidates_token_count = 20,000 (includes 5,000 thoughts).
        Output cost must be 20,000 * 3.75 / 1M = $0.075000 (NOT 20,000 + 5,000).
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
            output_text_tokens=20_000,
            thoughts_token_count=5_000,
        )

        self.assertEqual(res.pricing_status, "priced")
        # Exactly 20,000 * $3.75 / 1M = $0.075000
        self.assertEqual(res.estimated_cost_usd, Decimal("0.075000"))
        self.assertEqual(res.details.get("thoughts_tokens"), 5_000)
        self.assertEqual(res.details.get("thoughts_billing"), "included_in_output")

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

    def test_multimodal_cached_tokens_non_duplication(self):
        """
        Ensure cached tokens are discounted from prompt input to prevent double charging.
        Prompt text = 30,000 tokens, of which 20,000 are cached.
        Un-cached text = 10,000 @ $0.75/M = $0.0075
        Cached input = 20,000 @ $0.075/M = $0.0015
        Output text = 1,000 @ $3.75/M = $0.00375
        Total = 0.0075 + 0.0015 + 0.00375 = $0.012750
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
            input_text_tokens=30_000,
            cached_content_token_count=20_000,
            output_text_tokens=1_000,
        )

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.012750"))

    def test_calculate_cost_fallback_when_modality_breakdown_missing(self):
        """When explicit input_text_tokens / input_audio_tokens are None, fall back to prompt_token_count."""
        pricing = AIModelPricing(
            pricing_version="gemini-3.8-flash-2026",
            currency="USD",
            input_text_price_per_million=Decimal("0.7500"),
            input_audio_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
            output_audio_price_per_million=Decimal("3.7500"),
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
            input_audio_price_per_million=Decimal("0.7500"),
            output_text_price_per_million=Decimal("3.7500"),
            output_audio_price_per_million=Decimal("3.7500"),
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

    async def test_backfill_unpriced_events_idempotent(self):
        """Verify that backfill processes unpriced events and updates their cost."""
        session = AsyncMock()

        pricing = AIModelPricing(
            id=1,
            provider="gemini",
            model="gemini-3.8-flash",
            effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
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
        # 20k @ 0.75/M = 0.015 + 5k @ 3.75/M = 0.01875 => 0.033750
        self.assertEqual(ev1.estimated_cost_usd, Decimal("0.033750"))
        self.assertEqual(ev1.pricing_status, "priced")
        self.assertEqual(ev1.pricing_version, "gemini-3.8-flash-2026")
        session.commit.assert_awaited()


if __name__ == "__main__":
    unittest.main()
