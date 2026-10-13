import abc
import asyncio
import base64
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from app.config import get_settings
from app.services.ai_usage_service import (
    AIUsageMetadata,
    parse_usage_metadata,
    record_ai_usage_event,
)
from app.utils.error_categorization import is_transient_llm_error

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class AIResponse:
    """Standardized response from AI providers containing text output and usage telemetry."""
    text: str
    usage: Optional[AIUsageMetadata] = None
    response_id: Optional[str] = None
    model: Optional[str] = None
    provider: str = "gemini"
    credential_alias: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

    def __getitem__(self, item: str) -> Any:
        if self.data and item in self.data:
            return self.data[item]
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        if self.data and item in self.data:
            return self.data[item]
        if hasattr(self, item):
            return getattr(self, item)
        return default

    def __str__(self) -> str:
        return self.text


class AIProvider(abc.ABC):
    @abc.abstractmethod
    async def complete_text(
        self,
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
        """Execute text completion and return raw response string or AIResponse."""
        pass

    @abc.abstractmethod
    async def analyze_audio_bytes(
        self,
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
        """Analyze raw audio bytes using multimodal input and return raw JSON/response string or AIResponse."""
        pass

    @abc.abstractmethod
    async def transcribe_audio(
        self,
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
        """Transcribe audio bytes and return {'text': '...', 'model': '...', 'provider': '...'} or AIResponse."""
        pass

    async def complete_text_with_usage(self, *args, **kwargs) -> AIResponse:
        kwargs["return_usage"] = True
        return await self.complete_text(*args, **kwargs)

    async def analyze_audio_bytes_with_usage(self, *args, **kwargs) -> AIResponse:
        kwargs["return_usage"] = True
        return await self.analyze_audio_bytes(*args, **kwargs)

    async def transcribe_audio_with_usage(self, *args, **kwargs) -> AIResponse:
        kwargs["return_usage"] = True
        return await self.transcribe_audio(*args, **kwargs)


class GeminiProvider(AIProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        credential_alias: str = "global_fallback",
        company_id: Optional[int] = None,
        service_id: Optional[int] = None,
        module: Optional[str] = None,
    ):
        try:
            from google import genai
            from google.genai import types
            effective_key = api_key or settings.gemini_api_key
            if not effective_key:
                raise ValueError("GEMINI_API_KEY is not configured.")
            self._client = genai.Client(api_key=effective_key)
            self._types = types
            self._api_key = effective_key
            self.credential_alias = credential_alias
            self.company_id = company_id
            self.service_id = service_id
            self.module = module
        except ImportError:
            raise RuntimeError("google-genai package not installed")

    @property
    def api_key(self) -> str:
        return self._api_key

    async def complete_text(
        self,
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
        model_name = model or settings.gemini_report_model
        
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
                gemini_contents.append(
                    self._types.Content(
                        role="user",
                        parts=[self._types.Part.from_text(text=content)]
                    )
                )
            elif role in ["assistant", "model"]:
                gemini_contents.append(
                    self._types.Content(
                        role="model",
                        parts=[self._types.Part.from_text(text=content)]
                    )
                )
        
        config_args = {
            "temperature": temperature,
            "system_instruction": system_instruction,
        }
        if response_format == "json_object":
            config_args["response_mime_type"] = "application/json"
            
        if settings.gemini_max_output_tokens:
            config_args["max_output_tokens"] = settings.gemini_max_output_tokens

        logger.info("Calling Gemini complete_text (google-genai): model=%s, temp=%.2f, json=%s", model_name, temperature, response_format)

        cid = company_id if company_id is not None else self.company_id
        sid = service_id if service_id is not None else self.service_id
        mod = module if module is not None else self.module
        op_name = operation or "complete_text"

        t_start = time.perf_counter()
        try:
            response = await self._client.aio.models.generate_content(
                model=model_name,
                contents=gemini_contents,
                config=self._types.GenerateContentConfig(**config_args)
            )
            duration = time.perf_counter() - t_start
            duration_ms = int(duration * 1000)
            logger.info("Gemini complete_text completed in %.2f s", duration)

            resp_id = getattr(response, "response_id", None)
            raw_usage = getattr(response, "usage_metadata", None)
            usage = parse_usage_metadata(
                raw_usage,
                model=model_name,
                credential_alias=self.credential_alias,
                response_id=resp_id,
                execution_latency_ms=duration_ms,
                audio_duration_seconds=audio_duration_seconds,
                status="success",
                attempt_number=1,
            )

            await record_ai_usage_event(
                company_id=cid,
                service_id=sid,
                module=mod,
                operation=op_name,
                provider="gemini",
                model=model_name,
                credential_alias=self.credential_alias,
                usage=usage,
                request_id=resp_id,
                call_id=call_id,
                mass_analysis_id=mass_analysis_id,
                cycle_id=cycle_id,
                trainer_session_id=trainer_session_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=duration_ms,
                status="success",
                attempt_number=1,
            )

            text_out = response.text or ""
            if return_usage:
                return AIResponse(
                    text=text_out,
                    usage=usage,
                    response_id=resp_id,
                    model=model_name,
                    provider="gemini",
                    credential_alias=self.credential_alias,
                )
            return text_out

        except Exception as exc:
            duration_ms = int((time.perf_counter() - t_start) * 1000)
            await record_ai_usage_event(
                company_id=cid,
                service_id=sid,
                module=mod,
                operation=op_name,
                provider="gemini",
                model=model_name,
                credential_alias=self.credential_alias,
                usage=None,
                call_id=call_id,
                mass_analysis_id=mass_analysis_id,
                cycle_id=cycle_id,
                trainer_session_id=trainer_session_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=duration_ms,
                status="error",
                attempt_number=1,
            )
            raise

    async def analyze_audio_bytes(
        self,
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
        
        audio_part = self._types.Part.from_bytes(
            data=audio_bytes,
            mime_type=mime_type
        )
        
        contents = [
            prompt_text,
            audio_part
        ]
        
        config_args = {
            "temperature": settings.gemini_temperature,
            "response_mime_type": "application/json",
            "system_instruction": system_prompt
        }
        if settings.gemini_max_output_tokens:
            config_args["max_output_tokens"] = settings.gemini_max_output_tokens

        logger.info("Calling Gemini analyze_audio_bytes (google-genai): model=%s, format=%s, size=%.2f MB", model_name, audio_format, len(audio_bytes)/(1024*1024))

        cid = company_id if company_id is not None else self.company_id
        sid = service_id if service_id is not None else self.service_id
        mod = module if module is not None else self.module
        op_name = operation or "analyze_audio_bytes"

        max_attempts = 5
        last_exception = None

        for attempt in range(1, max_attempts + 1):
            t_attempt_start = time.perf_counter()
            try:
                response = await self._client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=self._types.GenerateContentConfig(**config_args)
                )
                duration = time.perf_counter() - t_attempt_start
                duration_ms = int(duration * 1000)
                logger.info(
                    "Gemini analyze_audio_bytes completed in %.2f s (attempt %d/%d)",
                    duration, attempt, max_attempts,
                    extra={
                        "provider": "gemini",
                        "model": model_name,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "transient": False,
                        "elapsed": duration,
                    }
                )
                resp_id = getattr(response, "response_id", None)
                raw_usage = getattr(response, "usage_metadata", None)
                usage = parse_usage_metadata(
                    raw_usage,
                    model=model_name,
                    credential_alias=self.credential_alias,
                    response_id=resp_id,
                    execution_latency_ms=duration_ms,
                    audio_duration_seconds=audio_duration_seconds,
                    status="success",
                    attempt_number=attempt,
                )

                await record_ai_usage_event(
                    company_id=cid,
                    service_id=sid,
                    module=mod,
                    operation=op_name,
                    provider="gemini",
                    model=model_name,
                    credential_alias=self.credential_alias,
                    usage=usage,
                    request_id=resp_id,
                    call_id=call_id,
                    mass_analysis_id=mass_analysis_id,
                    cycle_id=cycle_id,
                    trainer_session_id=trainer_session_id,
                    audio_duration_seconds=audio_duration_seconds,
                    execution_latency_ms=duration_ms,
                    status="success",
                    attempt_number=attempt,
                )

                text_out = response.text or ""
                if return_usage:
                    return AIResponse(
                        text=text_out,
                        usage=usage,
                        response_id=resp_id,
                        model=model_name,
                        provider="gemini",
                        credential_alias=self.credential_alias,
                    )
                return text_out

            except Exception as e:
                last_exception = e
                elapsed = time.perf_counter() - t_attempt_start
                elapsed_ms = int(elapsed * 1000)
                is_transient = is_transient_llm_error(e)
                status_str = "transient_retry" if (is_transient and attempt < max_attempts) else "error"

                await record_ai_usage_event(
                    company_id=cid,
                    service_id=sid,
                    module=mod,
                    operation=op_name,
                    provider="gemini",
                    model=model_name,
                    credential_alias=self.credential_alias,
                    usage=None,
                    call_id=call_id,
                    mass_analysis_id=mass_analysis_id,
                    cycle_id=cycle_id,
                    trainer_session_id=trainer_session_id,
                    audio_duration_seconds=audio_duration_seconds,
                    execution_latency_ms=elapsed_ms,
                    status=status_str,
                    attempt_number=attempt,
                )

                if is_transient and attempt < max_attempts:
                    delay = (1.0 * (2 ** (attempt - 1))) + random.uniform(0.1, 0.35)
                    logger.warning(
                        "[gemini_retry] attempt=%d/%d delay_seconds=%.2f error_type=transient: %s. Retrying in %.2fs...",
                        attempt, max_attempts, delay, e, delay,
                        extra={
                            "provider": "gemini",
                            "model": model_name,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "transient": True,
                            "elapsed": elapsed,
                        }
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "Gemini analyze_audio_bytes failed [attempt %d/%d, transient=%s, elapsed=%.2fs]: %s",
                        attempt, max_attempts, is_transient, elapsed, e,
                        extra={
                            "provider": "gemini",
                            "model": model_name,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "transient": is_transient,
                            "elapsed": elapsed,
                        }
                    )
                    raise last_exception

    async def transcribe_audio(
        self,
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
        model_name = settings.gemini_analysis_model
        
        audio_format = "mp3"
        if filename.lower().endswith(".wav"):
            audio_format = "wav"
            
        mime_type = "audio/mp3" if audio_format.lower() in ["mp3", "mpeg"] else f"audio/{audio_format.lower()}"
        
        audio_part = self._types.Part.from_bytes(
            data=audio_bytes,
            mime_type=mime_type
        )
        
        system_prompt = (
            "Eres un transcriptor experto. Tu única tarea es transcribir el audio completo de forma exacta, "
            "palabra por palabra. Si hay varios hablantes, sepáralos en líneas e indícalo si es claro. "
            "Devuelve estrictamente el texto transcribido. No agregues introducciones, conclusiones, explicaciones ni etiquetas markdown."
        )
        
        contents = [
            "Transcribe esta llamada telefónica de forma exacta.",
            audio_part
        ]
        
        config_args = {
            "system_instruction": system_prompt
        }
        
        logger.info("Calling Gemini transcribe_audio (google-genai): model=%s, size=%.2f MB", model_name, len(audio_bytes)/(1024*1024))

        cid = company_id if company_id is not None else self.company_id
        sid = service_id if service_id is not None else self.service_id
        mod = module if module is not None else self.module
        op_name = operation or "transcribe_audio"

        t_start = time.perf_counter()
        try:
            response = await self._client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=self._types.GenerateContentConfig(**config_args)
            )
            
            duration = time.perf_counter() - t_start
            duration_ms = int(duration * 1000)
            logger.info("Gemini transcribe_audio completed in %.2f s", duration)

            resp_id = getattr(response, "response_id", None)
            raw_usage = getattr(response, "usage_metadata", None)
            usage = parse_usage_metadata(
                raw_usage,
                model=model_name,
                credential_alias=self.credential_alias,
                response_id=resp_id,
                execution_latency_ms=duration_ms,
                audio_duration_seconds=audio_duration_seconds,
                status="success",
                attempt_number=1,
            )

            await record_ai_usage_event(
                company_id=cid,
                service_id=sid,
                module=mod,
                operation=op_name,
                provider="gemini",
                model=model_name,
                credential_alias=self.credential_alias,
                usage=usage,
                request_id=resp_id,
                call_id=call_id,
                mass_analysis_id=mass_analysis_id,
                cycle_id=cycle_id,
                trainer_session_id=trainer_session_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=duration_ms,
                status="success",
                attempt_number=1,
            )

            res_dict = {
                "text": response.text or "",
                "model": model_name,
                "provider": "gemini"
            }
            if return_usage:
                return AIResponse(
                    text=response.text or "",
                    usage=usage,
                    response_id=resp_id,
                    model=model_name,
                    provider="gemini",
                    credential_alias=self.credential_alias,
                    data=res_dict,
                )
            return res_dict

        except Exception as exc:
            duration_ms = int((time.perf_counter() - t_start) * 1000)
            await record_ai_usage_event(
                company_id=cid,
                service_id=sid,
                module=mod,
                operation=op_name,
                provider="gemini",
                model=model_name,
                credential_alias=self.credential_alias,
                usage=None,
                call_id=call_id,
                mass_analysis_id=mass_analysis_id,
                cycle_id=cycle_id,
                trainer_session_id=trainer_session_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=duration_ms,
                status="error",
                attempt_number=1,
            )
            raise


class AzureOpenAIProvider(AIProvider):
    async def complete_text(
        self,
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
        
        kwargs: Dict[str, Any] = {
            "model": deployment,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
            
        logger.info("Calling Legacy Azure complete_text (deployment: %s)", deployment)
        response = await client.chat.completions.create(**kwargs)
        text_out = response.choices[0].message.content or ""
        if return_usage:
            return AIResponse(
                text=text_out,
                usage=None,
                response_id=getattr(response, "id", None),
                model=deployment,
                provider="azure_openai",
            )
        return text_out

    async def analyze_audio_bytes(
        self,
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
        text_out = response.choices[0].message.content or ""
        if return_usage:
            return AIResponse(
                text=text_out,
                usage=None,
                response_id=getattr(response, "id", None),
                model=deployment,
                provider="azure_openai",
            )
        return text_out

    async def transcribe_audio(
        self,
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
        res_dict = {
            "text": response.text,
            "model": deployment,
            "provider": "azure_openai"
        }
        if return_usage:
            return AIResponse(
                text=response.text,
                usage=None,
                response_id=getattr(response, "id", None),
                model=deployment,
                provider="azure_openai",
                data=res_dict,
            )
        return res_dict


def get_ai_provider(
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
    module: Optional[str] = None,
) -> AIProvider:
    """Resolve and return the active AI provider based on settings and context."""
    provider = (settings.ai_provider or "gemini").lower()
    if provider == "gemini":
        from app.services.ai_credentials import resolve_gemini_credentials
        api_key, alias = resolve_gemini_credentials(company_id=company_id, service_id=service_id, module=module)
        return GeminiProvider(
            api_key=api_key,
            credential_alias=alias,
            company_id=company_id,
            service_id=service_id,
            module=module,
        )
    elif provider in ["azure", "azure_openai", "openai"]:
        return AzureOpenAIProvider()
    else:
        raise ValueError(f"Unknown AI provider: {provider}")
