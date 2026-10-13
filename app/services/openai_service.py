"""
Azure OpenAI / Gemini service wrapper — delegates calls dynamically to get_ai_provider()
"""
import logging
from app.services.ai_provider import get_ai_provider

logger = logging.getLogger(__name__)

async def complete_text(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float = 0.2,
    response_format: str | None = "json_object",
    company_id: int | None = None,
    service_id: int | None = None,
    module: str | None = None,
) -> str:
    """Delegate text completion to the active AI provider."""
    provider = get_ai_provider(company_id=company_id, service_id=service_id, module=module)
    return await provider.complete_text(
        messages=messages,
        model=model,
        temperature=temperature,
        response_format=response_format,
    )

async def transcribe_audio(
    audio_bytes: bytes,
    filename: str = "audio.wav",
    company_id: int | None = None,
    service_id: int | None = None,
    module: str | None = None,
) -> dict[str, str]:
    """Delegate transcription to the active AI provider."""
    provider = get_ai_provider(company_id=company_id, service_id=service_id, module=module)
    return await provider.transcribe_audio(audio_bytes=audio_bytes, filename=filename)

async def analyze_audio_bytes(
    audio_bytes: bytes,
    prompt_text: str,
    audio_format: str = "mp3",
    company_id: int | None = None,
    service_id: int | None = None,
    module: str | None = None,
) -> str:
    """Delegate multimodal audio bytes analysis to the active AI provider."""
    provider = get_ai_provider(company_id=company_id, service_id=service_id, module=module)
    return await provider.analyze_audio_bytes(
        audio_bytes=audio_bytes,
        prompt_text=prompt_text,
        audio_format=audio_format,
    )

async def analyze_audio_url(
    audio_url: str,
    prompt_text: str,
    model: str | None = None,
    company_id: int | None = None,
    service_id: int | None = None,
    module: str | None = None,
) -> str:
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
        )



