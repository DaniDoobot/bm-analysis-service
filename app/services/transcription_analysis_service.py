"""
Transcription analysis service.
Handles analyzing an existing transcription using the active prompt.

Prompt resolution rules:
  - If prompt_id is provided → use it directly.
  - If prompt_id is NOT provided → use the active prompt of type="audio" (default).
    The audio prompt is the canonical one with the 45 active criteria and correct categories.
    Fallback to type="text" only if no audio prompt exists.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services import openai_service
from app.services.analysis_persistence import save_analysis
from app.services.prompts_service import get_active_prompt
from app.utils.json_utils import safe_parse_json

logger = logging.getLogger(__name__)
settings = get_settings()

# Exhaustive list of allowed tipo_llamada values
_ALLOWED_TIPOS: frozenset[str] = frozenset({
    "cita",
    "informacion_sin_cita",
    "confirmacion",
    "cancelacion",
    "reagendo",
    "falta",
    "falta_con_reagendo",
    "falta_sin_reagendo",
    "no_interesado",
    "no_apto",
    "otros",
})


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

    Returns a structured dict with ok/status/stage on every code path —
    never raises an unhandled exception.
    """
    if not call_id:
        return {
            "ok": False,
            "status": "error",
            "stage": "validation",
            "error_message": "call_id is required.",
        }
    if not transcription:
        return {
            "ok": False,
            "status": "error",
            "stage": "validation",
            "error_message": "transcription is required.",
        }

    now = datetime.now(timezone.utc)

    # ── 1. Resolve Prompt ──────────────────────────────────────────────────
    resolved_prompt_id = prompt_id
    resolved_version_id = prompt_version_id
    prompt_content: str | None = None

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
        prompt_content = v.prompt

    else:
        # No explicit prompt_id → default to the active "audio" prompt.
        # This is the canonical prompt for Boston Medical (45 criteria, correct categories).
        active_prompt = await get_active_prompt(db, "audio")

        if not active_prompt:
            # Only fall back to "text" if there is genuinely no audio prompt
            logger.info(
                "No active audio prompt found; falling back to text prompt for transcription analysis."
            )
            active_prompt = await get_active_prompt(db, "text")

        if not active_prompt:
            return {
                "ok": False,
                "status": "error",
                "stage": "validation",
                "error_message": "No active prompt found (tried audio, then text).",
            }

        resolved_prompt_id = active_prompt.get("prompt_id")
        resolved_version_id = (
            active_prompt.get("prompt_version_id")
            or active_prompt.get("current_version_id")
        )
        prompt_content = active_prompt.get("prompt")

        if not resolved_version_id or not prompt_content:
            return {
                "ok": False,
                "status": "error",
                "stage": "validation",
                "error_message": (
                    f"Could not resolve active prompt version. "
                    f"Available keys: {list(active_prompt.keys())}"
                ),
            }

    if not prompt_content:
        return {
            "ok": False,
            "status": "error",
            "stage": "validation",
            "error_message": "Resolved prompt has no content.",
        }

    # Self-heal/Sync the prompt content with active criteria before validating or analyzing
    try:
        from app.services.prompts_service import sync_prompt_text_with_active_criteria, PromptValidationError
        from app.models.prompts import PromptVersion
        
        prompt_content_healed, changed = await sync_prompt_text_with_active_criteria(db, resolved_prompt_id, prompt_content)
        if changed:
            prompt_content = prompt_content_healed
            v_obj = await db.get(PromptVersion, resolved_version_id)
            if v_obj:
                from sqlalchemy import func
                v_obj.prompt = prompt_content
                v_obj.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                db.add(v_obj)
                await db.commit()
                logger.info("Self-healed prompt version ID %s in transcription analysis pipeline.", resolved_version_id)
    except PromptValidationError as val_ex:
        logger.error("Prompt validation failed: %s", val_ex)
        return {
            "ok": False,
            "status": "error",
            "stage": "prompt_validation",
            "error_message": f"Prompt validation failed: {str(val_ex)}",
        }
    except Exception as ex:
        logger.error("Error during prompt self-healing in transcription analysis pipeline: %s", ex, exc_info=True)

    # ── 1.2. Validate Active Criteria are in prompt_content ────────────
    from app.services.criteria_service import get_active_criteria
    active_criteria = await get_active_criteria(db, resolved_prompt_id)
    missing_keys = []
    for c in active_criteria:
        if c.output_key and c.output_key not in prompt_content:
            missing_keys.append(c.output_key)

    if missing_keys:
        return {
            "ok": False,
            "status": "error",
            "stage": "validation",
            "error_message": "Hay criterios activos que no están incluidos en el prompt activo. Regenera y activa una nueva versión del prompt.",
            "details": {
                "missing_keys": missing_keys
            }
        }


    # ── 2. Call Azure OpenAI ───────────────────────────────────────────────
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

    try:
        raw_response = await openai_service.complete_text(
            messages=messages,
            response_format="json_object",
            model=settings.azure_openai_text_deployment,
        )
    except Exception as e:
        logger.error("Error calling Azure OpenAI: %s", e, exc_info=True)
        return {
            "ok": False,
            "status": "error",
            "stage": "azure",
            "error_message": f"Azure OpenAI error: {str(e)}",
        }

    # ── 3. Parse JSON ──────────────────────────────────────────────────────
    parsed = safe_parse_json(raw_response)
    if not parsed or not isinstance(parsed, dict):
        logger.error("AI returned non-JSON response: %s", raw_response[:500])
        return {
            "ok": False,
            "status": "error",
            "stage": "parse",
            "error_message": "El modelo no devolvió un JSON válido.",
            "details": raw_response[:500] if raw_response else None,
        }

    # ── 4. Validate AI output ──────────────────────────────────────────────
    tipo_llamada = parsed.get("tipo_llamada")
    
    # Dynamically fetch active typologies for the prompt's service to avoid validation issues
    try:
        from sqlalchemy import select
        from app.models.prompts import Prompt
        from app.models.typologies import Typology
        
        prompt_obj = await db.get(Prompt, resolved_prompt_id)
        if prompt_obj and prompt_obj.service_id:
            t_stmt = select(Typology.typology_key).where(
                Typology.service_id == prompt_obj.service_id,
                Typology.is_active == True
            )
            t_res = await db.execute(t_stmt)
            active_keys = set(t_res.scalars().all())
        else:
            t_stmt = select(Typology.typology_key).where(Typology.is_active == True)
            t_res = await db.execute(t_stmt)
            active_keys = set(t_res.scalars().all())
    except Exception as e:
        logger.warning("Failed to fetch active typologies from DB for dynamic validation: %s", e)
        active_keys = set(["cita", "confirmacion", "cancelacion", "reagendo", "falta", "otros"])

    if not active_keys:
        active_keys = set(["cita", "confirmacion", "cancelacion", "reagendo", "falta", "otros"])
        
    # Map legacy or inactive typology to "otros" to prevent pipeline crash
    if tipo_llamada not in active_keys:
        logger.warning(
            "tipo_llamada %r is not active or legacy (allowed: %s). Mapping to 'otros' for call_id=%s, prompt_id=%s",
            tipo_llamada,
            sorted(active_keys),
            call_id,
            resolved_prompt_id
        )
        tipo_llamada = "otros"
        parsed["tipo_llamada"] = "otros"

    # ── 4.5. Defensive Keys Guard ──────────────────────────────────────────
    try:
        if parsed and isinstance(parsed, dict):
            from app.services.criteria_service import get_active_criteria
            from app.models.criteria import PromptCriterionTypology
            from app.models.typologies import Typology
            from sqlalchemy import select
            
            active_criteria_objs = await get_active_criteria(db, resolved_prompt_id)
            if active_criteria_objs:
                # Fetch criterion-typology mappings in batch
                criteria_ids = [c.criterion_id for c in active_criteria_objs]
                criterion_typologies_map = {c_id: [] for c_id in criteria_ids}
                
                assoc_res = await db.execute(
                    select(PromptCriterionTypology.criterion_id, Typology.typology_key)
                    .join(Typology, PromptCriterionTypology.typology_id == Typology.typology_id)
                    .where(PromptCriterionTypology.criterion_id.in_(criteria_ids), Typology.is_active == True)
                )
                for c_id, t_key in assoc_res.all():
                    if c_id in criterion_typologies_map:
                        criterion_typologies_map[c_id].append(t_key)

                # Determine the format section text to check if the key is in output format
                import re
                header_pattern = re.compile(
                    r"^(?:###?\s+)?(?:FORMATO\s+DE\s+(?:RESPUESTA|SALIDA(?:\s+JSON)?))\b",
                    re.IGNORECASE | re.MULTILINE
                )
                matches = list(header_pattern.finditer(prompt_content or ""))
                format_section = (prompt_content or "")[matches[-1].start():] if matches else ""
                
                missing_result_keys = []
                for c in active_criteria_objs:
                    # Check output_key
                    if c.output_key:
                        if c.output_key not in parsed:
                            parsed[c.output_key] = None
                            missing_result_keys.append(c.output_key)
                            
                            # Log structured warning
                            in_text = "true" if c.output_key in (prompt_content or "") else "false"
                            in_format = "true" if c.output_key in format_section else "false"
                            associated_typos = criterion_typologies_map.get(c.criterion_id) or []
                            
                            logger.warning(
                                "Missing expected key %s. Present in active criteria: true. "
                                "Present in prompt text: %s. Present in output format: %s. "
                                "prompt_version_id=%s. criterion_id=%s. criterion_name='%s'. "
                                "typologies=%s. call_id=%s",
                                c.output_key,
                                in_text,
                                in_format,
                                resolved_version_id,
                                c.criterion_id,
                                c.criterion_name,
                                associated_typos,
                                call_id
                            )
                            
                    # Check feed_key
                    if c.feed_key:
                        if c.feed_key not in parsed:
                            parsed[c.feed_key] = None
                            missing_result_keys.append(c.feed_key)
                            
                            # Log structured warning
                            in_text = "true" if c.feed_key in (prompt_content or "") else "false"
                            in_format = "true" if c.feed_key in format_section else "false"
                            associated_typos = criterion_typologies_map.get(c.criterion_id) or []
                            
                            logger.warning(
                                "Missing expected key %s. Present in active criteria: true. "
                                "Present in prompt text: %s. Present in output format: %s. "
                                "prompt_version_id=%s. criterion_id=%s. criterion_name='%s'. "
                                "typologies=%s. call_id=%s",
                                c.feed_key,
                                in_text,
                                in_format,
                                resolved_version_id,
                                c.criterion_id,
                                c.criterion_name,
                                associated_typos,
                                call_id
                            )

                if missing_result_keys:
                    logger.info(
                        "Defensive Keys Guard: Injected missing keys in analysis result JSON: %s for call_id=%s, prompt_id=%s",
                        missing_result_keys,
                        call_id,
                        resolved_prompt_id
                    )
    except Exception as ex:
        logger.error("Error in Defensive Keys Guard for call_id=%s: %s", call_id, ex, exc_info=True)

    # ── 5. Persist ────────────────────────────────────────────────────────
    call_metadata: dict[str, Any] = dict(metadata or {})
    call_metadata["call_id"] = call_id
    # Ensure run_ts is always a timezone-aware datetime, never a string
    call_metadata.setdefault("run_ts", now)

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
                "raw_response": raw_response,
            },
            transcription=transcription,
        )
    except Exception as e:
        logger.error("Error saving analysis to DB: %s", e, exc_info=True)
        return {
            "ok": False,
            "status": "error",
            "stage": "save_analysis",
            "error_message": f"DB save error: {str(e)}",
        }

    # ── 6. Build response ─────────────────────────────────────────────────
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
        "result": parsed,
    }
