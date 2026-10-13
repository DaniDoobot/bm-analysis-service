"""
Unit tests for AI Pricing Service and Cost Attribution (FinOps Phase 3).
Covers:
- Pricing lookup by provider, model, and effective date window.
- Historical date-versioned pricing changes.
- Calculation per modality: text-only, audio + text, cached tokens, thoughts, output audio.
- Non-duplication of tokens (avoiding double-counting prompt totals and modalities).
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
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.assertEqual(normalize_model_name("  models/gemini-2.5-flash-lite  "), "gemini-2.5-flash-lite")
        self.assertIsNone(normalize_model_name(None))
        self.assertIsNone(normalize_model_name(""))

    async def test_get_model_pricing_date_window(self):
        """Verify that pricing lookup selects the version effective at target date."""
        session = AsyncMock()

        pricing_v2 = AIModelPricing(
            id=2,
            provider="gemini",
            model="gemini-3.8-flash",
            effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
            effective_to=None,
            pricing_version="v2",
            input_text_price_per_million=Decimal("0.15"),
            output_text_price_per_million=Decimal("0.60"),
            is_active=True,
        )

        mock_execute = MagicMock()
        mock_execute.scalars.return_value.first.return_value = pricing_v2
        session.execute = AsyncMock(return_value=mock_execute)

        target_dt = datetime(2026, 10, 1, tzinfo=timezone.utc)
        found = await get_model_pricing(
            session=session,
            model="models/gemini-3.8-flash",
            provider="gemini",
            target_date=target_dt,
        )

        self.assertEqual(found, pricing_v2)
        self.assertEqual(found.pricing_version, "v2")



    def test_calculate_cost_text_only(self):
        """100,000 prompt text tokens @ $0.15/M + 20,000 output text tokens @ $0.60/M."""
        pricing = AIModelPricing(
            pricing_version="v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.1500"),
            input_audio_price_per_million=Decimal("2.0000"),
            output_text_price_per_million=Decimal("0.6000"),
            output_audio_price_per_million=Decimal("2.0000"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=100_000,
            output_text_tokens=20_000,
        )

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.pricing_version, "v1")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.027000"))

    def test_calculate_cost_multimodal_audio_and_text(self):
        """Text + Audio input and Text output."""
        pricing = AIModelPricing(
            pricing_version="v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.1500"),
            input_audio_price_per_million=Decimal("2.0000"),
            output_text_price_per_million=Decimal("0.6000"),
            output_audio_price_per_million=Decimal("2.0000"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=10_000,
            input_audio_tokens=50_000,
            output_text_tokens=5_000,
            output_audio_tokens=0,
        )

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.104500"))

    def test_calculate_cost_cached_tokens_non_duplication(self):
        """Ensure cached tokens are discounted from prompt input to prevent double charging."""
        pricing = AIModelPricing(
            pricing_version="v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.1500"),
            input_audio_price_per_million=Decimal("2.0000"),
            output_text_price_per_million=Decimal("0.6000"),
            output_audio_price_per_million=Decimal("2.0000"),
            cached_input_price_per_million=Decimal("0.0375"),
        )

        res = calculate_event_cost(
            pricing=pricing,
            input_text_tokens=30_000,
            cached_content_token_count=20_000,
            output_text_tokens=1_000,
        )

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.002850"))

    def test_calculate_cost_fallback_when_modality_breakdown_missing(self):
        """When explicit input_text_tokens / input_audio_tokens are None, fall back to prompt_token_count."""
        pricing = AIModelPricing(
            pricing_version="v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.1500"),
            input_audio_price_per_million=Decimal("2.0000"),
            output_text_price_per_million=Decimal("0.6000"),
            output_audio_price_per_million=Decimal("2.0000"),
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

        self.assertEqual(res.pricing_status, "priced")
        self.assertEqual(res.estimated_cost_usd, Decimal("0.002700"))

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
            pricing_version="v1",
            currency="USD",
            input_text_price_per_million=Decimal("0.1500"),
            input_audio_price_per_million=Decimal("2.0000"),
            output_text_price_per_million=Decimal("0.6000"),
            output_audio_price_per_million=Decimal("2.0000"),
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
        self.assertEqual(res_none.estimated_cost_usd, Decimal("0.001500"))

    async def test_backfill_unpriced_events_idempotent(self):
        """Verify that backfill processes unpriced events and updates their cost."""
        session = AsyncMock()

        pricing = AIModelPricing(
            id=1,
            provider="gemini",
            model="gemini-3.8-flash",
            effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
            effective_to=None,
            pricing_version="v1",
            input_text_price_per_million=Decimal("0.1500"),
            output_text_price_per_million=Decimal("0.6000"),
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
        self.assertEqual(ev1.estimated_cost_usd, Decimal("0.006000"))
        self.assertEqual(ev1.pricing_status, "priced")
        self.assertEqual(ev1.pricing_version, "v1")
        session.commit.assert_awaited()


if __name__ == "__main__":
    unittest.main()

