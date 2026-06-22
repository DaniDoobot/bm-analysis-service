import abc
import base64
import logging
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class AIProvider(abc.ABC):
    @abc.abstractmethod
    async def complete_text(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        response_format: str | None = "json_object",
    ) -> str:
        """Execute text completion and return raw response string."""
        pass

    @abc.abstractmethod
    async def analyze_audio_bytes(
        self,
        audio_bytes: bytes,
        prompt_text: str,
        audio_format: str = "mp3"
    ) -> str:
        """Analyze raw audio bytes using multimodal input and return raw JSON/response string."""
        pass

    @abc.abstractmethod
    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav"
    ) -> dict[str, str]:
        """Transcribe audio bytes and return {"text": "...", "model": "...", "provider": "..."}."""
        pass


class GeminiProvider(AIProvider):
    def __init__(self):
        # Configure the Google Generative AI SDK
        try:
            import google.generativeai as genai
            if not settings.gemini_api_key:
                raise ValueError("GEMINI_API_KEY is not configured.")
            genai.configure(api_key=settings.gemini_api_key)
            self._genai = genai
        except ImportError:
            raise RuntimeError("google-generativeai package not installed")

    async def complete_text(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        response_format: str | None = "json_object",
    ) -> str:
        model_name = model or settings.gemini_report_model
        
        # System instructions and contents mapping
        system_instruction = None
        gemini_contents = []
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if not content:
                continue
            if role == "system":
                if system_instruction:
                    system_instruction += "\n" + content
                else:
                    system_instruction = content
            elif role == "user":
                gemini_contents.append({"role": "user", "parts": [content]})
            elif role in ["assistant", "model"]:
                gemini_contents.append({"role": "model", "parts": [content]})
        
        config = {
            "temperature": temperature,
        }
        if response_format == "json_object":
            config["response_mime_type"] = "application/json"
            
        if settings.gemini_max_output_tokens:
            config["max_output_tokens"] = settings.gemini_max_output_tokens

        logger.info("Calling Gemini complete_text: model=%s, temp=%.2f, json=%s", model_name, temperature, response_format)
        t_start = time.perf_counter()
        
        generative_model = self._genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_instruction
        )
        
        response = await generative_model.generate_content_async(
            contents=gemini_contents,
            generation_config=config
        )
        duration = time.perf_counter() - t_start
        logger.info("Gemini complete_text completed in %.2f s", duration)
        
        return response.text or ""

    async def analyze_audio_bytes(
        self,
        audio_bytes: bytes,
        prompt_text: str,
        audio_format: str = "mp3"
    ) -> str:
        model_name = settings.gemini_analysis_model
        
        system_prompt = (
            "Eres un experto analizador de llamadas. "
            "Devuelve EXCLUSIVAMENTE un objeto JSON válido y bien formado, sin markdown ni texto adicional. "
            "REGLAS CRÍTICAS para el JSON: "
            "1) Todos los caracteres especiales dentro de strings (comillas, saltos de línea, tabulaciones) DEBEN estar escapados correctamente (e.g. \\\" para comillas, \\n para saltos de línea). "
            "2) Nunca insertes saltos de línea literales dentro de valores de string JSON. "
            "3) El campo 'transcription' debe ser una cadena de texto plana con los turnos de conversación separados por \\n. "
            "4) No añadas ningún texto fuera del objeto JSON."
        )

        mime_type = "audio/mp3" if audio_format.lower() in ["mp3", "mpeg"] else f"audio/{audio_format.lower()}"
        
        audio_part = {
            "mime_type": mime_type,
            "data": audio_bytes
        }
        
        contents = [
            prompt_text,
            audio_part
        ]
        
        config = {
            "temperature": settings.gemini_temperature,
            "response_mime_type": "application/json"
        }
        if settings.gemini_max_output_tokens:
            config["max_output_tokens"] = settings.gemini_max_output_tokens

        logger.info("Calling Gemini analyze_audio_bytes: model=%s, format=%s, size=%.2f MB", model_name, audio_format, len(audio_bytes)/(1024*1024))
        t_start = time.perf_counter()
        
        generative_model = self._genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_prompt
        )
        
        response = await generative_model.generate_content_async(
            contents=contents,
            generation_config=config
        )
        
        duration = time.perf_counter() - t_start
        logger.info("Gemini analyze_audio_bytes completed in %.2f s", duration)
        return response.text or ""

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav"
    ) -> dict[str, str]:
        model_name = settings.gemini_analysis_model
        
        audio_format = "mp3"
        if filename.lower().endswith(".wav"):
            audio_format = "wav"
            
        mime_type = "audio/mp3" if audio_format.lower() in ["mp3", "mpeg"] else f"audio/{audio_format.lower()}"
        
        audio_part = {
            "mime_type": mime_type,
            "data": audio_bytes
        }
        
        system_prompt = (
            "Eres un transcriptor experto. Tu única tarea es transcribir el audio completo de forma exacta, "
            "palabra por palabra. Si hay varios hablantes, sepáralos en líneas e indícalo si es claro. "
            "Devuelve estrictamente el texto transcribido. No agregues introducciones, conclusiones, explicaciones ni etiquetas markdown."
        )
        
        contents = [
            "Transcribe esta llamada telefónica de forma exacta.",
            audio_part
        ]
        
        logger.info("Calling Gemini transcribe_audio: model=%s, size=%.2f MB", model_name, len(audio_bytes)/(1024*1024))
        t_start = time.perf_counter()
        
        generative_model = self._genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_prompt
        )
        
        response = await generative_model.generate_content_async(
            contents=contents
        )
        
        duration = time.perf_counter() - t_start
        logger.info("Gemini transcribe_audio completed in %.2f s", duration)
        
        return {
            "text": response.text or "",
            "model": model_name,
            "provider": "gemini"
        }


