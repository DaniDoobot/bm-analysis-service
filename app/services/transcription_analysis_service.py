"""
Transcription analysis service.
Handles analyzing an existing transcription using the active text prompt.
"""
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services import openai_service
from app.services.analysis_persistence import save_analysis
from app.services.prompts_service import get_active_prompt
from app.utils.json_utils import safe_parse_json

logger = logging.getLogger(__name__)
settings = get_settings()


async def analyze_transcription_pipeline(
    db: AsyncSession,
    call_id: str,
    transcription: str,
    analysis_type: str = "text",
    prompt_id: int | None = None,
    prompt_version_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Core pipeline to analyze an existing transcription.
    """
    if not call_id:
        return {"ok": False, "status": "error", "error_message": "call_id is required."}
    if not transcription:
        return {"ok": False, "status": "error", "error_message": "transcription is required."}

    # 1. Resolve Prompt
    resolved_prompt_id = prompt_id
    resolved_version_id = prompt_version_id
    prompt_content = None

    if not resolved_prompt_id or not resolved_version_id:
        # Fallback 1: Active "audio" prompt (preferred as it has all categories)
        active_prompt = await get_active_prompt(db, "audio")
        if not active_prompt:
            # Fallback 2: Active "text" prompt
            logger.info("No active audio prompt found, falling back to text prompt for transcription analysis.")
            active_prompt = await get_active_prompt(db, "text")

        if not active_prompt:
            return {"ok": False, "status": "error", "error_message": "No active prompt found for text or audio."}
        
        resolved_prompt_id = active_prompt.get("prompt_id")
        resolved_version_id = (
            active_prompt.get("prompt_version_id")
            or active_prompt.get("active_version_id")
            or active_prompt.get("current_version_id")
            or active_prompt.get("version_id")
            or active_prompt.get("id")
        )
        prompt_content = (
            active_prompt.get("prompt")
            or active_prompt.get("prompt_content")
            or active_prompt.get("content")
        )
        
        if not resolved_version_id or not prompt_content:
            keys_avail = list(active_prompt.keys())
            return {
                "ok": False, 
                "status": "error", 
                "error_message": f"No se pudo resolver la versión activa del prompt. Claves disponibles: {keys_avail}"
            }
    else:
        # We need to fetch the content if IDs were explicitly provided. 
        # But for now, we assume if they are provided, we should fetch them. Let's do a simple query.
        # To avoid extra queries if the user doesn't strictly need it, we'll fetch via _get_current_version.
        from app.services.prompts_service import _get_current_version
        v = await _get_current_version(db, resolved_prompt_id)
        if not v:
            return {"ok": False, "status": "error", "error_message": f"Prompt ID {resolved_prompt_id} not found."}
        resolved_version_id = v.id
        prompt_content = v.prompt

    if not prompt_content:
        return {"ok": False, "status": "error", "error_message": "Resolved prompt has no content."}

    # 2. Call Azure OpenAI
    messages = [
        {
            "role": "system",
            "content": f"{prompt_content}\n\nDevuelve exclusivamente JSON válido, sin markdown ni texto adicional."
        },
        {
            "role": "user",
            "content": f"Transcripción de la llamada:\n\n{transcription}"
        }
    ]

    try:
        raw_response = await openai_service.complete_text(
            messages=messages,
            response_format="json_object",
            model=settings.azure_openai_text_deployment
        )
    except Exception as e:
        logger.error("Error calling Azure OpenAI: %s", e, exc_info=True)
        return {"ok": False, "status": "error", "error_message": f"Azure OpenAI error: {str(e)}"}

    # 3. Parse JSON
    parsed = safe_parse_json(raw_response)
    if not parsed or not isinstance(parsed, dict):
        logger.error("AI returned non-JSON response: %s", raw_response[:500])
        return {
            "ok": False,
            "status": "error",
            "error_message": "El modelo no devolvió un JSON válido.",
            "stage": "json_parsing",
            "raw_response": raw_response
        }

    # Validate tipo_llamada
    allowed_tipos = [
        "cita", "informacion_sin_cita", "confirmacion", "cancelacion", 
        "reagendo", "falta_con_reagendo", "falta_sin_reagendo", 
        "no_interesado", "no_apto", "otros"
    ]
    tipo_llamada = parsed.get("tipo_llamada")
    if tipo_llamada not in allowed_tipos:
        return {
            "ok": False,
            "status": "error",
            "error_message": f"tipo_llamada no permitido: {tipo_llamada}",
            "stage": "validation",
            "raw_response": raw_response
        }

    # 4. Persistence
    call_metadata = metadata or {}
    call_metadata["call_id"] = call_id

    try:
        analysis_record = await save_analysis(
            db=db,
            analysis_type=analysis_type,
            call_metadata=call_metadata,
            prompt_metadata={
                "prompt_id": resolved_prompt_id,
                "prompt_version_id": resolved_version_id,
            },
            model_metadata={
                "model_provider": "azure_openai",
                "model_name": settings.azure_openai_text_deployment,
            },
            result_json=parsed,
            payload={
                "transcription": transcription,
                "raw_response": raw_response
            },
            transcription=transcription
        )
    except Exception as e:
        logger.error("Error saving analysis to DB: %s", e, exc_info=True)
        return {
            "ok": False, 
            "status": "error", 
            "error_message": f"DB Save error: {str(e)}",
            "stage": "save_analysis"
        }

    # 5. Build response
    summary = {
        "tipo_llamada": parsed.get("tipo_llamada"),
        "evaluacion_global": parsed.get("evaluacion_global"),
        "sentiment": parsed.get("sentiment"),
        "empatia": parsed.get("empatia"),
        "simpatia": parsed.get("simpatia"),
        "claridad": parsed.get("claridad"),
        "procedimiento": parsed.get("procedimiento"),
        "agente_telefonico": call_metadata.get("agente_telefonico") or parsed.get("agente_telefonico"),
        "objeciones": parsed.get("objeciones"),
        "propension": parsed.get("propension"),
    }

    return {
        "ok": True,
        "status": "completed",
        "message": "Análisis por transcripción completado y guardado en base de datos",
        "call_id": call_id,
        "analysis_id": analysis_record.analysis_id,
        "summary": summary,
        "result": parsed
    }
