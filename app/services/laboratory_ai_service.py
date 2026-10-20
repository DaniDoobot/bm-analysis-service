"""
Isolated AI Model Router & Multi-Provider Execution Service for Laboratory (test_analysis).

STRICT ISOLATION PRINCIPLE:
- This module is used EXCLUSIVELY by Laboratory / Test Analysis endpoints when `model_key` is specified,
  or by the Laboratory model catalog/availability endpoints.
- It NEVER modifies global Speech defaults, Mass Evaluations, Automations, Trainer, Tutor IA, Cycles, or CloudTalk.
- It NEVER performs silent fallback to another model if a requested model fails, lacks credentials, or is unauthorized.
"""
import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import get_settings
from app.services.ai_credentials import resolve_gemini_credentials
from app.services.ai_usage_service import (
    AIUsageMetadata,
    parse_usage_metadata,
    record_ai_usage_event,
)

logger = logging.getLogger(__name__)

# Canonical FinOps module name for Laboratory
LAB_FINOPS_MODULE = "test_analysis"

# Exact required Spanish message when prompt + context exceeds model context window
CONTEXT_LIMIT_ERROR_MESSAGE = (
    "El contexto de esta estructura supera el límite admitido por el modelo seleccionado."
)


class LaboratoryExecutionError(Exception):
    """Controlled exception raised during Laboratory model validation or execution."""

    def __init__(
        self,
        stage: str,
        message: str,
        status_code: int = 400,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        model_key: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.status_code = status_code
        self.provider = provider
        self.model = model
        self.model_key = model_key
        self.details = details or {}


@dataclass
class LaboratoryExecutionResult:
    """Standardized output from a Laboratory AI execution (direct_audio or stt_plus_llm)."""
    raw_response: str
    transcription: Optional[str]
    requested_model_key: str
    pipeline_mode: str  # "direct_audio" | "stt_plus_llm"
    actual_provider: str
    actual_model: str
    stt_provider: Optional[str] = None
    stt_model: Optional[str] = None
    analysis_provider: Optional[str] = None
    analysis_model: Optional[str] = None
    estimated_cost_usd: Optional[float] = None
    stt_cost_usd: Optional[float] = None
    llm_cost_usd: Optional[float] = None
    pricing_status: str = "unpriced"
    latency_ms: int = 0
    stt_latency_ms: Optional[int] = None
    llm_latency_ms: Optional[int] = None
    usage_events: List[Dict[str, Any]] = field(default_factory=list)


def get_lab_model_catalog() -> Dict[str, Dict[str, Any]]:
    """
    Build the central Laboratory model catalog reading dynamic model names from Settings.
    """
    s = get_settings()
    return {
        "gemini_3_8_flash": {
            "model_key": "gemini_3_8_flash",
            "display_name": "Gemini 3.8 Flash",
            "provider": "Google",
            "provider_key": "gemini",
            "mode": "direct_audio",
            "model_name": s.lab_gemini_3_8_model or "gemini-3.8-flash",
            "stt_provider": None,
            "stt_model": None,
            "llm_provider": "gemini",
            "llm_model": s.lab_gemini_3_8_model or "gemini-3.8-flash",
            "enabled": True,
            "supports_audio": True,
            "context_limit": 1048576,
            "pricing_key": "gemini:gemini-3.8-flash",
            "pricing_components": [
                "input_text_tokens",
                "input_audio_tokens",
                "cached_input_tokens",
                "output_text_tokens",
                "thoughts_tokens",
            ],
            "required_env_vars": ["LAB_GEMINI_API_KEY"],
        },
        "gemini_3_5_flash_lite": {
            "model_key": "gemini_3_5_flash_lite",
            "display_name": "Gemini 3.5 Flash-Lite",
            "provider": "Google",
            "provider_key": "gemini",
            "mode": "direct_audio",
            "model_name": s.lab_gemini_3_5_flash_lite_model or "gemini-3.5-flash-lite",
            "stt_provider": None,
            "stt_model": None,
            "llm_provider": "gemini",
            "llm_model": s.lab_gemini_3_5_flash_lite_model or "gemini-3.5-flash-lite",
            "enabled": True,
            "supports_audio": True,
            "context_limit": 1048576,
            "pricing_key": "gemini:gemini-3.5-flash-lite",
            "pricing_components": [
                "input_text_tokens",
                "input_audio_tokens",
                "cached_input_tokens",
                "output_text_tokens",
            ],
            "required_env_vars": ["LAB_GEMINI_API_KEY", "LAB_GEMINI_3_5_FLASH_LITE_MODEL"],
        },
        "gemini_3_1_flash_lite": {
            "model_key": "gemini_3_1_flash_lite",
            "display_name": "Gemini 3.1 Flash-Lite",
            "provider": "Google",
            "provider_key": "gemini",
            "mode": "direct_audio",
            "model_name": s.lab_gemini_3_1_flash_lite_model or "gemini-3.1-flash-lite",
            "stt_provider": None,
            "stt_model": None,
            "llm_provider": "gemini",
            "llm_model": s.lab_gemini_3_1_flash_lite_model or "gemini-3.1-flash-lite",
            "enabled": True,
            "supports_audio": True,
            "context_limit": 1048576,
            "pricing_key": "gemini:gemini-3.1-flash-lite",
            "pricing_components": [
                "input_text_tokens",
                "input_audio_tokens",
                "cached_input_tokens",
                "output_text_tokens",
            ],
            "required_env_vars": ["LAB_GEMINI_API_KEY", "LAB_GEMINI_3_1_FLASH_LITE_MODEL"],
        },
        "gemini_2_5_flash_lite": {
            "model_key": "gemini_2_5_flash_lite",
            "display_name": "Gemini 2.5 Flash-Lite",
            "provider": "Google",
            "provider_key": "gemini",
            "mode": "direct_audio",
            "model_name": s.lab_gemini_2_5_flash_lite_model or "gemini-2.5-flash-lite",
            "stt_provider": None,
            "stt_model": None,
            "llm_provider": "gemini",
            "llm_model": s.lab_gemini_2_5_flash_lite_model or "gemini-2.5-flash-lite",
            "enabled": True,
            "supports_audio": True,
            "context_limit": 1048576,
            "pricing_key": "gemini:gemini-2.5-flash-lite",
            "pricing_components": [
                "input_text_tokens",
                "input_audio_tokens",
                "cached_input_tokens",
                "output_text_tokens",
            ],
            "required_env_vars": ["LAB_GEMINI_API_KEY", "LAB_GEMINI_2_5_FLASH_LITE_MODEL"],
        },
        "voxtral_small": {
            "model_key": "voxtral_small",
            "display_name": "Mistral Voxtral Small",
            "provider": "Mistral",
            "provider_key": "mistral",
            "mode": "direct_audio",
            "model_name": s.lab_mistral_voxtral_small_model or "voxtral-small-latest",
            "stt_provider": None,
            "stt_model": None,
            "llm_provider": "mistral",
            "llm_model": s.lab_mistral_voxtral_small_model or "voxtral-small-latest",
            "enabled": True,
            "supports_audio": True,
            "context_limit": 32000,
            "pricing_key": "mistral:voxtral-small-latest",
            "pricing_components": [
                "audio_duration_minutes",
                "input_text_tokens",
                "output_text_tokens",
            ],
            "required_env_vars": ["LAB_MISTRAL_API_KEY", "LAB_MISTRAL_VOXTRAL_SMALL_MODEL"],
        },
        "groq_whisper_gpt_luna": {
            "model_key": "groq_whisper_gpt_luna",
            "display_name": "Groq Whisper Large v3 Turbo + GPT-5.6 Luna",
            "provider": "Groq + OpenAI",
            "provider_key": "groq+openai",
            "mode": "stt_plus_llm",
            "model_name": f"{s.lab_groq_whisper_model or 'whisper-large-v3-turbo'} + {s.lab_openai_luna_model or 'gpt-5.6-luna'}",
            "stt_provider": "groq",
            "stt_model": s.lab_groq_whisper_model or "whisper-large-v3-turbo",
            "llm_provider": "openai",
            "llm_model": s.lab_openai_luna_model or "gpt-5.6-luna",
            "enabled": True,
            "supports_audio": True,
            "context_limit": 128000,
            "pricing_key": "groq:whisper-large-v3-turbo+openai:gpt-5.6-luna",
            "pricing_components": [
                "stt_audio_minutes",
                "llm_input_text_tokens",
                "llm_cached_input_tokens",
                "llm_output_text_tokens",
            ],
            "required_env_vars": [
                "LAB_GROQ_API_KEY",
                "LAB_GROQ_WHISPER_MODEL",
                "LAB_OPENAI_API_KEY",
                "LAB_OPENAI_LUNA_MODEL",
            ],
        },
        "mistral_transcribe_gpt_luna": {
            "model_key": "mistral_transcribe_gpt_luna",
            "display_name": "Mistral Voxtral Mini Transcribe 2 + GPT-5.6 Luna",
            "provider": "Mistral + OpenAI",
            "provider_key": "mistral+openai",
            "mode": "stt_plus_llm",
            "model_name": f"{s.lab_mistral_transcribe_model or 'voxtral-mini-latest'} + {s.lab_openai_luna_model or 'gpt-5.6-luna'}",
            "stt_provider": "mistral",
            "stt_model": s.lab_mistral_transcribe_model or "voxtral-mini-latest",
            "llm_provider": "openai",
            "llm_model": s.lab_openai_luna_model or "gpt-5.6-luna",
            "enabled": True,
            "supports_audio": True,
            "context_limit": 128000,
            "pricing_key": "mistral:voxtral-mini-latest+openai:gpt-5.6-luna",
            "pricing_components": [
                "stt_audio_minutes",
                "llm_input_text_tokens",
                "llm_cached_input_tokens",
                "llm_output_text_tokens",
            ],
            "required_env_vars": [
                "LAB_MISTRAL_API_KEY",
                "LAB_MISTRAL_TRANSCRIBE_MODEL",
                "LAB_OPENAI_API_KEY",
                "LAB_OPENAI_LUNA_MODEL",
            ],
        },
        "deepgram_nova3_gpt_luna": {
            "model_key": "deepgram_nova3_gpt_luna",
            "display_name": "Deepgram Nova-3 + GPT-5.6 Luna",
            "provider": "Deepgram + OpenAI",
            "provider_key": "deepgram+openai",
            "mode": "stt_plus_llm",
            "model_name": f"{s.lab_deepgram_model or 'nova-3'} + {s.lab_openai_luna_model or 'gpt-5.6-luna'}",
            "stt_provider": "deepgram",
            "stt_model": s.lab_deepgram_model or "nova-3",
            "llm_provider": "openai",
            "llm_model": s.lab_openai_luna_model or "gpt-5.6-luna",
            "enabled": True,
            "supports_audio": True,
            "context_limit": 128000,
            "pricing_key": "deepgram:nova-3+openai:gpt-5.6-luna",
            "pricing_components": [
                "stt_audio_minutes",
                "llm_input_text_tokens",
                "llm_cached_input_tokens",
                "llm_output_text_tokens",
            ],
            "required_env_vars": [
                "LAB_DEEPGRAM_API_KEY",
                "LAB_DEEPGRAM_MODEL",
                "LAB_OPENAI_API_KEY",
                "LAB_OPENAI_LUNA_MODEL",
            ],
        },
    }


# Keep module-level alias for direct imports/inspection
LAB_MODEL_CATALOG = get_lab_model_catalog()


def _resolve_lab_gemini_credentials(
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
) -> Tuple[str, str]:
    """
    Resolve Gemini API key for Laboratory:
    1. Explicit `LAB_GEMINI_API_KEY` if configured.
    2. Explicit fallback to the existing `test_analysis` credential resolution.
    Never modifies or touches credentials of other modules.
    """
    s = get_settings()
    if s.lab_gemini_api_key and s.lab_gemini_api_key.strip():
        return s.lab_gemini_api_key.strip(), "lab_gemini_key"

    api_key, alias = resolve_gemini_credentials(
        company_id=company_id,
        service_id=service_id,
        module=LAB_FINOPS_MODULE,
    )
    return (api_key or "").strip(), alias


def validate_model_configuration(
    model_key: str,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Validate whether `model_key` exists in `LAB_MODEL_CATALOG` and has all required
    credentials and model identifiers configured. Never exposes secret values.
    """
    catalog = get_lab_model_catalog()
    if not model_key or model_key not in catalog:
        return {
            "valid": False,
            "configured": False,
            "model_key": model_key,
            "missing_vars": [],
            "detail": f"Modelo '{model_key}' desconocido. Selecciona un modelo válido del catálogo de Laboratorio.",
        }

    entry = catalog[model_key]
    if not entry.get("enabled", True):
        return {
            "valid": False,
            "configured": False,
            "model_key": model_key,
            "missing_vars": [],
            "detail": f"El modelo '{entry['display_name']}' está deshabilitado actualmente.",
        }

    s = get_settings()
    missing_vars: List[str] = []

    provider_key = entry["provider_key"]
    if provider_key == "gemini":
        gem_key, _ = _resolve_lab_gemini_credentials(company_id=company_id, service_id=service_id)
        if not gem_key:
            missing_vars.append("LAB_GEMINI_API_KEY")
        if not entry.get("model_name"):
            missing_vars.append("LAB_GEMINI_MODEL")

    elif provider_key == "mistral":
        if not (s.lab_mistral_api_key and s.lab_mistral_api_key.strip()):
            missing_vars.append("LAB_MISTRAL_API_KEY")
        if not (s.lab_mistral_voxtral_small_model and s.lab_mistral_voxtral_small_model.strip()):
            missing_vars.append("LAB_MISTRAL_VOXTRAL_SMALL_MODEL")

    elif provider_key == "groq+openai":
        if not (s.lab_groq_api_key and s.lab_groq_api_key.strip()):
            missing_vars.append("LAB_GROQ_API_KEY")
        if not (s.lab_groq_whisper_model and s.lab_groq_whisper_model.strip()):
            missing_vars.append("LAB_GROQ_WHISPER_MODEL")
        if not (s.lab_openai_api_key and s.lab_openai_api_key.strip()):
            missing_vars.append("LAB_OPENAI_API_KEY")
        if not (s.lab_openai_luna_model and s.lab_openai_luna_model.strip()):
            missing_vars.append("LAB_OPENAI_LUNA_MODEL")

    elif provider_key == "mistral+openai":
        if not (s.lab_mistral_api_key and s.lab_mistral_api_key.strip()):
            missing_vars.append("LAB_MISTRAL_API_KEY")
        if not (s.lab_mistral_transcribe_model and s.lab_mistral_transcribe_model.strip()):
            missing_vars.append("LAB_MISTRAL_TRANSCRIBE_MODEL")
        if not (s.lab_openai_api_key and s.lab_openai_api_key.strip()):
            missing_vars.append("LAB_OPENAI_API_KEY")
        if not (s.lab_openai_luna_model and s.lab_openai_luna_model.strip()):
            missing_vars.append("LAB_OPENAI_LUNA_MODEL")

    elif provider_key == "deepgram+openai":
        if not (s.lab_deepgram_api_key and s.lab_deepgram_api_key.strip()):
            missing_vars.append("LAB_DEEPGRAM_API_KEY")
        if not (s.lab_deepgram_model and s.lab_deepgram_model.strip()):
            missing_vars.append("LAB_DEEPGRAM_MODEL")
        if not (s.lab_openai_api_key and s.lab_openai_api_key.strip()):
            missing_vars.append("LAB_OPENAI_API_KEY")
        if not (s.lab_openai_luna_model and s.lab_openai_luna_model.strip()):
            missing_vars.append("LAB_OPENAI_LUNA_MODEL")

    configured = len(missing_vars) == 0
    if configured:
        detail = "Modelo configurado"
    else:
        detail = f"Modelo no configurado. Faltan variables de entorno: {', '.join(missing_vars)}"

    return {
        "valid": True,
        "configured": configured,
        "model_key": model_key,
        "missing_vars": missing_vars,
        "detail": detail,
        "entry": entry,
    }


def list_laboratory_models(
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return the list of enabled Laboratory models for the frontend selector.
    Includes `configured: true/false` without exposing any secrets.
    """
    catalog = get_lab_model_catalog()
    output: List[Dict[str, Any]] = []
    for key, entry in catalog.items():
        if not entry.get("enabled", True):
            continue
        val = validate_model_configuration(key, company_id=company_id, service_id=service_id)
        is_configured = bool(val.get("configured"))
        output.append({
            "model_key": entry["model_key"],
            "display_name": entry["display_name"],
            "provider": entry["provider"],
            "provider_key": entry["provider_key"],
            "mode": entry["mode"],
            "model_name": entry["model_name"],
            "stt_provider": entry["stt_provider"],
            "stt_model": entry["stt_model"],
            "llm_provider": entry["llm_provider"],
            "llm_model": entry["llm_model"],
            "supports_audio": entry["supports_audio"],
            "context_limit": entry["context_limit"],
            "configured": is_configured,
            "status_label": "Disponible" if is_configured else "No configurado",
        })
    return output


async def check_model_availability(
    model_key: str,
    company_id: Optional[int] = None,
    service_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Verify real availability of a Laboratory model using a lightweight metadata/auth request.
    Never performs silent fallback.
    """
    val = validate_model_configuration(model_key, company_id=company_id, service_id=service_id)
    if not val["valid"]:
        raise LaboratoryExecutionError(
            stage="validation",
            message=val["detail"],
            status_code=400,
            model_key=model_key,
        )

    if not val["configured"]:
        return {
            "model_key": model_key,
            "configured": False,
            "reachable": False,
            "detail": val["detail"],
        }

    entry = val["entry"]
    s = get_settings()
    provider_key = entry["provider_key"]

    try:
        if provider_key == "gemini":
            api_key, _ = _resolve_lab_gemini_credentials(company_id=company_id, service_id=service_id)
            model_name = entry["model_name"]
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            # Perform a minimal REAL inference request with generate_content
            # to verify actual execution permissions and model availability.
            # (GET metadata can return 200 while generate_content returns 404).
            await client.aio.models.generate_content(
                model=model_name,
                contents="Responde únicamente OK",
                config=types.GenerateContentConfig(max_output_tokens=5, temperature=0.0),
            )
            return {
                "model_key": model_key,
                "model_name": model_name,
                "configured": True,
                "reachable": True,
                "detail": "Modelo disponible y verificado con inferencia real",
            }

        elif provider_key == "mistral":
            model_name = entry["model_name"]
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"https://api.mistral.ai/v1/models/{model_name}",
                    headers={"Authorization": f"Bearer {s.lab_mistral_api_key.strip()}"},
                )
                if resp.status_code == 200:
                    return {
                        "model_key": model_key,
                        "model_name": model_name,
                        "configured": True,
                        "reachable": True,
                        "detail": "Modelo disponible",
                    }
                return {
                    "model_key": model_key,
                    "model_name": model_name,
                    "configured": True,
                    "reachable": False,
                    "detail": _map_http_availability_error("Mistral", model_name, resp.status_code, resp.text),
                }

        elif entry["mode"] == "stt_plus_llm":
            stt_provider = entry["stt_provider"]
            stt_model = entry["stt_model"]
            llm_model = entry["llm_model"]

            async with httpx.AsyncClient(timeout=15.0) as client:
                # 1. Check STT provider
                if stt_provider == "groq":
                    r_stt = await client.get(
                        f"https://api.groq.com/openai/v1/models/{stt_model}",
                        headers={"Authorization": f"Bearer {s.lab_groq_api_key.strip()}"},
                    )
                    if r_stt.status_code != 200:
                        return {
                            "model_key": model_key,
                            "configured": True,
                            "reachable": False,
                            "detail": _map_http_availability_error("Groq STT", stt_model, r_stt.status_code, r_stt.text),
                        }
                elif stt_provider == "mistral":
                    r_stt = await client.get(
                        f"https://api.mistral.ai/v1/models/{stt_model}",
                        headers={"Authorization": f"Bearer {s.lab_mistral_api_key.strip()}"},
                    )
                    if r_stt.status_code != 200:
                        return {
                            "model_key": model_key,
                            "configured": True,
                            "reachable": False,
                            "detail": _map_http_availability_error("Mistral STT", stt_model, r_stt.status_code, r_stt.text),
                        }
                elif stt_provider == "deepgram":
                    r_stt = await client.get(
                        "https://api.deepgram.com/v1/projects",
                        headers={"Authorization": f"Token {s.lab_deepgram_api_key.strip()}"},
                    )
                    if r_stt.status_code != 200:
                        return {
                            "model_key": model_key,
                            "configured": True,
                            "reachable": False,
                            "detail": _map_http_availability_error("Deepgram STT", stt_model, r_stt.status_code, r_stt.text),
                        }

                # 2. Check OpenAI LLM
                r_llm = await client.get(
                    f"https://api.openai.com/v1/models/{llm_model}",
                    headers={"Authorization": f"Bearer {s.lab_openai_api_key.strip()}"},
                )
                if r_llm.status_code != 200:
                    return {
                        "model_key": model_key,
                        "configured": True,
                        "reachable": False,
                        "detail": _map_http_availability_error("OpenAI", llm_model, r_llm.status_code, r_llm.text),
                    }

            return {
                "model_key": model_key,
                "configured": True,
                "reachable": True,
                "detail": "Modelo disponible",
            }

    except Exception as exc:
        err_str = str(exc).lower()
        if "not found" in err_str or "404" in err_str:
            detail = f"El modelo '{entry.get('model_name', model_key)}' no está disponible o no existe para la API key actual."
            error_code = "model_not_available"
        elif "permission" in err_str or "403" in err_str or "401" in err_str or "api_key" in err_str or "unauthorized" in err_str:
            detail = f"La cuenta/API key no tiene permisos para ejecutar inferencia con '{entry.get('model_name', model_key)}'."
            error_code = "permission_denied"
        else:
            detail = f"No se pudo verificar disponibilidad del modelo: {str(exc)}"
            error_code = "inference_check_failed"
        return {
            "model_key": model_key,
            "model_name": entry.get("model_name"),
            "configured": True,
            "reachable": False,
            "error_code": error_code,
            "detail": detail,
        }

    return {
        "model_key": model_key,
        "configured": True,
        "reachable": False,
        "detail": "Proveedor no soportado en verificación.",
    }


def _map_http_availability_error(provider_label: str, model_name: str, status_code: int, body_text: str) -> str:
    if status_code == 404:
        return f"El modelo '{model_name}' no fue encontrado en {provider_label}."
    if status_code in (401, 403):
        return f"La cuenta/API key no tiene acceso al modelo '{model_name}' en {provider_label}."
    return f"{provider_label} devolvió HTTP {status_code} al verificar el modelo '{model_name}'."


def _check_context_limit(text_input: str, context_limit: Optional[int], model_key: str) -> None:
    """
    Validate that the input text does not exceed the known context window of the model.
    Uses a conservative character-to-token heuristic (~3.2 chars/token) to prevent silent truncation.
    """
    if not context_limit or not text_input:
        return
    estimated_tokens = len(text_input) // 3
    if estimated_tokens > context_limit:
        raise LaboratoryExecutionError(
            stage="context_limit",
            message=CONTEXT_LIMIT_ERROR_MESSAGE,
            status_code=422,
            model_key=model_key,
        )


def _aggregate_costs(events_meta: List[Optional[Dict[str, Any]]]) -> Tuple[Optional[float], str]:
    """
    Combine estimated costs and pricing statuses across 1 or 2 FinOps usage events.
    """
    valid_events = [e for e in events_meta if isinstance(e, dict)]
    if not valid_events:
        return None, "unpriced"

    costs = [e.get("estimated_cost_usd") for e in valid_events]
    statuses = [e.get("pricing_status", "unpriced") for e in valid_events]

    if all(s == "priced" for s in statuses) and all(c is not None for c in costs):
        return round(sum(float(c) for c in costs), 6), "priced"

    if any(c is not None for c in costs):
        total = round(sum(float(c) for c in costs if c is not None), 6)
        return total, "partial"

    return None, "unpriced"


class LaboratoryAIProviderService:
    """
    Isolated execution engine for Laboratory benchmark runs.
    Supports:
      - direct_audio (Google Gemini 3.8 / 3.5 Flash-Lite / 2.5 Flash-Lite, Mistral Voxtral Small)
      - stt_plus_llm (Groq Whisper / Mistral Voxtral Mini Transcribe / Deepgram Nova-3 -> OpenAI GPT-5.6 Luna)
    """

    @classmethod
    async def execute_laboratory_analysis(
        cls,
        *,
        model_key: str,
        prompt_content: str,
        audio_bytes: Optional[bytes] = None,
        audio_format: str = "mp3",
        transcription: Optional[str] = None,
        call_id: Optional[str] = None,
        company_id: Optional[int] = None,
        service_id: Optional[int] = None,
        audio_duration_seconds: Optional[float] = None,
    ) -> LaboratoryExecutionResult:
        """
        Validate configuration and execute the selected Laboratory model pipeline without silent fallbacks.
        """
        val = validate_model_configuration(model_key, company_id=company_id, service_id=service_id)
        if not val["valid"]:
            raise LaboratoryExecutionError(
                stage="validation",
                message=val["detail"],
                status_code=400,
                model_key=model_key,
            )
        if not val["configured"]:
            raise LaboratoryExecutionError(
                stage="model_config",
                message=val["detail"],
                status_code=400,
                model_key=model_key,
                details={"missing_vars": val["missing_vars"]},
            )

        entry = val["entry"]
        mode = entry["mode"]

        if mode == "direct_audio":
            return await cls._execute_direct_audio(
                entry=entry,
                prompt_content=prompt_content,
                audio_bytes=audio_bytes,
                audio_format=audio_format,
                transcription=transcription,
                call_id=call_id,
                company_id=company_id,
                service_id=service_id,
                audio_duration_seconds=audio_duration_seconds,
            )
        elif mode == "stt_plus_llm":
            return await cls._execute_stt_plus_llm(
                entry=entry,
                prompt_content=prompt_content,
                audio_bytes=audio_bytes,
                audio_format=audio_format,
                transcription=transcription,
                call_id=call_id,
                company_id=company_id,
                service_id=service_id,
                audio_duration_seconds=audio_duration_seconds,
            )
        else:
            raise LaboratoryExecutionError(
                stage="validation",
                message=f"Modo de ejecución '{mode}' no soportado en Laboratorio.",
                status_code=400,
                model_key=model_key,
            )

    @classmethod
    async def _execute_direct_audio(
        cls,
        *,
        entry: Dict[str, Any],
        prompt_content: str,
        audio_bytes: Optional[bytes],
        audio_format: str,
        transcription: Optional[str],
        call_id: Optional[str],
        company_id: Optional[int],
        service_id: Optional[int],
        audio_duration_seconds: Optional[float],
    ) -> LaboratoryExecutionResult:
        model_key = entry["model_key"]
        provider_key = entry["provider_key"]
        model_name = entry["model_name"]
        context_limit = entry.get("context_limit")

        _check_context_limit(prompt_content, context_limit, model_key)

        if not audio_bytes and not transcription:
            raise LaboratoryExecutionError(
                stage="validation",
                message="El modelo seleccionado requiere audio o transcripción de entrada.",
                status_code=400,
                provider=provider_key,
                model=model_name,
                model_key=model_key,
            )

        if provider_key == "gemini":
            return await cls._run_gemini_direct(
                entry=entry,
                prompt_content=prompt_content,
                audio_bytes=audio_bytes,
                audio_format=audio_format,
                transcription=transcription,
                call_id=call_id,
                company_id=company_id,
                service_id=service_id,
                audio_duration_seconds=audio_duration_seconds,
            )
        elif provider_key == "mistral":
            return await cls._run_mistral_voxtral_direct(
                entry=entry,
                prompt_content=prompt_content,
                audio_bytes=audio_bytes,
                audio_format=audio_format,
                transcription=transcription,
                call_id=call_id,
                company_id=company_id,
                service_id=service_id,
                audio_duration_seconds=audio_duration_seconds,
            )
        else:
            raise LaboratoryExecutionError(
                stage="validation",
                message=f"Proveedor direct_audio '{provider_key}' no soportado.",
                status_code=400,
                model_key=model_key,
            )

    @classmethod
    async def _run_gemini_direct(
        cls,
        *,
        entry: Dict[str, Any],
        prompt_content: str,
        audio_bytes: Optional[bytes],
        audio_format: str,
        transcription: Optional[str],
        call_id: Optional[str],
        company_id: Optional[int],
        service_id: Optional[int],
        audio_duration_seconds: Optional[float],
    ) -> LaboratoryExecutionResult:
        s = get_settings()
        model_key = entry["model_key"]
        model_name = entry["model_name"]
        api_key, credential_alias = _resolve_lab_gemini_credentials(company_id=company_id, service_id=service_id)

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        system_prompt = (
            "Eres un experto analizador de llamadas. "
            "Devuelve EXCLUSIVAMENTE un objeto JSON válido y bien formado, sin markdown ni texto adicional. "
            "REGLAS CRÍTICAS para el JSON: "
            "1) Todos los caracteres especiales dentro de strings (comillas, saltos de línea, tabulaciones) DEBEN estar escapados correctamente. "
            "2) Nunca insertes saltos de línea literales dentro de valores de string JSON. "
            "3) El campo 'transcription' debe ser una cadena de texto plana con los turnos de conversación separados por \\n. "
            "4) No añadas ningún texto fuera del objeto JSON."
        )

        if audio_bytes:
            mime_type = "audio/mp3" if audio_format.lower() in ("mp3", "mpeg", ".mp3") else "audio/wav"
            audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
            contents: List[Any] = [prompt_content, audio_part]
            operation_name = "analyze_audio_bytes"
        else:
            contents = [
                f"{prompt_content}\n\nTranscripción de la llamada:\n\n{transcription}"
            ]
            operation_name = "analysis"

        config_args: Dict[str, Any] = {
            "temperature": s.gemini_temperature,
            "response_mime_type": "application/json",
            "system_instruction": system_prompt,
        }
        if s.gemini_max_output_tokens:
            config_args["max_output_tokens"] = s.gemini_max_output_tokens

        logger.info(
            "[Laboratory] Calling Gemini direct: model_key=%s model=%s operation=%s call_id=%s",
            model_key, model_name, operation_name, call_id,
        )

        t_start = time.perf_counter()
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(**config_args),
            )
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            resp_id = getattr(response, "response_id", None)
            raw_usage = getattr(response, "usage_metadata", None)

            usage = parse_usage_metadata(
                raw_usage,
                model=model_name,
                model_key=model_key,
                credential_alias=credential_alias,
                response_id=resp_id,
                execution_latency_ms=latency_ms,
                audio_duration_seconds=audio_duration_seconds,
                status="success",
                attempt_number=1,
                provider="gemini",
            )

            ev_meta = await record_ai_usage_event(
                company_id=company_id,
                service_id=service_id,
                module=LAB_FINOPS_MODULE,
                operation=operation_name,
                provider="gemini",
                model=model_name,
                model_key=model_key,
                credential_alias=credential_alias,
                usage=usage,
                request_id=resp_id,
                call_id=call_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=latency_ms,
                status="success",
                attempt_number=1,
            )

            raw_text = response.text or ""
            est_cost, p_status = _aggregate_costs([ev_meta])

            return LaboratoryExecutionResult(
                raw_response=raw_text,
                transcription=transcription,
                requested_model_key=model_key,
                pipeline_mode="direct_audio",
                actual_provider="gemini",
                actual_model=model_name,
                stt_provider=None,
                stt_model=None,
                analysis_provider="gemini",
                analysis_model=model_name,
                estimated_cost_usd=est_cost,
                pricing_status=p_status,
                latency_ms=latency_ms,
                usage_events=[ev_meta] if ev_meta else [],
            )

        except Exception as exc:
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            await record_ai_usage_event(
                company_id=company_id,
                service_id=service_id,
                module=LAB_FINOPS_MODULE,
                operation=operation_name,
                provider="gemini",
                model=model_name,
                model_key=model_key,
                credential_alias=credential_alias,
                usage=None,
                call_id=call_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=latency_ms,
                status="error",
                attempt_number=1,
            )
            raise _map_provider_exception(exc, provider="gemini", model=model_name, model_key=model_key, stage="llm")

    @classmethod
    async def _run_mistral_voxtral_direct(
        cls,
        *,
        entry: Dict[str, Any],
        prompt_content: str,
        audio_bytes: Optional[bytes],
        audio_format: str,
        transcription: Optional[str],
        call_id: Optional[str],
        company_id: Optional[int],
        service_id: Optional[int],
        audio_duration_seconds: Optional[float],
    ) -> LaboratoryExecutionResult:
        s = get_settings()
        model_key = entry["model_key"]
        model_name = entry["model_name"]
        context_limit = entry.get("context_limit", 32000)
        api_key = s.lab_mistral_api_key.strip()

        _check_context_limit(prompt_content + (transcription or ""), context_limit, model_key)

        if not audio_bytes and not transcription:
            raise LaboratoryExecutionError(
                stage="validation",
                message="Voxtral requiere audio de entrada para el análisis.",
                status_code=400,
                provider="mistral",
                model=model_name,
                model_key=model_key,
            )

        system_prompt = (
            "Eres un experto analizador de llamadas. "
            "Devuelve EXCLUSIVAMENTE un objeto JSON válido, sin markdown ni texto adicional."
        )

        if audio_bytes:
            b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
            user_content: Any = [
                {
                    "type": "input_audio",
                    "input_audio": b64_audio,
                },
                {
                    "type": "text",
                    "text": prompt_content,
                },
            ]
            operation_name = "analyze_audio_bytes"
        else:
            user_content = f"{prompt_content}\n\nTranscripción de la llamada:\n\n{transcription}"
            operation_name = "analysis"

        payload = {
            "model": model_name,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }

        logger.info(
            "[Laboratory] Calling Mistral Voxtral direct: model_key=%s model=%s call_id=%s",
            model_key, model_name, call_id,
        )

        t_start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=float(s.gemini_timeout_seconds or 120)) as client:
                resp = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

            latency_ms = int((time.perf_counter() - t_start) * 1000)

            if resp.status_code != 200:
                await record_ai_usage_event(
                    company_id=company_id,
                    service_id=service_id,
                    module=LAB_FINOPS_MODULE,
                    operation=operation_name,
                    provider="mistral",
                    model=model_name,
                    model_key=model_key,
                    credential_alias="lab_mistral",
                    usage=None,
                    call_id=call_id,
                    audio_duration_seconds=audio_duration_seconds,
                    execution_latency_ms=latency_ms,
                    status="error",
                    attempt_number=1,
                )
                raise _map_http_execution_error(
                    resp.status_code,
                    resp.text,
                    provider="mistral",
                    model=model_name,
                    model_key=model_key,
                    stage="llm",
                )

            data = resp.json()
            raw_text = (
                data.get("choices", [{}])[0].get("message", {}).get("content") or ""
            )
            raw_usage = data.get("usage") or {}
            resp_id = data.get("id")

            usage = parse_usage_metadata(
                raw_usage,
                model=model_name,
                model_key=model_key,
                credential_alias="lab_mistral",
                response_id=resp_id,
                execution_latency_ms=latency_ms,
                audio_duration_seconds=audio_duration_seconds,
                status="success",
                attempt_number=1,
                provider="mistral",
            )

            ev_meta = await record_ai_usage_event(
                company_id=company_id,
                service_id=service_id,
                module=LAB_FINOPS_MODULE,
                operation=operation_name,
                provider="mistral",
                model=model_name,
                model_key=model_key,
                credential_alias="lab_mistral",
                usage=usage,
                request_id=resp_id,
                call_id=call_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=latency_ms,
                status="success",
                attempt_number=1,
            )

            est_cost, p_status = _aggregate_costs([ev_meta])

            return LaboratoryExecutionResult(
                raw_response=raw_text,
                transcription=transcription,
                requested_model_key=model_key,
                pipeline_mode="direct_audio",
                actual_provider="mistral",
                actual_model=model_name,
                stt_provider=None,
                stt_model=None,
                analysis_provider="mistral",
                analysis_model=model_name,
                estimated_cost_usd=est_cost,
                pricing_status=p_status,
                latency_ms=latency_ms,
                usage_events=[ev_meta] if ev_meta else [],
            )

        except LaboratoryExecutionError:
            raise
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            await record_ai_usage_event(
                company_id=company_id,
                service_id=service_id,
                module=LAB_FINOPS_MODULE,
                operation=operation_name,
                provider="mistral",
                model=model_name,
                model_key=model_key,
                credential_alias="lab_mistral",
                usage=None,
                call_id=call_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=latency_ms,
                status="error",
                attempt_number=1,
            )
            raise _map_provider_exception(exc, provider="mistral", model=model_name, model_key=model_key, stage="llm")

    @classmethod
    async def _execute_stt_plus_llm(
        cls,
        *,
        entry: Dict[str, Any],
        prompt_content: str,
        audio_bytes: Optional[bytes],
        audio_format: str,
        transcription: Optional[str],
        call_id: Optional[str],
        company_id: Optional[int],
        service_id: Optional[int],
        audio_duration_seconds: Optional[float],
    ) -> LaboratoryExecutionResult:
        """
        2-Stage Pipeline:
          Stage A: Audio -> STT (Groq Whisper / Mistral Transcribe / Deepgram Nova-3) -> transcription text
          Stage B: Prompt + Transcription (NO audio) -> OpenAI GPT-5.6 Luna -> JSON evaluation
        Records TWO separate FinOps usage events (`operation="transcription"` and `operation="analysis"`).
        If STT fails, never calls LLM.
        """
        model_key = entry["model_key"]
        stt_provider = entry["stt_provider"]
        stt_model = entry["stt_model"]
        llm_provider = entry["llm_provider"]
        llm_model = entry["llm_model"]
        context_limit = entry.get("context_limit", 128000)

        stt_ev_meta: Optional[Dict[str, Any]] = None
        stt_latency_ms = 0
        resolved_transcription = transcription
        eff_duration = audio_duration_seconds

        # ── Stage 1: STT Transcription ───────────────────────────────────────
        if audio_bytes is not None:
            stt_text, stt_dur, stt_latency_ms, stt_ev_meta = await cls._run_stt_stage(
                stt_provider=stt_provider,
                stt_model=stt_model,
                model_key=model_key,
                audio_bytes=audio_bytes,
                audio_format=audio_format,
                call_id=call_id,
                company_id=company_id,
                service_id=service_id,
                audio_duration_seconds=audio_duration_seconds,
            )
            resolved_transcription = stt_text
            if stt_dur is not None:
                eff_duration = stt_dur

        if not resolved_transcription or not resolved_transcription.strip():
            raise LaboratoryExecutionError(
                stage="transcription",
                message=f"El motor STT ({stt_provider}/{stt_model}) devolvió una transcripción vacía.",
                status_code=400,
                provider=stt_provider,
                model=stt_model,
                model_key=model_key,
            )

        # Check combined context limit before calling LLM
        _check_context_limit(f"{prompt_content}\n\n{resolved_transcription}", context_limit, model_key)

        # ── Stage 2: OpenAI Luna Evaluation (Text Only — NEVER sends audio) ──
        raw_json_response, llm_latency_ms, llm_ev_meta = await cls._run_openai_luna_stage(
            llm_model=llm_model,
            model_key=model_key,
            prompt_content=prompt_content,
            transcription=resolved_transcription,
            call_id=call_id,
            company_id=company_id,
            service_id=service_id,
            audio_duration_seconds=eff_duration,
        )

        events_list = [e for e in (stt_ev_meta, llm_ev_meta) if e is not None]
        total_cost, p_status = _aggregate_costs([stt_ev_meta, llm_ev_meta])
        stt_cost = stt_ev_meta.get("estimated_cost_usd") if stt_ev_meta else None
        llm_cost = llm_ev_meta.get("estimated_cost_usd") if llm_ev_meta else None

        return LaboratoryExecutionResult(
            raw_response=raw_json_response,
            transcription=resolved_transcription,
            requested_model_key=model_key,
            pipeline_mode="stt_plus_llm",
            actual_provider=f"{stt_provider}+{llm_provider}",
            actual_model=f"{stt_model} + {llm_model}",
            stt_provider=stt_provider,
            stt_model=stt_model,
            analysis_provider=llm_provider,
            analysis_model=llm_model,
            estimated_cost_usd=total_cost,
            stt_cost_usd=stt_cost,
            llm_cost_usd=llm_cost,
            pricing_status=p_status,
            latency_ms=stt_latency_ms + llm_latency_ms,
            stt_latency_ms=stt_latency_ms,
            llm_latency_ms=llm_latency_ms,
            usage_events=events_list,
        )

    @classmethod
    async def _run_stt_stage(
        cls,
        *,
        stt_provider: str,
        stt_model: str,
        model_key: str,
        audio_bytes: bytes,
        audio_format: str,
        call_id: Optional[str],
        company_id: Optional[int],
        service_id: Optional[int],
        audio_duration_seconds: Optional[float],
    ) -> Tuple[str, Optional[float], int, Optional[Dict[str, Any]]]:
        s = get_settings()
        ext = "wav" if "wav" in audio_format.lower() else "mp3"
        mime = "audio/wav" if ext == "wav" else "audio/mpeg"
        filename = f"call.{ext}"

        logger.info(
            "[Laboratory] Calling STT stage: provider=%s model=%s model_key=%s call_id=%s",
            stt_provider, stt_model, model_key, call_id,
        )

        t_start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=float(s.gemini_timeout_seconds or 120)) as client:
                if stt_provider == "groq":
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {s.lab_groq_api_key.strip()}"},
                        files={"file": (filename, audio_bytes, mime)},
                        data={"model": stt_model, "response_format": "verbose_json"},
                    )
                elif stt_provider == "mistral":
                    resp = await client.post(
                        "https://api.mistral.ai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {s.lab_mistral_api_key.strip()}"},
                        files={"file": (filename, audio_bytes, mime)},
                        data={"model": stt_model},
                    )
                elif stt_provider == "deepgram":
                    resp = await client.post(
                        "https://api.deepgram.com/v1/listen",
                        params={"model": stt_model, "smart_format": "true", "punctuate": "true"},
                        headers={
                            "Authorization": f"Token {s.lab_deepgram_api_key.strip()}",
                            "Content-Type": mime,
                        },
                        content=audio_bytes,
                    )
                else:
                    raise LaboratoryExecutionError(
                        stage="transcription",
                        message=f"Proveedor STT '{stt_provider}' no soportado.",
                        status_code=400,
                        provider=stt_provider,
                        model=stt_model,
                        model_key=model_key,
                    )

            latency_ms = int((time.perf_counter() - t_start) * 1000)

            if resp.status_code != 200:
                await record_ai_usage_event(
                    company_id=company_id,
                    service_id=service_id,
                    module=LAB_FINOPS_MODULE,
                    operation="transcription",
                    provider=stt_provider,
                    model=stt_model,
                    model_key=model_key,
                    credential_alias=f"lab_{stt_provider}",
                    usage=None,
                    call_id=call_id,
                    audio_duration_seconds=audio_duration_seconds,
                    execution_latency_ms=latency_ms,
                    status="error",
                    attempt_number=1,
                )
                raise _map_http_execution_error(
                    resp.status_code,
                    resp.text,
                    provider=stt_provider,
                    model=stt_model,
                    model_key=model_key,
                    stage="transcription",
                )

            data = resp.json()
            detected_duration: Optional[float] = audio_duration_seconds
            raw_usage_dict: Dict[str, Any] = {}

            if stt_provider == "groq":
                text_out = data.get("text") or ""
                if data.get("duration") is not None:
                    detected_duration = float(data["duration"])
                raw_usage_dict = data.get("usage") or {}
            elif stt_provider == "mistral":
                text_out = data.get("text") or ""
                raw_usage_dict = data.get("usage") or {}
                if raw_usage_dict.get("prompt_audio_seconds") is not None:
                    detected_duration = float(raw_usage_dict["prompt_audio_seconds"])
            elif stt_provider == "deepgram":
                channels = data.get("results", {}).get("channels", [])
                alts = channels[0].get("alternatives", []) if channels else []
                text_out = alts[0].get("transcript", "") if alts else ""
                dg_dur = data.get("metadata", {}).get("duration")
                if dg_dur is not None:
                    detected_duration = float(dg_dur)

            usage = parse_usage_metadata(
                raw_usage_dict if raw_usage_dict else None,
                model=stt_model,
                model_key=model_key,
                credential_alias=f"lab_{stt_provider}",
                response_id=data.get("id") or data.get("metadata", {}).get("request_id"),
                execution_latency_ms=latency_ms,
                audio_duration_seconds=detected_duration,
                status="success",
                attempt_number=1,
                provider=stt_provider,
            )

            ev_meta = await record_ai_usage_event(
                company_id=company_id,
                service_id=service_id,
                module=LAB_FINOPS_MODULE,
                operation="transcription",
                provider=stt_provider,
                model=stt_model,
                model_key=model_key,
                credential_alias=f"lab_{stt_provider}",
                usage=usage,
                request_id=usage.response_id,
                call_id=call_id,
                audio_duration_seconds=detected_duration,
                execution_latency_ms=latency_ms,
                status="success",
                attempt_number=1,
            )

            return text_out, detected_duration, latency_ms, ev_meta

        except LaboratoryExecutionError:
            raise
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            await record_ai_usage_event(
                company_id=company_id,
                service_id=service_id,
                module=LAB_FINOPS_MODULE,
                operation="transcription",
                provider=stt_provider,
                model=stt_model,
                model_key=model_key,
                credential_alias=f"lab_{stt_provider}",
                usage=None,
                call_id=call_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=latency_ms,
                status="error",
                attempt_number=1,
            )
            raise _map_provider_exception(
                exc,
                provider=stt_provider,
                model=stt_model,
                model_key=model_key,
                stage="transcription",
            )

    @classmethod
    async def _run_openai_luna_stage(
        cls,
        *,
        llm_model: str,
        model_key: str,
        prompt_content: str,
        transcription: str,
        call_id: Optional[str],
        company_id: Optional[int],
        service_id: Optional[int],
        audio_duration_seconds: Optional[float],
    ) -> Tuple[str, int, Optional[Dict[str, Any]]]:
        """
        Execute LLM evaluation on OpenAI (`LAB_OPENAI_LUNA_MODEL`) using ONLY prompt + transcription text.
        Never sends audio bytes to Luna.
        """
        s = get_settings()
        api_key = s.lab_openai_api_key.strip()

        messages = [
            {
                "role": "system",
                "content": (
                    f"{prompt_content}\n\n"
                    "Devuelve exclusivamente JSON válido, sin markdown ni texto adicional."
                ),
            },
            {
                "role": "user",
                "content": f"Transcripción de la llamada:\n\n{transcription}",
            },
        ]

        payload = {
            "model": llm_model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        logger.info(
            "[Laboratory] Calling OpenAI LLM stage: model=%s model_key=%s call_id=%s",
            llm_model, model_key, call_id,
        )

        t_start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=float(s.gemini_timeout_seconds or 120)) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

            latency_ms = int((time.perf_counter() - t_start) * 1000)

            if resp.status_code != 200:
                await record_ai_usage_event(
                    company_id=company_id,
                    service_id=service_id,
                    module=LAB_FINOPS_MODULE,
                    operation="analysis",
                    provider="openai",
                    model=llm_model,
                    model_key=model_key,
                    credential_alias="lab_openai",
                    usage=None,
                    call_id=call_id,
                    audio_duration_seconds=audio_duration_seconds,
                    execution_latency_ms=latency_ms,
                    status="error",
                    attempt_number=1,
                )
                raise _map_http_execution_error(
                    resp.status_code,
                    resp.text,
                    provider="openai",
                    model=llm_model,
                    model_key=model_key,
                    stage="llm",
                )

            data = resp.json()
            raw_text = (
                data.get("choices", [{}])[0].get("message", {}).get("content") or ""
            )
            raw_usage = data.get("usage") or {}
            resp_id = data.get("id")

            usage = parse_usage_metadata(
                raw_usage,
                model=llm_model,
                model_key=model_key,
                credential_alias="lab_openai",
                response_id=resp_id,
                execution_latency_ms=latency_ms,
                audio_duration_seconds=audio_duration_seconds,
                status="success",
                attempt_number=1,
                provider="openai",
            )
            # Ensure input_text_tokens / output_text_tokens are populated for OpenAI text completions
            if usage.input_text_tokens is None and usage.prompt_token_count is not None:
                usage.input_text_tokens = usage.prompt_token_count
            if usage.output_text_tokens is None and usage.candidates_token_count is not None:
                usage.output_text_tokens = usage.candidates_token_count

            ev_meta = await record_ai_usage_event(
                company_id=company_id,
                service_id=service_id,
                module=LAB_FINOPS_MODULE,
                operation="analysis",
                provider="openai",
                model=llm_model,
                model_key=model_key,
                credential_alias="lab_openai",
                usage=usage,
                request_id=resp_id,
                call_id=call_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=latency_ms,
                status="success",
                attempt_number=1,
            )

            return raw_text, latency_ms, ev_meta

        except LaboratoryExecutionError:
            raise
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t_start) * 1000)
            await record_ai_usage_event(
                company_id=company_id,
                service_id=service_id,
                module=LAB_FINOPS_MODULE,
                operation="analysis",
                provider="openai",
                model=llm_model,
                model_key=model_key,
                credential_alias="lab_openai",
                usage=None,
                call_id=call_id,
                audio_duration_seconds=audio_duration_seconds,
                execution_latency_ms=latency_ms,
                status="error",
                attempt_number=1,
            )
            raise _map_provider_exception(
                exc,
                provider="openai",
                model=llm_model,
                model_key=model_key,
                stage="llm",
            )


def _map_http_execution_error(
    status_code: int,
    body_text: str,
    *,
    provider: str,
    model: str,
    model_key: str,
    stage: str,
) -> LaboratoryExecutionError:
    lower_body = (body_text or "").lower()
    if any(k in lower_body for k in ("context_length", "context length", "maximum context", "too many tokens", "prompt is too long")):
        return LaboratoryExecutionError(
            stage="context_limit",
            message=CONTEXT_LIMIT_ERROR_MESSAGE,
            status_code=422,
            provider=provider,
            model=model,
            model_key=model_key,
        )
    if status_code == 404 or "model_not_found" in lower_body or "does not exist" in lower_body:
        return LaboratoryExecutionError(
            stage="model_not_available",
            message=f"El modelo '{model}' ({provider}) no está disponible o no existe para la cuenta actual.",
            status_code=400,
            provider=provider,
            model=model,
            model_key=model_key,
        )
    if status_code in (401, 403) or "permission" in lower_body or "unauthorized" in lower_body or "invalid_api_key" in lower_body:
        return LaboratoryExecutionError(
            stage="model_unauthorized",
            message=f"La API key configurada para {provider} no tiene autorización o acceso al modelo '{model}'.",
            status_code=400,
            provider=provider,
            model=model,
            model_key=model_key,
        )
    stage_label = "transcripción STT" if stage == "transcription" else "evaluación LLM"
    return LaboratoryExecutionError(
        stage=stage,
        message=f"Fallo en {stage_label} ({provider}/{model}): HTTP {status_code}.",
        status_code=502,
        provider=provider,
        model=model,
        model_key=model_key,
    )


def _map_provider_exception(
    exc: Exception,
    *,
    provider: str,
    model: str,
    model_key: str,
    stage: str,
) -> LaboratoryExecutionError:
    err_str = str(exc)
    lower = err_str.lower()

    if any(k in lower for k in ("context", "maximum context", "too many tokens", "token limit")):
        return LaboratoryExecutionError(
            stage="context_limit",
            message=CONTEXT_LIMIT_ERROR_MESSAGE,
            status_code=422,
            provider=provider,
            model=model,
            model_key=model_key,
        )
    if any(k in lower for k in ("not found", "404", "model_not_found", "does not exist", "is not found")):
        return LaboratoryExecutionError(
            stage="model_not_available",
            message=f"El modelo '{model}' ({provider}) no está disponible o fue deprecado.",
            status_code=400,
            provider=provider,
            model=model,
            model_key=model_key,
        )
    if any(k in lower for k in ("permission_denied", "permission denied", "403", "401", "unauthorized", "api_key_invalid", "invalid api key")):
        return LaboratoryExecutionError(
            stage="model_unauthorized",
            message=f"La cuenta o API key de {provider} no tiene acceso autorizado al modelo '{model}'.",
            status_code=400,
            provider=provider,
            model=model,
            model_key=model_key,
        )
    if "unsupported" in lower or "audio" in lower and "not supported" in lower:
        return LaboratoryExecutionError(
            stage="unsupported_audio",
            message=f"El modelo '{model}' ({provider}) no soporta el formato o entrada de audio proporcionada.",
            status_code=400,
            provider=provider,
            model=model,
            model_key=model_key,
        )

    stage_label = "transcripción STT" if stage == "transcription" else "evaluación IA"
    return LaboratoryExecutionError(
        stage=stage,
        message=f"Fallo durante {stage_label} con {provider} ({model}): {err_str}",
        status_code=502,
        provider=provider,
        model=model,
        model_key=model_key,
    )