class AzureOpenAIProvider(AIProvider):
    async def complete_text(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        response_format: str | None = "json_object",
    ) -> str:
        from openai import AsyncAzureOpenAI
        endpoint = settings.azure_openai_text_endpoint
        api_key = settings.azure_openai_text_api_key
        deployment = model or settings.azure_openai_text_deployment
        
        if not endpoint or not api_key or not deployment:
            raise ValueError("Azure OpenAI text configuration missing.")
            
        client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=settings.azure_openai_api_version,
        )
        
        kwargs: dict[str, Any] = {
            "model": deployment,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
            
        logger.info("Calling Legacy Azure complete_text (deployment: %s)", deployment)
        response = await client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    async def analyze_audio_bytes(
        self,
        audio_bytes: bytes,
        prompt_text: str,
        audio_format: str = "mp3"
    ) -> str:
        from openai import AsyncAzureOpenAI
        endpoint = settings.azure_openai_audio_endpoint
        api_key = settings.azure_openai_audio_api_key
        deployment = settings.azure_openai_audio_deployment
        
        if not endpoint or not api_key or not deployment:
            raise ValueError("Azure OpenAI audio configuration missing.")
            
        client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=settings.azure_openai_api_version,
        )
        
        encoded_audio = base64.b64encode(audio_bytes).decode("utf-8")
        
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
            {"role": "system", "content": system_prompt},
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
        
        logger.info("Calling Legacy Azure analyze_audio_bytes (deployment: %s)", deployment)
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
        return response.choices[0].message.content or ""

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        filename: str = "audio.wav"
    ) -> dict[str, str]:
        from openai import AsyncAzureOpenAI
        import io
        endpoint = settings.azure_openai_transcription_endpoint
        api_key = settings.azure_openai_transcription_api_key
        deployment = settings.azure_openai_transcription_deployment
        
        if not endpoint or not api_key or not deployment:
            raise ValueError("Azure OpenAI transcription configuration missing.")
            
        client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=settings.azure_openai_api_version,
        )
        
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = filename
        
        logger.info("Calling Legacy Azure Whisper transcription (deployment: %s)", deployment)
        response = await client.audio.transcriptions.create(
            model=deployment,
            file=audio_file,
        )
        return {
            "text": response.text,
            "model": deployment,
            "provider": "azure_openai"
        }


def get_ai_provider() -> AIProvider:
    """Resolve and return the active AI provider based on settings."""
    provider = (settings.ai_provider or "gemini").lower()
    if provider == "gemini":
        return GeminiProvider()
    elif provider in ["azure", "azure_openai", "openai"]:
        return AzureOpenAIProvider()
    else:
        raise ValueError(f"Unknown AI provider: {provider}")
