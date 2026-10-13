"""
Centralized AI usage service for parsing, normalizing, and non-blocking persistence
of Gemini token usage metadata, latency, and operational telemetry.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from app.db import AsyncSessionLocal
from app.models.ai_usage import AIUsageEvent

logger = logging.getLogger(__name__)


@dataclass
class AIUsageMetadata:
    """Normalized usage telemetry data."""
    provider: str = "gemini"
    model: Optional[str] = None
    credential_alias: Optional[str] = None

    prompt_token_count: Optional[int] = None
    candidates_token_count: Optional[int] = None
    total_token_count: Optional[int] = None
    cached_content_token_count: Optional[int] = None
    thoughts_token_count: Optional[int] = None

    input_text_tokens: Optional[int] = None
    input_audio_tokens: Optional[int] = None
    output_text_tokens: Optional[int] = None
    output_audio_tokens: Optional[int] = None

    response_id: Optional[str] = None
    execution_latency_ms: Optional[int] = None
    audio_duration_seconds: Optional[float] = None

    status: str = "success"
    attempt_number: int = 1


def _extract_val(obj: Any, *keys: str) -> Any:
    """Helper to extract a value from either an object attribute or dict key."""
    if obj is None:
        return None
    for k in keys:
        if isinstance(obj, dict):
            if k in obj and obj[k] is not None:
                return obj[k]
        else:
            val = getattr(obj, k, None)
            if val is not None:
                return val
    return None


def parse_usage_metadata(
    raw_usage_metadata: Any,
    model: Optional[str] = None,
    credential_alias: Optional[str] = None,
    response_id: Optional[str] = None,
    execution_latency_ms: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
    status: str = "success",
    attempt_number: int = 1,
) -> AIUsageMetadata:
    """
    Parse and normalize raw usage metadata from Google GenAI SDK or WebSocket JSON.
    Gracefully handles None, empty dictionaries, missing fields, and custom modalities.
    """
    if raw_usage_metadata is None:
        return AIUsageMetadata(
            provider="gemini",
            model=model,
            credential_alias=credential_alias,
            response_id=response_id,
            execution_latency_ms=execution_latency_ms,
            audio_duration_seconds=audio_duration_seconds,
            status=status,
            attempt_number=attempt_number,
        )

    prompt_token_count = _extract_val(raw_usage_metadata, "prompt_token_count", "promptTokenCount")
    candidates_token_count = _extract_val(raw_usage_metadata, "candidates_token_count", "candidatesTokenCount")
    total_token_count = _extract_val(raw_usage_metadata, "total_token_count", "totalTokenCount")
    cached_content_token_count = _extract_val(raw_usage_metadata, "cached_content_token_count", "cachedContentTokenCount")
    thoughts_token_count = _extract_val(raw_usage_metadata, "thoughts_token_count", "thoughtsTokenCount")

    # Prompt modalities
    raw_prompt_details = _extract_val(raw_usage_metadata, "prompt_tokens_details", "promptTokensDetails")
    input_text_tokens = None
    input_audio_tokens = None

    if raw_prompt_details is not None:
        input_text_tokens = 0
        input_audio_tokens = 0
        if isinstance(raw_prompt_details, (list, tuple)):
            for item in raw_prompt_details:
                modality = str(_extract_val(item, "modality") or "").upper()
                count = _extract_val(item, "token_count", "tokenCount") or 0
                if "TEXT" in modality:
                    input_text_tokens += count
                elif "AUDIO" in modality:
                    input_audio_tokens += count

    # Candidates modalities
    raw_cand_details = _extract_val(raw_usage_metadata, "candidates_tokens_details", "candidatesTokensDetails")
    output_text_tokens = None
    output_audio_tokens = None

    if raw_cand_details is not None:
        output_text_tokens = 0
        output_audio_tokens = 0
        if isinstance(raw_cand_details, (list, tuple)):
            for item in raw_cand_details:
                modality = str(_extract_val(item, "modality") or "").upper()
                count = _extract_val(item, "token_count", "tokenCount") or 0
                if "TEXT" in modality:
                    output_text_tokens += count
                elif "AUDIO" in modality:
                    output_audio_tokens += count

    return AIUsageMetadata(
        provider="gemini",
        model=model,
        credential_alias=credential_alias,
        prompt_token_count=prompt_token_count,
        candidates_token_count=candidates_token_count,
        total_token_count=total_token_count,
        cached_content_token_count=cached_content_token_count,
        thoughts_token_count=thoughts_token_count,
        input_text_tokens=input_text_tokens,
        input_audio_tokens=input_audio_tokens,
        output_text_tokens=output_text_tokens,
        output_audio_tokens=output_audio_tokens,
        response_id=response_id,
        execution_latency_ms=execution_latency_ms,
        audio_duration_seconds=audio_duration_seconds,
        status=status,
        attempt_number=attempt_number,
    )


async def record_ai_usage_event(
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    operation: Optional[str] = None,
    provider: str = "gemini",
    model: Optional[str] = None,
    credential_alias: Optional[str] = None,
    usage: Optional[AIUsageMetadata] = None,
    request_id: Optional[str] = None,
    call_id: Optional[str] = None,
    mass_analysis_id: Optional[int] = None,
    cycle_id: Optional[int] = None,
    trainer_session_id: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
    execution_latency_ms: Optional[int] = None,
    status: str = "success",
    attempt_number: int = 1,
) -> None:
    """
    Persist an AI usage event into bm_ai_usage_events non-blockingly.
    If database insertion fails, logs an error without raising or disrupting the main flow.
    """
    try:
        # Extract fields from usage if provided
        u_model = model or (usage.model if usage else None)
        u_alias = credential_alias or (usage.credential_alias if usage else None)
        u_req_id = request_id or (usage.response_id if usage else None)
        u_latency = execution_latency_ms if execution_latency_ms is not None else (usage.execution_latency_ms if usage else None)
        u_dur = audio_duration_seconds if audio_duration_seconds is not None else (usage.audio_duration_seconds if usage else None)
        u_status = status or (usage.status if usage else "success")
        u_attempt = attempt_number or (usage.attempt_number if usage else 1)

        dur_decimal = Decimal(str(round(u_dur, 2))) if u_dur is not None else None

        event = AIUsageEvent(
            company_id=company_id,
            service_id=service_id,
            module=module,
            operation=operation,
            provider=provider,
            model=u_model,
            credential_alias=u_alias,
            request_id=u_req_id,
            call_id=call_id,
            mass_analysis_id=mass_analysis_id,
            cycle_id=cycle_id,
            trainer_session_id=trainer_session_id,
            prompt_token_count=usage.prompt_token_count if usage else None,
            candidates_token_count=usage.candidates_token_count if usage else None,
            total_token_count=usage.total_token_count if usage else None,
            cached_content_token_count=usage.cached_content_token_count if usage else None,
            thoughts_token_count=usage.thoughts_token_count if usage else None,
            input_text_tokens=usage.input_text_tokens if usage else None,
            input_audio_tokens=usage.input_audio_tokens if usage else None,
            output_text_tokens=usage.output_text_tokens if usage else None,
            output_audio_tokens=usage.output_audio_tokens if usage else None,
            audio_duration_seconds=dur_decimal,
            execution_latency_ms=u_latency,
            status=u_status,
            attempt_number=u_attempt,
        )

        async with AsyncSessionLocal() as session:
            session.add(event)
            await session.commit()

        logger.debug(
            "AI usage event recorded: module=%s operation=%s model=%s alias=%s total_tokens=%s latency_ms=%s",
            module, operation, u_model, u_alias,
            usage.total_token_count if usage else None,
            u_latency,
        )

    except Exception as exc:
        logger.error(
            "Failed to record AI usage event [module=%s, operation=%s, model=%s]: %s",
            module, operation, model, exc,
            exc_info=True,
        )


class LiveSessionUsageTracker:
    """
    Cumulative usage tracker for Gemini Live WebSocket connections (BidiGenerateContent).
    Maintains the authoritative session-level totals and writes a single consolidated event at disconnect.
    """
    def __init__(
        self,
        company_id: Optional[int] = None,
        service_id: Optional[int] = None,
        module: Optional[str] = "live_session",
        operation: Optional[str] = "live_stream",
        model: Optional[str] = None,
        credential_alias: Optional[str] = None,
        trainer_session_id: Optional[int] = None,
        call_id: Optional[str] = None,
    ):
        self.company_id = company_id
        self.service_id = service_id
        self.module = module
        self.operation = operation
        self.model = model
        self.credential_alias = credential_alias
        self.trainer_session_id = trainer_session_id
        self.call_id = call_id
        self.start_time = time.perf_counter()

        self.prompt_token_count: Optional[int] = None
        self.candidates_token_count: Optional[int] = None
        self.total_token_count: Optional[int] = None
        self.cached_content_token_count: Optional[int] = None
        self.thoughts_token_count: Optional[int] = None

        self.input_text_tokens: Optional[int] = None
        self.input_audio_tokens: Optional[int] = None
        self.output_text_tokens: Optional[int] = None
        self.output_audio_tokens: Optional[int] = None

        self.has_usage = False

    def update(self, raw_usage_metadata: Any) -> None:
        """
        Update tracker with latest cumulative usage metadata reported by the Live server.
        Uses max/latest monotonic updates to prevent undercounting or duplications.
        """
        if not raw_usage_metadata:
            return

        parsed = parse_usage_metadata(raw_usage_metadata)
        self.has_usage = True

        if parsed.prompt_token_count is not None:
            self.prompt_token_count = max(self.prompt_token_count or 0, parsed.prompt_token_count)
        if parsed.candidates_token_count is not None:
            self.candidates_token_count = max(self.candidates_token_count or 0, parsed.candidates_token_count)
        if parsed.total_token_count is not None:
            self.total_token_count = max(self.total_token_count or 0, parsed.total_token_count)
        if parsed.cached_content_token_count is not None:
            self.cached_content_token_count = max(self.cached_content_token_count or 0, parsed.cached_content_token_count)
        if parsed.thoughts_token_count is not None:
            self.thoughts_token_count = max(self.thoughts_token_count or 0, parsed.thoughts_token_count)

        if parsed.input_text_tokens is not None:
            self.input_text_tokens = max(self.input_text_tokens or 0, parsed.input_text_tokens)
        if parsed.input_audio_tokens is not None:
            self.input_audio_tokens = max(self.input_audio_tokens or 0, parsed.input_audio_tokens)
        if parsed.output_text_tokens is not None:
            self.output_text_tokens = max(self.output_text_tokens or 0, parsed.output_text_tokens)
        if parsed.output_audio_tokens is not None:
            self.output_audio_tokens = max(self.output_audio_tokens or 0, parsed.output_audio_tokens)

    async def finalize(self, audio_duration_seconds: Optional[float] = None) -> None:
        """Finalize and persist the live session usage."""
        latency_ms = int((time.perf_counter() - self.start_time) * 1000)
        usage = None
        if self.has_usage:
            usage = AIUsageMetadata(
                provider="gemini",
                model=self.model,
                credential_alias=self.credential_alias,
                prompt_token_count=self.prompt_token_count,
                candidates_token_count=self.candidates_token_count,
                total_token_count=self.total_token_count,
                cached_content_token_count=self.cached_content_token_count,
                thoughts_token_count=self.thoughts_token_count,
                input_text_tokens=self.input_text_tokens,
                input_audio_tokens=self.input_audio_tokens,
                output_text_tokens=self.output_text_tokens,
                output_audio_tokens=self.output_audio_tokens,
                execution_latency_ms=latency_ms,
                audio_duration_seconds=audio_duration_seconds,
                status="success",
            )

        await record_ai_usage_event(
            company_id=self.company_id,
            service_id=self.service_id,
            module=self.module,
            operation=self.operation,
            provider="gemini",
            model=self.model,
            credential_alias=self.credential_alias,
            usage=usage,
            call_id=self.call_id,
            trainer_session_id=self.trainer_session_id,
            audio_duration_seconds=audio_duration_seconds,
            execution_latency_ms=latency_ms,
            status="success" if self.has_usage else "completed_without_usage",
        )
