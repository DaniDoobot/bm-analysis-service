"""
Azure OpenAI service — wraps the official OpenAI SDK using Azure configuration.
The system is exclusively bound to Azure OpenAI. Direct OpenAI is not supported.
"""
import io
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _get_azure_client(provider_type: str):
    """Return a configured Azure OpenAI client for the specified type (text, audio, transcription)."""
    try:
        from openai import AsyncAzureOpenAI
    except ImportError:
        raise RuntimeError("openai package not installed")

    if provider_type == "text":
        endpoint = settings.azure_openai_text_endpoint
        api_key = settings.azure_openai_text_api_key
    elif provider_type == "audio":
        endpoint = settings.azure_openai_audio_endpoint
        api_key = settings.azure_openai_audio_api_key
    elif provider_type == "transcription":
        endpoint = settings.azure_openai_transcription_endpoint
        api_key = settings.azure_openai_transcription_api_key
    else:
        raise ValueError(f"Unknown Azure OpenAI provider type: {provider_type}")

    if not endpoint:
        raise ValueError(f"Azure OpenAI {provider_type} endpoint is not configured. Set AZURE_OPENAI_{provider_type.upper()}_ENDPOINT.")
    if not api_key:
        raise ValueError(f"Azure OpenAI {provider_type} API key is not configured. Set AZURE_OPENAI_{provider_type.upper()}_API_KEY.")

    return AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=settings.azure_openai_api_version,
    )


async def complete_text(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float = 0.2,
    response_format: str | None = "json_object",
) -> str:
    """Call the chat completions endpoint using the text deployment and return raw text."""
    client = _get_azure_client("text")

    deployment = model or settings.azure_openai_text_deployment
    if not deployment:
        raise ValueError("Azure OpenAI text deployment is not configured. Set AZURE_OPENAI_TEXT_DEPLOYMENT.")

    kwargs: dict[str, Any] = {
        "model": deployment,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format == "json_object":
        kwargs["response_format"] = {"type": "json_object"}

    response = await client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.wav") -> dict[str, str]:
    """
    Transcribe audio using Whisper via Azure OpenAI.
    Returns {"text": "...", "model": "...", "provider": "azure_openai"}.
    """
    client = _get_azure_client("transcription")
    deployment = settings.azure_openai_transcription_deployment
    
    if not deployment:
        raise ValueError("Azure OpenAI transcription deployment is not configured. Set AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT.")

    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = filename

    response = await client.audio.transcriptions.create(
        model=deployment,
        file=audio_file,
    )
    return {
        "text": response.text,
        "model": deployment,
        "provider": "azure_openai",
    }


async def analyze_audio_url(audio_url: str, prompt_text: str, model: str | None = None) -> str:
    """
    Analyze audio from a URL using a multimodal audio model via Azure.
    Returns raw JSON string from the model.
    """
    client = _get_azure_client("audio")
    deployment = model or settings.azure_openai_audio_deployment
    
    if not deployment:
        raise ValueError("Azure OpenAI audio deployment is not configured. Set AZURE_OPENAI_AUDIO_DEPLOYMENT.")

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "audio_url", "audio_url": {"url": audio_url}},
            ],
        }
    ]

    response = await client.chat.completions.create(
        model=deployment,
        messages=messages,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""

