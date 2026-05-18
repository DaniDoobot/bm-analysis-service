"""
Service for orchestrating the audio analysis pipeline (Phase 2.3).
Integrates HubSpot, Twilio, Azure OpenAI, and PostgreSQL persistence.
"""
import logging
import sys
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.analyses import AnalyzeAudioRequest
from app.services.analysis_persistence import save_analysis
from app.services.hubspot_service import HubSpotService
from app.services.openai_service import analyze_audio_bytes
from app.services.twilio_service import TwilioService
from app.utils.json_utils import safe_parse_json

logger = logging.getLogger(__name__)

_ALLOWED_TIPOS = frozenset(
    [
        "cita",
        "informacion_sin_cita",
        "confirmacion",
        "cancelacion",
        "reagendo",
        "falta_con_reagendo",
        "falta_sin_reagendo",
        "no_interesado",
        "no_apto",
        "otros",
    ]
)

MAX_AUDIO_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB


async def process_audio_analysis(db: AsyncSession, request: AnalyzeAudioRequest) -> dict[str, Any]:
    """
    Execute the audio analysis pipeline:
    1. Resolve HubSpot metadata (if needed).
    2. Download audio via Twilio.
    3. Validate size (< 20 MB).
    4. Resolve Prompt.
    5. Call Azure OpenAI (audio).
    6. Parse & Validate JSON.
    7. Persist.
    """
    call_id = request.call_id
    metadata = request.metadata or {}
    recording_url = request.recording_url
    audio_url = request.audio_url

    # Normalize url (recording_url vs audio_url)
    target_url = recording_url or audio_url

    hubspot_data = {}
    hubspot_url = None
    call_direction = metadata.get("call_direction")
    call_timestamp = metadata.get("call_timestamp")
    agente_telefonico = metadata.get("agente_telefonico")
    hubspot_owner_id = metadata.get("hubspot_owner_id")
    duracion_llamada = metadata.get("duracion_llamada")
    source = metadata.get("source", "hubspot")

    # ── 1. HubSpot Resolution ──────────────────────────────────────────────
    if not target_url:
        logger.info("No direct recording_url provided. Resolving via HubSpot for call_id=%s", call_id)
        try:
            hs_service = HubSpotService()
            hubspot_data = await hs_service.get_call(call_id)
            target_url = hubspot_data.get("recording_url")
            hubspot_url = hubspot_data.get("hubspot_url")
            call_direction = hubspot_data.get("call_direction") or call_direction
            call_timestamp = hubspot_data.get("call_timestamp") or call_timestamp
            hubspot_owner_id = hubspot_data.get("hubspot_owner_id") or hubspot_owner_id
            duracion_llamada = hubspot_data.get("call_duration") or duracion_llamada
            agente_telefonico = hubspot_data.get("agente_telefonico") or agente_telefonico

            if hubspot_owner_id and not metadata.get("agente_telefonico"):
                # Try to resolve agent name
                owner_name = await hs_service.get_owner_name(hubspot_owner_id)
                if owner_name:
                    agente_telefonico = owner_name

        except Exception as e:
            logger.error("Failed to fetch HubSpot data for call_id=%s: %s", call_id, e)
            return {
                "ok": False,
                "status": "error",
                "stage": "hubspot",
                "error_message": f"HubSpot resolution failed: {str(e)}",
            }
    else:
        logger.info("Using direct recording_url for call_id=%s", call_id)

    if not target_url:
        return {
            "ok": False,
            "status": "error",
            "stage": "validation",
            "error_message": "No recording_url could be resolved for this call.",
        }

    # ── 2 & 3. Audio Download & Size Validation ─────────────────────────────
    try:
        twilio_service = TwilioService()
        audio_bytes = await twilio_service.download_audio(target_url)
    except Exception as e:
        logger.error("Failed to download audio from %s: %s", target_url, e)
        return {
            "ok": False,
            "status": "error",
            "stage": "download_audio",
            "error_message": f"Audio download failed: {str(e)}",
        }

    audio_size = sys.getsizeof(audio_bytes)
    logger.info("Downloaded audio size: %.2f MB", audio_size / (1024 * 1024))

    if audio_size > MAX_AUDIO_SIZE_BYTES:
        return {
            "ok": False,
            "status": "error",
            "stage": "audio_validation",
            "error_message": "El audio supera el tamaño máximo permitido por Azure OpenAI (20 MB)",
        }

    # Guess format
    audio_format = "mp3"
    if target_url.endswith(".wav") or target_url.endswith(".WAV"):
        audio_format = "wav"

    # ── 4. Prompt Resolution ────────────────────────────────────────────────
    resolved_prompt_id = request.prompt_id
    resolved_version_id = request.prompt_version_id
    prompt_text = None

    if resolved_prompt_id:
        # Explicit prompt_id provided: fetch its current active version
        from app.services.prompts_service import _get_current_version
        v = await _get_current_version(db, resolved_prompt_id)
        if not v:
            return {
                "ok": False,
                "status": "error",
                "stage": "validation",
                "error_message": f"Prompt ID {resolved_prompt_id} not found or has no current version.",
            }
        resolved_version_id = v.id
        prompt_text = v.prompt
    else:
        # No explicit prompt_id -> default to active "audio" prompt
        from app.services.prompts_service import get_active_prompt
        active_prompt = await get_active_prompt(db, "audio")
        if not active_prompt:
            return {
                "ok": False,
                "status": "error",
                "stage": "prompt_resolution",
                "error_message": "No active audio prompt found.",
            }
        resolved_prompt_id = active_prompt.get("prompt_id")
        resolved_version_id = active_prompt.get("prompt_version_id") or active_prompt.get("current_version_id")
        prompt_text = active_prompt.get("prompt")

    if not prompt_text:
        return {
            "ok": False,
            "status": "error",
            "stage": "prompt_resolution",
            "error_message": "Resolved prompt has no content.",
        }

    logger.info("Resolved prompt_id=%s, version_id=%s for call_id=%s", resolved_prompt_id, resolved_version_id, call_id)

    # ── 5. Call Azure OpenAI Audio ──────────────────────────────────────────
    try:
        raw_response = await analyze_audio_bytes(
            audio_bytes=audio_bytes,
            prompt_text=prompt_text,
            audio_format=audio_format,
        )
    except ValueError as ve:
        logger.error("Azure config error: %s", ve)
        return {
            "ok": False,
            "status": "error",
            "stage": "azure_config",
            "error_message": str(ve),
        }
    except Exception as e:
        logger.error("Azure OpenAI audio error for call_id=%s: %s", call_id, e)
        return {
            "ok": False,
            "status": "error",
            "stage": "azure",
            "error_message": f"Azure OpenAI error: {str(e)}",
        }

    # ── 6. Parse & Validate ────────────────────────────────────────────────
    parsed = safe_parse_json(raw_response)
    if not parsed:
        logger.error("Failed to parse JSON for call_id=%s. Raw: %r", call_id, raw_response[:200])
        return {
            "ok": False,
            "status": "error",
            "stage": "parse",
            "error_message": "El modelo no devolvió un JSON válido.",
            "raw_response": raw_response[:500] if raw_response else None,
        }

    tipo_llamada = parsed.get("tipo_llamada")
    if tipo_llamada not in _ALLOWED_TIPOS:
        logger.warning(
            "tipo_llamada no permitido: %r (call_id=%s)", tipo_llamada, call_id
        )
        return {
            "ok": False,
            "status": "error",
            "stage": "validation",
            "error_message": f"tipo_llamada no permitido: {tipo_llamada!r}",
        }

    # ── 7. Persist ──────────────────────────────────────────────────────────
    call_metadata: dict[str, Any] = {
        "call_id": call_id,
        "hubspot_url": hubspot_url,
        "call_direction": call_direction,
        "call_timestamp": call_timestamp,
        "agente_telefonico": agente_telefonico,
        "hubspot_owner_id": hubspot_owner_id,
        "duracion_llamada": duracion_llamada,
        "source": source,
        "run_ts": metadata.get("run_ts"),
        "fecha_eval": metadata.get("fecha_eval"),
    }

    try:
        analysis_record = await save_analysis(
            db=db,
            analysis_type="audio",
            call_metadata=call_metadata,
            prompt_metadata={
                "prompt_id": resolved_prompt_id,
                "prompt_version_id": resolved_version_id,
            },
            model_metadata={
                "model_provider": "azure_openai",
                "model_name": "gpt-audio-1.5",  # We log this explicitly as per request, could also import from config
            },
            result_json=parsed,
            payload={
                "recording_url": target_url,
                "hubspot_data": hubspot_data if hubspot_data else None,
                "raw_response": raw_response,
            },
            transcription=None,  # Audio has no explicit transcription
        )
    except Exception as e:
        logger.error("Error saving audio analysis to DB for call_id=%s: %s", call_id, e, exc_info=True)
        return {
            "ok": False,
            "status": "error",
            "stage": "save_analysis",
            "error_message": f"DB save error: {str(e)}",
        }

    # ── 8. Build Response ───────────────────────────────────────────────────
    summary = {
        "tipo_llamada": parsed.get("tipo_llamada"),
        "evaluacion_global": parsed.get("evaluacion_global"),
        "sentiment": parsed.get("sentiment"),
        "empatia": parsed.get("empatia"),
        "simpatia": parsed.get("simpatia"),
        "claridad": parsed.get("claridad"),
        "procedimiento": parsed.get("procedimiento"),
        "duracion_llamada": duracion_llamada,
        "agente_telefonico": agente_telefonico or parsed.get("agente_telefonico"),
        "objeciones": parsed.get("objeciones"),
        "propension": parsed.get("propension"),
    }

    logger.info("Completed audio analysis for call_id=%s (analysis_id=%s)", call_id, analysis_record.analysis_id)

    return {
        "ok": True,
        "status": "completed",
        "message": "Análisis de audio completado y guardado en base de datos",
        "call_id": call_id,
        "analysis_id": analysis_record.analysis_id,
        "summary": summary,
        "result": parsed,
    }
