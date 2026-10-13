"""
Azure OpenAI / Gemini service wrapper — delegates calls dynamically to get_ai_provider()
with full support for usage metadata telemetry and backward-compatible returns.
"""
import logging
from typing import Any, Dict, List, Optional, Union

from app.services.ai_provider import AIProvider, AIResponse, get_ai_provider
from app.services.ai_usage_service import AIUsageMetadata

logger = logging.getLogger(__name__)


async def complete_text(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.2,
    response_format: Optional[str] = "json_object",
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    operation: Optional[str] = None,
    call_id: Optional[str] = None,
    mass_analysis_id: Optional[int] = None,
    cycle_id: Optional[int] = None,
    trainer_session_id: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
    return_usage: bool = False,
) -> Union[str, AIResponse]:
    """Delegate text completion to the active AI provider."""
    provider = get_ai_provider(company_id=company_id, service_id=service_id, module=module)
    return await provider.complete_text(
        messages=messages,
        model=model,
        temperature=temperature,
        response_format=response_format,
        company_id=company_id,
        service_id=service_id,
        module=module,
        operation=operation,
        call_id=call_id,
        mass_analysis_id=mass_analysis_id,
        cycle_id=cycle_id,
        trainer_session_id=trainer_session_id,
        audio_duration_seconds=audio_duration_seconds,
        return_usage=return_usage,
    )


async def complete_text_with_usage(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.2,
    response_format: Optional[str] = "json_object",
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    operation: Optional[str] = None,
    call_id: Optional[str] = None,
    mass_analysis_id: Optional[int] = None,
    cycle_id: Optional[int] = None,
    trainer_session_id: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
) -> AIResponse:
    """Delegate text completion requesting full usage metadata."""
    res = await complete_text(
        messages=messages,
        model=model,
        temperature=temperature,
        response_format=response_format,
        company_id=company_id,
        service_id=service_id,
        module=module,
        operation=operation,
        call_id=call_id,
        mass_analysis_id=mass_analysis_id,
        cycle_id=cycle_id,
        trainer_session_id=trainer_session_id,
        audio_duration_seconds=audio_duration_seconds,
        return_usage=True,
    )
    assert isinstance(res, AIResponse)
    return res


async def transcribe_audio(
    audio_bytes: bytes,
    filename: str = "audio.wav",
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    operation: Optional[str] = None,
    call_id: Optional[str] = None,
    mass_analysis_id: Optional[int] = None,
    cycle_id: Optional[int] = None,
    trainer_session_id: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
    return_usage: bool = False,
) -> Union[Dict[str, str], AIResponse]:
    """Delegate transcription to the active AI provider."""
    provider = get_ai_provider(company_id=company_id, service_id=service_id, module=module)
    return await provider.transcribe_audio(
        audio_bytes=audio_bytes,
        filename=filename,
        company_id=company_id,
        service_id=service_id,
        module=module,
        operation=operation,
        call_id=call_id,
        mass_analysis_id=mass_analysis_id,
        cycle_id=cycle_id,
        trainer_session_id=trainer_session_id,
        audio_duration_seconds=audio_duration_seconds,
        return_usage=return_usage,
    )


async def transcribe_audio_with_usage(
    audio_bytes: bytes,
    filename: str = "audio.wav",
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    operation: Optional[str] = None,
    call_id: Optional[str] = None,
    mass_analysis_id: Optional[int] = None,
    cycle_id: Optional[int] = None,
    trainer_session_id: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
) -> AIResponse:
    """Delegate transcription requesting full usage metadata."""
    res = await transcribe_audio(
        audio_bytes=audio_bytes,
        filename=filename,
        company_id=company_id,
        service_id=service_id,
        module=module,
        operation=operation,
        call_id=call_id,
        mass_analysis_id=mass_analysis_id,
        cycle_id=cycle_id,
        trainer_session_id=trainer_session_id,
        audio_duration_seconds=audio_duration_seconds,
        return_usage=True,
    )
    assert isinstance(res, AIResponse)
    return res


async def analyze_audio_bytes(
    audio_bytes: bytes,
    prompt_text: str,
    audio_format: str = "mp3",
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    operation: Optional[str] = None,
    call_id: Optional[str] = None,
    mass_analysis_id: Optional[int] = None,
    cycle_id: Optional[int] = None,
    trainer_session_id: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
    return_usage: bool = False,
) -> Union[str, AIResponse]:
    """Delegate multimodal audio bytes analysis to the active AI provider."""
    provider = get_ai_provider(company_id=company_id, service_id=service_id, module=module)
    return await provider.analyze_audio_bytes(
        audio_bytes=audio_bytes,
        prompt_text=prompt_text,
        audio_format=audio_format,
        company_id=company_id,
        service_id=service_id,
        module=module,
        operation=operation,
        call_id=call_id,
        mass_analysis_id=mass_analysis_id,
        cycle_id=cycle_id,
        trainer_session_id=trainer_session_id,
        audio_duration_seconds=audio_duration_seconds,
        return_usage=return_usage,
    )


async def analyze_audio_bytes_with_usage(
    audio_bytes: bytes,
    prompt_text: str,
    audio_format: str = "mp3",
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    operation: Optional[str] = None,
    call_id: Optional[str] = None,
    mass_analysis_id: Optional[int] = None,
    cycle_id: Optional[int] = None,
    trainer_session_id: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
) -> AIResponse:
    """Delegate multimodal audio bytes analysis requesting full usage metadata."""
    res = await analyze_audio_bytes(
        audio_bytes=audio_bytes,
        prompt_text=prompt_text,
        audio_format=audio_format,
        company_id=company_id,
        service_id=service_id,
        module=module,
        operation=operation,
        call_id=call_id,
        mass_analysis_id=mass_analysis_id,
        cycle_id=cycle_id,
        trainer_session_id=trainer_session_id,
        audio_duration_seconds=audio_duration_seconds,
        return_usage=True,
    )
    assert isinstance(res, AIResponse)
    return res


async def analyze_audio_url(
    audio_url: str,
    prompt_text: str,
    model: Optional[str] = None,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
    operation: Optional[str] = None,
    call_id: Optional[str] = None,
    mass_analysis_id: Optional[int] = None,
    cycle_id: Optional[int] = None,
    trainer_session_id: Optional[int] = None,
    audio_duration_seconds: Optional[float] = None,
    return_usage: bool = False,
) -> Union[str, AIResponse]:
    """Delegate multimodal audio URL analysis to the active AI provider (downloads audio for Gemini)."""
    provider = get_ai_provider(company_id=company_id, service_id=service_id, module=module)
    if hasattr(provider, "analyze_audio_url"):
        return await provider.analyze_audio_url(audio_url, prompt_text, model)
    else:
        # Fallback: download audio and use analyze_audio_bytes
        from app.services.twilio_service import TwilioService
        twilio_service = TwilioService()
        audio_bytes = await twilio_service.download_audio(audio_url)
        audio_format = "mp3"
        if audio_url.lower().endswith(".wav"):
            audio_format = "wav"
        return await provider.analyze_audio_bytes(
            audio_bytes=audio_bytes,
            prompt_text=prompt_text,
            audio_format=audio_format,
            company_id=company_id,
            service_id=service_id,
            module=module,
            operation=operation,
            call_id=call_id,
            mass_analysis_id=mass_analysis_id,
            cycle_id=cycle_id,
            trainer_session_id=trainer_session_id,
            audio_duration_seconds=audio_duration_seconds,
            return_usage=return_usage,
        )
