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
        "falta",
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

    call_timestamp_source = "null"
    if call_timestamp:
        call_timestamp_source = "request_metadata"

    # ── 1. HubSpot Resolution ──────────────────────────────────────────────
    if not target_url:
        logger.info("No direct recording_url provided. Resolving via HubSpot for call_id=%s", call_id)
        try:
            hs_service = HubSpotService()
            hubspot_data = await hs_service.get_call(call_id)
            target_url = hubspot_data.get("recording_url")
            hubspot_url = hubspot_data.get("hubspot_url")
            call_direction = hubspot_data.get("call_direction") or call_direction
            
            # Prioritized HubSpot timestamps
            hs_timestamp = hubspot_data.get("hs_timestamp")
            hs_createdate = hubspot_data.get("hs_createdate")
            if hs_timestamp:
                call_timestamp = hs_timestamp
                call_timestamp_source = "hubspot_hs_timestamp"
            elif hs_createdate:
                call_timestamp = hs_createdate
                call_timestamp_source = "hubspot_hs_createdate"
            else:
                call_timestamp = hubspot_data.get("call_timestamp") or call_timestamp
                if call_timestamp and call_timestamp_source == "null":
                    call_timestamp_source = "hubspot_call_timestamp"
            
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

    # ── 1.5. Twilio Fallback Resolution ────────────────────────────────────
    if not call_timestamp and target_url:
        twilio_service = TwilioService()
        if twilio_service.is_twilio_url(target_url) and twilio_service.account_sid and twilio_service.auth_token:
            logger.info("Checking Twilio recording metadata fallback for call_id=%s (url=%s)", call_id, target_url)
            try:
                tw_meta = await twilio_service.get_recording_metadata(target_url)
                if tw_meta:
                    tw_date = tw_meta.get("date_created")
                    if tw_date:
                        call_timestamp = tw_date
                        call_timestamp_source = "twilio_recording_date_created"
            except Exception as e:
                logger.warning("Twilio fallback failed for call_id=%s: %s", call_id, e)

    # ── 1.8. Log resolved call timestamp ──
    logger.info("Call timestamp resolution: call_timestamp_source=%s, call_timestamp=%s", call_timestamp_source, call_timestamp)

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

    # Self-heal/Sync the prompt text with active criteria before validating or analyzing
    try:
        from app.services.prompts_service import sync_prompt_text_with_active_criteria, PromptValidationError
        from app.models.prompts import PromptVersion
        
        prompt_text_healed, changed = await sync_prompt_text_with_active_criteria(db, resolved_prompt_id, prompt_text)
        if changed:
            prompt_text = prompt_text_healed
            v_obj = await db.get(PromptVersion, resolved_version_id)
            if v_obj:
                from sqlalchemy import func
                v_obj.prompt = prompt_text
                v_obj.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                db.add(v_obj)
                await db.commit()
                logger.info("Self-healed prompt version ID %s in audio analysis pipeline.", resolved_version_id)
    except PromptValidationError as val_ex:
        logger.error("Prompt validation failed in audio analysis pipeline: %s", val_ex)
        return {
            "ok": False,
            "status": "error",
            "stage": "prompt_validation",
            "error_message": f"Prompt validation failed: {str(val_ex)}",
        }
    except Exception as ex:
        logger.error("Error during prompt self-healing in audio analysis pipeline: %s", ex, exc_info=True)

    # ── 4.2. Validate Active Criteria are in prompt_text ──────────────
    from app.services.criteria_service import get_active_criteria
    active_criteria = await get_active_criteria(db, resolved_prompt_id)
    missing_keys = []
    for c in active_criteria:
        if c.output_key and c.output_key not in prompt_text:
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

    # ── 6.5. Defensive Keys Guard ──────────────────────────────────────────
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
                matches = list(header_pattern.finditer(prompt_text or ""))
                format_section = (prompt_text or "")[matches[-1].start():] if matches else ""
                
                missing_result_keys = []
                for c in active_criteria_objs:
                    # Check output_key
                    if c.output_key:
                        if c.output_key not in parsed:
                            parsed[c.output_key] = None
                            missing_result_keys.append(c.output_key)
                            
                            # Log structured warning
                            in_text = "true" if c.output_key in (prompt_text or "") else "false"
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
                            in_text = "true" if c.feed_key in (prompt_text or "") else "false"
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
