"""
Pricing calculation service for AI usage events (FinOps Phase 3).
Provides model pricing lookup by date, non-duplicative cost calculation per modality,
and safe backfill utilities for unpriced historical events.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Optional, Tuple, Dict, Any, List

from sqlalchemy import select, and_, or_, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.ai_usage import AIModelPricing, AIUsageEvent

logger = logging.getLogger(__name__)

ONE_MILLION = Decimal("1000000")


@dataclass
class CostCalculationResult:
    estimated_cost_usd: Optional[Decimal]
    pricing_version: Optional[str]
    pricing_status: str  # "priced", "partial", "unpriced"
    details: Dict[str, Any]


def normalize_model_name(model_name: Optional[str]) -> Optional[str]:
    """
    Strip 'models/' prefix or whitespace if present for consistent pricing lookup.
    E.g. 'models/gemini-3.8-flash' -> 'gemini-3.8-flash'.
    """
    if not model_name:
        return None
    cleaned = str(model_name).strip()
    if cleaned.startswith("models/"):
        return cleaned[len("models/"):]
    return cleaned


async def get_model_pricing(
    session: AsyncSession,
    model: str,
    provider: str = "gemini",
    target_date: Optional[datetime] = None,
) -> Optional[AIModelPricing]:
    """
    Look up active pricing matching provider, model, and effective date window.
    effective_from <= target_date AND (effective_to IS NULL OR effective_to > target_date).
    """
    norm_model = normalize_model_name(model)
    if not norm_model:
        return None

    dt = target_date or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    # First attempt exact normalized model, fallback to raw model if different
    stmt = (
        select(AIModelPricing)
        .where(
            and_(
                AIModelPricing.is_active.is_(True),
                AIModelPricing.provider == provider,
                or_(
                    AIModelPricing.model == norm_model,
                    AIModelPricing.model == model,
                ),
                AIModelPricing.effective_from <= dt,
                or_(
                    AIModelPricing.effective_to.is_(None),
                    AIModelPricing.effective_to > dt,
                ),
            )
        )
        .order_by(AIModelPricing.effective_from.desc())
        .limit(1)
    )

    result = await session.execute(stmt)
    return result.scalars().first()


def calculate_event_cost(
    pricing: Optional[AIModelPricing],
    prompt_token_count: Optional[int] = None,
    candidates_token_count: Optional[int] = None,
    cached_content_token_count: Optional[int] = None,
    thoughts_token_count: Optional[int] = None,
    input_text_tokens: Optional[int] = None,
    input_audio_tokens: Optional[int] = None,
    output_text_tokens: Optional[int] = None,
    output_audio_tokens: Optional[int] = None,
    audio_duration_seconds: Optional[float | Decimal] = None,
) -> CostCalculationResult:
    """
    Calculate estimated cost in USD using non-duplicative modality breakdown.
    
    Modalities rule:
    - If input_text_tokens is not None or input_audio_tokens is not None:
      use explicit modality counts.
    - Fallback: if modality counts are None but prompt_token_count is present:
      treat prompt_token_count as input_text_tokens.
      
    - If output_text_tokens is not None or output_audio_tokens is not None:
      use explicit modality counts.
    - Fallback: if modality counts are None but candidates_token_count is present:
      treat candidates_token_count as output_text_tokens.

    - Cached tokens:
      If cached_content_token_count is present, priced with cached_input_price_per_million.
      Note: Google Gemini reports prompt_token_count INCLUDING cached tokens.
      The net un-cached input tokens = max(0, input_tokens - cached_tokens).
      
    - Thoughts tokens:
      If thoughts_token_count is present and thoughts_price_per_million is set, price separately;
      otherwise Google bundles thought tokens into output tokens.
    """
    if pricing is None:
        return CostCalculationResult(
            estimated_cost_usd=None,
            pricing_version=None,
            pricing_status="unpriced",
            details={"reason": "No pricing found for model and date"},
        )

    # 1. Input tokens determination & caching deduction
    # Google Gemini reports prompt_token_count as the total input token count (including cached tokens).
    # Net regular input = max(0, prompt_tokens - cached_tokens).
    is_partial = False
    details: Dict[str, Any] = {
        "pricing_version": pricing.pricing_version,
        "currency": pricing.currency,
    }

    eff_cached = max(0, cached_content_token_count or 0)
    eff_thoughts = max(0, thoughts_token_count or 0)

    # Check if we have explicit modality breakdown for prompt
    has_input_modalities = (input_text_tokens is not None) or (input_audio_tokens is not None)

    total_cost = Decimal("0.000000")

    if has_input_modalities:
        # Modality breakdown provided
        raw_text = input_text_tokens or 0
        raw_audio = input_audio_tokens or 0
        total_raw_input = raw_text + raw_audio

        # Net non-cached tokens deduction:
        # If rates for text and audio input are identical (like Gemini 3.8 Flash), deduction is uniform.
        # If rates differ (like Gemini Live), deduct cached tokens from text input first (caching in text),
        # ensuring net input is never negative.
        rem_cached = eff_cached
        deduct_text = min(raw_text, rem_cached)
        net_text = raw_text - deduct_text
        rem_cached -= deduct_text

        deduct_audio = min(raw_audio, rem_cached)
        net_audio = raw_audio - deduct_audio

        in_text_rate = Decimal(str(pricing.input_text_price_per_million))
        in_audio_rate = Decimal(str(pricing.input_audio_price_per_million)) if pricing.input_audio_price_per_million is not None else in_text_rate

        cost_in_text = (Decimal(net_text) / ONE_MILLION) * in_text_rate
        cost_in_audio = (Decimal(net_audio) / ONE_MILLION) * in_audio_rate
        total_cost += cost_in_text + cost_in_audio

        details["net_input_text_tokens"] = net_text
        details["net_input_audio_tokens"] = net_audio
        details["cost_input_text_usd"] = float(cost_in_text)
        details["cost_input_audio_usd"] = float(cost_in_audio)
    elif prompt_token_count is not None:
        # Fallback when modality breakdown is not available:
        # Treat net prompt as input_text_tokens
        net_prompt = max(0, prompt_token_count - eff_cached)
        cost_prompt = (Decimal(net_prompt) / ONE_MILLION) * Decimal(str(pricing.input_text_price_per_million))
        total_cost += cost_prompt
        details["net_prompt_tokens"] = net_prompt
        details["cost_input_usd"] = float(cost_prompt)
    else:
        is_partial = True

    # Cached Input cost
    if eff_cached > 0:
        rate_cached = pricing.cached_input_price_per_million if pricing.cached_input_price_per_million is not None else pricing.input_text_price_per_million
        cost_cached = (Decimal(eff_cached) / ONE_MILLION) * Decimal(str(rate_cached))
        total_cost += cost_cached
        details["cached_tokens"] = eff_cached
        details["cost_cached_usd"] = float(cost_cached)

    # 2. Output & Thinking tokens determination
    # In Google Gemini:
    # - candidates_token_count = tokens generated in candidates
    # - thoughts_token_count = tokens used in reasoning/thinking process
    # - total_token_count = prompt + candidates + thoughts
    # Pricing rule: "Output price (including thinking tokens)".
    # Both candidates and thoughts are billed at the OUTPUT rate.
    has_output_modalities = (output_text_tokens is not None) or (output_audio_tokens is not None)

    if has_output_modalities:
        cand_text = output_text_tokens or 0
        cand_audio = output_audio_tokens or 0

        out_text_rate = Decimal(str(pricing.output_text_price_per_million))
        out_audio_rate = Decimal(str(pricing.output_audio_price_per_million)) if pricing.output_audio_price_per_million is not None else out_text_rate

        cost_out_text = (Decimal(cand_text) / ONE_MILLION) * out_text_rate
        cost_out_audio = (Decimal(cand_audio) / ONE_MILLION) * out_audio_rate
        total_cost += cost_out_text + cost_out_audio

        details["output_text_tokens"] = cand_text
        details["output_audio_tokens"] = cand_audio
        details["cost_output_text_usd"] = float(cost_out_text)
        details["cost_output_audio_usd"] = float(cost_out_audio)
    elif candidates_token_count is not None:
        cost_out_cand = (Decimal(candidates_token_count) / ONE_MILLION) * Decimal(str(pricing.output_text_price_per_million))
        total_cost += cost_out_cand
        details["candidates_tokens"] = candidates_token_count
        details["cost_candidates_usd"] = float(cost_out_cand)
    else:
        is_partial = True

    # Thinking tokens cost:
    # Billed at output_text_price_per_million
    if eff_thoughts > 0:
        cost_thoughts = (Decimal(eff_thoughts) / ONE_MILLION) * Decimal(str(pricing.output_text_price_per_million))
        total_cost += cost_thoughts
        details["thoughts_tokens"] = eff_thoughts
        details["cost_thoughts_usd"] = float(cost_thoughts)

    # 3. Audio duration / per-minute pricing (for STT models: Groq Whisper, Mistral STT, Deepgram, etc.)
    audio_min_rate = getattr(pricing, "audio_price_per_minute", None)
    has_token_rates = any(
        (val is not None and Decimal(str(val)) > 0)
        for val in (
            pricing.input_text_price_per_million,
            pricing.input_audio_price_per_million,
            pricing.output_text_price_per_million,
            pricing.output_audio_price_per_million,
        )
    )
    if audio_min_rate is not None and Decimal(str(audio_min_rate)) > 0:
        if audio_duration_seconds is not None and float(audio_duration_seconds) >= 0:
            dur_sec = Decimal(str(audio_duration_seconds))
            cost_audio_min = (dur_sec / Decimal("60")) * Decimal(str(audio_min_rate))
            total_cost += cost_audio_min
            details["audio_duration_seconds"] = float(dur_sec)
            details["audio_price_per_minute"] = float(audio_min_rate)
            details["cost_audio_duration_usd"] = float(cost_audio_min)
            if not has_token_rates:
                # Pure per-minute model does not require token counts to be fully priced
                is_partial = False
        else:
            if not has_token_rates:
                return CostCalculationResult(
                    estimated_cost_usd=None,
                    pricing_version=pricing.pricing_version,
                    pricing_status="unpriced",
                    details={"reason": "Per-minute model missing audio_duration_seconds"},
                )
            is_partial = True

    # Round to 6 decimal places ($0.000001 precision)
    rounded_cost = total_cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    details["total_cost_usd"] = float(rounded_cost)

    status = "partial" if is_partial else "priced"
    return CostCalculationResult(
        estimated_cost_usd=rounded_cost,
        pricing_version=pricing.pricing_version,
        pricing_status=status,
        details=details,
    )


async def backfill_unpriced_events(
    batch_size: int = 500,
    max_events: Optional[int] = None,
    session: Optional[AsyncSession] = None,
) -> Dict[str, Any]:
    """
    Idempotent backfill utility to calculate estimated cost for events where
    pricing_status == 'unpriced' or estimated_cost_usd IS NULL.
    """
    processed = 0
    priced = 0
    unpriced = 0
    partial = 0

    async def _run_batch(db_session: AsyncSession):
        nonlocal processed, priced, unpriced, partial
        
        stmt = (
            select(AIUsageEvent)
            .where(
                or_(
                    AIUsageEvent.pricing_status == "unpriced",
                    AIUsageEvent.estimated_cost_usd.is_(None),
                )
            )
            .order_by(AIUsageEvent.id.asc())
            .limit(batch_size)
        )
        
        res = await db_session.execute(stmt)
        events = res.scalars().all()
        if not events:
            return 0

        # Cache pricing lookups in memory per (provider, model, date)
        pricing_cache: Dict[str, Optional[AIModelPricing]] = {}

        for ev in events:
            model_key = normalize_model_name(ev.model) or "unknown"
            target_dt = ev.created_at or datetime.now(timezone.utc)
            cache_key = f"{ev.provider}:{model_key}:{target_dt.strftime('%Y-%m-%d')}"
            
            if cache_key not in pricing_cache:
                pricing = await get_model_pricing(
                    session=db_session,
                    model=model_key,
                    provider=ev.provider,
                    target_date=target_dt,
                )
                pricing_cache[cache_key] = pricing
            else:
                pricing = pricing_cache[cache_key]

            calc = calculate_event_cost(
                pricing=pricing,
                prompt_token_count=ev.prompt_token_count,
                candidates_token_count=ev.candidates_token_count,
                cached_content_token_count=ev.cached_content_token_count,
                thoughts_token_count=ev.thoughts_token_count,
                input_text_tokens=ev.input_text_tokens,
                input_audio_tokens=ev.input_audio_tokens,
                output_text_tokens=ev.output_text_tokens,
                output_audio_tokens=ev.output_audio_tokens,
                audio_duration_seconds=ev.audio_duration_seconds,
            )

            ev.estimated_cost_usd = calc.estimated_cost_usd
            ev.pricing_version = calc.pricing_version
            ev.pricing_status = calc.pricing_status

            processed += 1
            if calc.pricing_status == "priced":
                priced += 1
            elif calc.pricing_status == "partial":
                partial += 1
            else:
                unpriced += 1

        await db_session.commit()
        return len(events)

    if session is not None:
        while True:
            n = await _run_batch(session)
            if n == 0 or (max_events and processed >= max_events):
                break
    else:
        async with AsyncSessionLocal() as session_local:
            while True:
                n = await _run_batch(session_local)
                if n == 0 or (max_events and processed >= max_events):
                    break

    return {
        "processed": processed,
        "priced": priced,
        "partial": partial,
        "unpriced": unpriced,
    }
