"""
Azure OpenAI service — wraps the official OpenAI SDK using Azure configuration.
The system is exclusively bound to Azure OpenAI. Direct OpenAI is not supported.
"""
import base64
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


import time


def _log_completion_metrics(response, deployment: str, operation: str, duration_sec: float):
    """Utility helper to log token usage and execution time taken for Azure OpenAI calls."""
    try:
        if hasattr(response, "usage") and response.usage:
            prompt_tokens = response.usage.prompt_tokens
            completion_tokens = response.usage.completion_tokens
            total_tokens = response.usage.total_tokens
            logger.info(
                f"\n==================================================\n"
                f"AZURE OPENAI PERFORMANCE DIAGNOSTIC ({operation.upper()}):\n"
                f"==================================================\n"
                f"- Deployment: {deployment}\n"
                f"- Duration: {duration_sec:.2f} s\n"
                f"- Prompt (Input) Tokens: {prompt_tokens}\n"
                f"- Completion (Output) Tokens: {completion_tokens}\n"
                f"- Total Tokens: {total_tokens}\n"
                f"=================================================="
            )
        else:
            logger.info(
                f"\n==================================================\n"
                f"AZURE OPENAI PERFORMANCE DIAGNOSTIC ({operation.upper()}):\n"
                f"==================================================\n"
                f"- Deployment: {deployment}\n"
                f"- Duration: {duration_sec:.2f} s\n"
                f"- Token usage info not available\n"
                f"=================================================="
            )
    except Exception as usage_ex:
        logger.warning(f"Failed to capture OpenAI token usage details for {operation}: {usage_ex}")


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

    t_start = time.perf_counter()
    response = await client.chat.completions.create(**kwargs)
    t_end = time.perf_counter()
    
    _log_completion_metrics(response, deployment, "complete_text", t_end - t_start)

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

    t_start = time.perf_counter()
    response = await client.audio.transcriptions.create(
        model=deployment,
        file=audio_file,
    )
    t_end = time.perf_counter()
    
    logger.info(
        f"\n==================================================\n"
        f"AZURE OPENAI WHISPER TRANSCRIPTION METRICS:\n"
        f"==================================================\n"
        f"- Deployment: {deployment}\n"
        f"- Duration: {t_end - t_start:.2f} s\n"
        f"=================================================="
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

    t_start = time.perf_counter()
    response = await client.chat.completions.create(
        model=deployment,
        messages=messages,
        response_format={"type": "json_object"},
    )
    t_end = time.perf_counter()
    
    _log_completion_metrics(response, deployment, "analyze_audio_url", t_end - t_start)
    return response.choices[0].message.content or ""


async def analyze_audio_bytes(audio_bytes: bytes, prompt_text: str, audio_format: str = "mp3") -> str:
    """
    Analyze audio from raw bytes using Azure OpenAI multimodal audio model.
    Encodes audio to base64 and sends it in the `input_audio` standard format.
    Returns raw JSON string from the model.
    """
    client = _get_azure_client("audio")
    deployment = settings.azure_openai_audio_deployment

    if not deployment:
        raise ValueError("Azure OpenAI audio deployment is not configured. Set AZURE_OPENAI_AUDIO_DEPLOYMENT.")

    encoded_audio = base64.b64encode(audio_bytes).decode("utf-8")

    # Strict JSON instruction — explicitly forbids unescaped quotes/newlines inside strings
    system_prompt = (
        "Eres un experto analizador de llamadas. "
        "Devuelve EXCLUSIVAMENTE un objeto JSON válido y bien formado, sin markdown ni texto adicional. "
        "REGLAS CRÍTICAS para el JSON: "
        "1) Todos los caracteres especiales dentro de strings (comillas, saltos de línea, tabulaciones) DEBEN estar escapados correctamente (e.g. \\\" para comillas, \\n para saltos de línea). "
        "2) Nunca insertes saltos de línea literales dentro de valores de string JSON. "
        "3) El campo 'transcription' debe ser una cadena de texto plana con los turnos de conversación separados por \\n. "
        "4) No añadas ningún texto fuera del objeto JSON."
    )

    messages = [
        {
            "role": "system",
            "content": system_prompt
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": encoded_audio,
                        "format": audio_format
                    }
                }
            ],
        }
    ]

    t_start = time.perf_counter()
    # Try with response_format=json_object first (enforces valid JSON at model level)
    # Fall back to plain call if the deployment does not support it
    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception:
        response = await client.chat.completions.create(
            model=deployment,
            messages=messages,
        )
    t_end = time.perf_counter()
    
    _log_completion_metrics(response, deployment, "analyze_audio_bytes", t_end - t_start)
    return response.choices[0].message.content or ""


