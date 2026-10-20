"""
Transcription analysis service.
Handles analyzing an existing transcription or audio using the active prompt.

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


def _extract_hubspot_duration_seconds(hubspot_data: dict[str, Any] | None) -> float | None:
    """Extract call duration in seconds from HubSpot call metadata if present."""
    if not isinstance(hubspot_data, dict):
        return None
    if hubspot_data.get("duration_seconds") is not None:
        try:
            return float(hubspot_data["duration_seconds"])
        except (ValueError, TypeError):
            pass
    raw_dur = hubspot_data.get("duration") or hubspot_data.get("hs_call_duration")
    if raw_dur is not None:
        try:
            val = float(raw_dur)
            # HubSpot hs_call_duration is typically in milliseconds if > 10000, or seconds if smaller
            return round(val / 1000.0, 2) if val > 7200 else round(val, 2)
        except (ValueError, TypeError):
            pass
    return None


async def analyze_transcription_pipeline(
    db: AsyncSession,
    call_id: str,
    transcription: str | None = None,
    analysis_type: str = "text",
    prompt_id: int | None = None,
    prompt_version_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    custom_prompt_text: str | None = None,
    service_id: int | None = None,
    model_key: str | None = None,
    audio_bytes: bytes | None = None,
    audio_format: str = "mp3",
) -> dict[str, Any]:
    """
    Core pipeline to analyze an existing transcription or Laboratory audio.

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

    clean_model_key = model_key.strip() if isinstance(model_key, str) and model_key.strip() else None

    # Early catalog validation if model_key is provided
    if clean_model_key:
        from app.services.laboratory_ai_service import get_lab_model_catalog
        catalog = get_lab_model_catalog()
        if clean_model_key not in catalog:
            return {
                "ok": False,
                "status": "error",
                "stage": "model_validation",
                "error_message": f"Modelo '{clean_model_key}' desconocido. Selecciona un modelo válido del catálogo de Laboratorio.",
            }

    now = datetime.now(timezone.utc)

    # ── 1. Resolve Prompt & Scope FIRST ──────────────────────────────────
    resolved_prompt_id = prompt_id
    resolved_version_id = prompt_version_id
    resolved_service_id = service_id
    resolved_company_id = (metadata.get("company_id") if isinstance(metadata, dict) else None)
    prompt_content: str | None = None

    logger.info(
        "Test analysis request: call_id=%s, service_id=%s, prompt_id=%s, company_id=%s, model_key=%s",
        call_id,
        resolved_service_id,
        resolved_prompt_id,
        resolved_company_id,
        clean_model_key,
    )

    if custom_prompt_text:
        # If custom prompt is passed explicitly, use it directly
        prompt_content = custom_prompt_text
        resolved_prompt_id = None
        resolved_version_id = None

    elif resolved_prompt_id:
        # Explicit prompt_id provided: load Prompt ORM and validate status & service
        from app.models.prompts import Prompt
        from app.services.prompts_service import _get_current_version

        p_obj = await db.get(Prompt, resolved_prompt_id)
        if not p_obj or p_obj.deleted_at is not None or p_obj.is_archived:
            return {
                "ok": False,
                "status": "error",
                "stage": "validation",
                "error_message": f"La estructura con ID {resolved_prompt_id} no existe o se encuentra archivada/eliminada.",
            }

        if p_obj.service_id is not None:
            if resolved_service_id is not None and resolved_service_id != p_obj.service_id:
                return {
                    "ok": False,
                    "status": "error",
                    "stage": "validation",
                    "error_message": (
                        f"La estructura con ID {resolved_prompt_id} pertenece al servicio {p_obj.service_id}, "
                        f"pero se seleccionó el servicio {resolved_service_id}."
                    ),
                }
            resolved_service_id = p_obj.service_id

        if p_obj.company_id is not None:
            resolved_company_id = p_obj.company_id

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

    elif resolved_service_id:
        # No explicit prompt_id → resolve active prompt for the requested service_id
        active_prompt = await get_active_prompt(db, "audio", service_id=resolved_service_id, company_id=resolved_company_id)

        if not active_prompt:
            # DO NOT fall back to Front or global prompt if a specific service_id was requested
            return {
                "ok": False,
                "status": "error",
                "stage": "validation",
                "error_message": (
                    f"No hay estructura activa para llamadas en el servicio seleccionado "
                    f"(service_id={resolved_service_id}, type=audio). "
                    f"Activa una estructura en ese servicio antes de analizar."
                ),
            }

        resolved_prompt_id = active_prompt.get("prompt_id")
        resolved_version_id = (
            active_prompt.get("prompt_version_id")
            or active_prompt.get("current_version_id")
        )
        prompt_content = active_prompt.get("prompt")
        if active_prompt.get("company_id"):
            resolved_company_id = active_prompt.get("company_id")

    else:
        # No prompt_id AND no service_id → global fallback
        active_prompt = await get_active_prompt(db, "audio")
        if not active_prompt:
            logger.info("No active audio prompt found; falling back to text prompt for transcription analysis.")
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
        resolved_service_id = active_prompt.get("service_id")
        resolved_company_id = active_prompt.get("company_id")

    if not prompt_content or (not custom_prompt_text and not resolved_version_id):
        return {
            "ok": False,
            "status": "error",
            "stage": "validation",
            "error_message": (
                f"Could not resolve active prompt version. "
                f"Available keys: {list(active_prompt.keys()) if 'active_prompt' in locals() and active_prompt else []}"
            ),
        }

    # Resolve company_id and demo status for credentials & HubSpot
    from app.core.side_effects import resolve_company_demo_status
    resolved_cid, resolved_is_demo = await resolve_company_demo_status(
        db,
        company_id=resolved_company_id,
        service_id=resolved_service_id,
    )
    if resolved_cid is not None and resolved_company_id is None:
        resolved_company_id = resolved_cid

    # If model_key is provided, validate required credentials BEFORE downloading audio or calling AI
    if clean_model_key:
        from app.services.laboratory_ai_service import validate_model_configuration
        val_cfg = validate_model_configuration(
            clean_model_key,
            company_id=resolved_company_id,
            service_id=resolved_service_id,
        )
        if not val_cfg["valid"]:
            return {
                "ok": False,
                "status": "error",
                "stage": "model_validation",
                "error_message": val_cfg["detail"],
            }
        if not val_cfg["configured"]:
            return {
                "ok": False,
                "status": "error",
                "stage": "model_config",
                "error_message": val_cfg["detail"],
                "details": {"missing_vars": val_cfg["missing_vars"]},
            }

    # ── 1.1. Self-heal/Sync the prompt content with active criteria ──────
    is_fallback_active = False
    if resolved_prompt_id and prompt_content:
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
            logger.warning(
                "Prompt validation failed during transcription analysis self-healing: %s. "
                "Falling back to the last valid prompt version content currently stored.",
                val_ex
            )
            is_fallback_active = True
        except Exception as ex:
            logger.error("Error during prompt self-healing in transcription analysis pipeline: %s", ex, exc_info=True)

    # ── 1.2. Validate Active Criteria are in prompt_content ────────────
    if resolved_prompt_id:
        from app.services.criteria_service import get_active_criteria
        active_criteria = await get_active_criteria(db, resolved_prompt_id)
        missing_keys = []
        for c in active_criteria:
            if c.output_key and c.output_key not in prompt_content:
                missing_keys.append(c.output_key)

        if missing_keys and not is_fallback_active:
            return {
                "ok": False,
                "status": "error",
                "stage": "validation",
                "error_message": "Hay criterios activos que no están incluidos en el prompt activo. Regenera y activa una nueva versión del prompt.",
                "details": {
                    "missing_keys": missing_keys
                }
            }

    # ── 2. Audio Resolution & AI Execution ───────────────────────────────
    lab_exec_result = None
    hubspot_data: dict[str, Any] | None = None

    if clean_model_key:
        # ── 2A. Laboratory Selectable Model Pipeline (Isolated) ──────────
        from app.services.laboratory_ai_service import (
            LaboratoryAIProviderService,
            LaboratoryExecutionError,
        )

        audio_duration_seconds: float | None = None

        if audio_bytes is None and not transcription:
            logger.info(
                "[Laboratory] Resolving and downloading audio for call_id=%s (model_key=%s, is_demo=%s)",
                call_id, clean_model_key, resolved_is_demo,
            )
            from app.services.hubspot_service import HubSpotService
            from app.services.audio_download_service import (
                AudioDownloadService,
                AudioDownloadError,
                detect_audio_format,
            )

            try:
                hs_service = HubSpotService(is_demo=resolved_is_demo)
                hubspot_data = await hs_service.get_call(call_id, is_demo=resolved_is_demo)
                target_url = hubspot_data.get("recording_url")
                cloudtalk_id = hubspot_data.get("cloudtalk_id")
                if not target_url and not cloudtalk_id:
                    return {
                        "ok": False,
                        "status": "error",
                        "stage": "hubspot",
                        "error_message": "Call resolved via HubSpot has no recording URL.",
                    }
                audio_duration_seconds = _extract_hubspot_duration_seconds(hubspot_data)
            except Exception as e:
                logger.error("Failed to fetch HubSpot data for call_id=%s: %s", call_id, e)
                return {
                    "ok": False,
                    "status": "error",
                    "stage": "hubspot",
                    "error_message": f"HubSpot resolution failed: {str(e)}",
                }

            try:
                downloader = AudioDownloadService()
                if cloudtalk_id:
                    audio_bytes = await downloader.download_audio(target_url, cloudtalk_id=cloudtalk_id)
                else:
                    audio_bytes = await downloader.download_audio(target_url)
                if not audio_format or audio_format == "mp3":
                    audio_format = detect_audio_format(audio_bytes, target_url)
            except AudioDownloadError as e:
                logger.error("Audio download error for call_id=%s (source=%s): %s", call_id, e.stage, e.message)
                return {
                    "ok": False,
                    "status": "error",
                    "stage": "download_audio",
                    "error_message": f"Audio download failed: {e.message}",
                }
            except Exception as e:
                logger.error("Failed to download audio for call_id=%s: %s", call_id, e)
                return {
                    "ok": False,
                    "status": "error",
                    "stage": "download_audio",
                    "error_message": f"Audio download failed: {str(e)}",
                }

        try:
            lab_exec_result = await LaboratoryAIProviderService.execute_laboratory_analysis(
                model_key=clean_model_key,
                prompt_content=prompt_content,
                audio_bytes=audio_bytes,
                audio_format=audio_format or "mp3",
                transcription=transcription,
                call_id=call_id,
                company_id=resolved_company_id,
                service_id=resolved_service_id,
                audio_duration_seconds=audio_duration_seconds,
            )
            raw_response = lab_exec_result.raw_response
            if lab_exec_result.transcription:
                transcription = lab_exec_result.transcription
        except LaboratoryExecutionError as lab_err:
            logger.error(
                "[Laboratory] Model execution failed: model_key=%s stage=%s error=%s",
                clean_model_key, lab_err.stage, lab_err.message,
            )
            return {
                "ok": False,
                "status": "error",
                "stage": lab_err.stage,
                "error_message": lab_err.message,
                "requested_model_key": clean_model_key,
                "provider": lab_err.provider,
                "model": lab_err.model,
                "details": lab_err.details,
            }

    else:
        # ── 2B. Default Legacy / Current Behavior (100% Backward Compatible) ──
        if not transcription:
            logger.info(
                "No transcription provided. Resolving and transcribing audio for call_id=%s (is_demo=%s)",
                call_id,
                resolved_is_demo,
            )
            from app.services.hubspot_service import HubSpotService
            from app.services.audio_download_service import (
                AudioDownloadService,
                AudioDownloadError,
                detect_audio_format,
            )
            
            # 1. HubSpot Resolution
            try:
                hs_service = HubSpotService(is_demo=resolved_is_demo)
                hubspot_data = await hs_service.get_call(call_id, is_demo=resolved_is_demo)
                target_url = hubspot_data.get("recording_url")
                cloudtalk_id = hubspot_data.get("cloudtalk_id")
                if not target_url and not cloudtalk_id:
                    return {
                        "ok": False,
                        "status": "error",
                        "stage": "hubspot",
                        "error_message": "Call resolved via HubSpot has no recording URL.",
                    }
            except Exception as e:
                logger.error("Failed to fetch HubSpot data for call_id=%s: %s", call_id, e)
                return {
                    "ok": False,
                    "status": "error",
                    "stage": "hubspot",
                    "error_message": f"HubSpot resolution failed: {str(e)}",
                }
                
            # 2. Download audio (CloudTalk primary when cloudtalk_id is present, with fallback to target_url)
            try:
                downloader = AudioDownloadService()
                if cloudtalk_id:
                    audio_bytes = await downloader.download_audio(target_url, cloudtalk_id=cloudtalk_id)
                else:
                    audio_bytes = await downloader.download_audio(target_url)
            except AudioDownloadError as e:
                logger.error("Audio download error for call_id=%s (source=%s): %s", call_id, e.stage, e.message)
                return {
                    "ok": False,
                    "status": "error",
                    "stage": "download_audio",
                    "error_message": f"Audio download failed: {e.message}",
                }
            except Exception as e:
                logger.error("Failed to download audio for call_id=%s: %s", call_id, e)
                return {
                    "ok": False,
                    "status": "error",
                    "stage": "download_audio",
                    "error_message": f"Audio download failed: {str(e)}",
                }
                
            # 3. Transcribe audio
            try:
                audio_ext = detect_audio_format(audio_bytes, target_url)
                logger.info("Transcribing call audio via Whisper for transcription analysis (format=%s)...", audio_ext)
                transcription_result = await openai_service.transcribe_audio(
                    audio_bytes,
                    filename=f"call.{audio_ext}",
                    company_id=resolved_company_id,
                    service_id=resolved_service_id,
                    module="test_analysis",
                )
                transcription = transcription_result.get("text")
                if not transcription:
                    return {
                        "ok": False,
                        "status": "error",
                        "stage": "transcription",
                        "error_message": "Whisper transcription returned empty text.",
                    }
            except Exception as e:
                logger.error("Whisper transcription failed: %s", e)
                return {
                    "ok": False,
                    "status": "error",
                    "stage": "transcription",
                    "error_message": f"Whisper transcription failed: {str(e)}",
                }

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
            model_to_use = None
            if (settings.ai_provider or "").lower() in ["azure", "azure_openai", "openai"]:
                model_to_use = settings.azure_openai_text_deployment
            raw_response = await openai_service.complete_text(
                messages=messages,
                response_format="json_object",
                model=model_to_use,
                company_id=resolved_company_id,
                service_id=resolved_service_id,
                module="test_analysis",
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
        logger.error("AI returned non-JSON response: %s", raw_response[:500] if raw_response else "")
        return {
            "ok": False,
            "status": "error",
            "stage": "parse",
            "error_message": "El modelo no devolvió un JSON válido.",
            "details": raw_response[:500] if raw_response else None,
        }

    # If direct_audio model included 'transcription' inside the JSON output, extract it if not already set
    if not transcription and isinstance(parsed.get("transcription"), str):
        transcription = parsed.get("transcription")

    # ── 4. Validate AI output ──────────────────────────────────────────────
    tipo_llamada = parsed.get("tipo_llamada")
    
    # Dynamically fetch active typologies (base structure priority, fallback to service)
    # Uses resolve_service_typology for canonical parity with mass evaluations & persistence
    typology_by_key = {}
    try:
        from sqlalchemy import select
        from app.models.prompts import Prompt, BaseStructureTypology
        from app.models.typologies import Typology
        from app.services.analysis_persistence import resolve_service_typology, normalize_tipo_llamada
        
        prompt_obj = await db.get(Prompt, resolved_prompt_id) if resolved_prompt_id else None
        typology_list = []
        if prompt_obj and prompt_obj.base_structure_id:
            t_stmt = (
                select(Typology)
                .join(BaseStructureTypology, BaseStructureTypology.typology_id == Typology.typology_id)
                .where(
                    BaseStructureTypology.base_structure_id == prompt_obj.base_structure_id,
                    Typology.is_active == True
                )
            )
            t_res = await db.execute(t_stmt)
            typology_list = t_res.scalars().all()
            
        if not typology_list and prompt_obj and prompt_obj.service_id:
            t_stmt = select(Typology).where(
                Typology.service_id == prompt_obj.service_id,
                Typology.is_active == True
            )
            t_res = await db.execute(t_stmt)
            typology_list = t_res.scalars().all()
            
        if not typology_list:
            t_stmt = select(Typology).where(Typology.is_active == True)
            t_res = await db.execute(t_stmt)
            typology_list = t_res.scalars().all()
            
        typology_by_key = {t.typology_key: t for t in typology_list}
        matched_typology, resolved_key = resolve_service_typology(tipo_llamada, typology_by_key)
        
        if resolved_key:
            tipo_llamada = resolved_key
            parsed["tipo_llamada"] = resolved_key
        elif tipo_llamada:
            norm_key = normalize_tipo_llamada(tipo_llamada)
            if norm_key in typology_by_key:
                tipo_llamada = norm_key
                parsed["tipo_llamada"] = norm_key
            else:
                tipo_llamada = norm_key or "otros"
                parsed["tipo_llamada"] = tipo_llamada
        else:
            tipo_llamada = "otros"
            parsed["tipo_llamada"] = "otros"
    except Exception as e:
        logger.warning("Failed to resolve dynamic typology in transcription analysis: %s", e)
        if tipo_llamada is None:
            parsed["tipo_llamada"] = "otros"

    # ── 4.5. Defensive Keys Guard ──────────────────────────────────────────
    try:
        if parsed and isinstance(parsed, dict) and resolved_prompt_id:
            from app.services.criteria_service import get_active_criteria
            from app.models.criteria import PromptCriterionTypology
            from app.models.typologies import Typology
            from sqlalchemy import select
            
            active_criteria_objs = await get_active_criteria(db, resolved_prompt_id)
            if active_criteria_objs:
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

                import re
                header_pattern = re.compile(
                    r"^(?:###?\s+)?(?:FORMATO\s+DE\s+(?:RESPUESTA|SALIDA(?:\s+JSON)?))\b",
                    re.IGNORECASE | re.MULTILINE
                )
                matches = list(header_pattern.finditer(prompt_content or ""))
                format_section = (prompt_content or "")[matches[-1].start():] if matches else ""
                
                missing_result_keys = []
                for c in active_criteria_objs:
                    if c.output_key:
                        if c.output_key not in parsed:
                            parsed[c.output_key] = None
                            missing_result_keys.append(c.output_key)
                            
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
                            
                    if c.feed_key:
                        if c.feed_key not in parsed:
                            parsed[c.feed_key] = None
                            missing_result_keys.append(c.feed_key)
                            
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
    call_metadata["service_id"] = resolved_service_id
    call_metadata["company_id"] = resolved_company_id
    call_metadata.setdefault("run_ts", now)

    if lab_exec_result is not None:
        eff_model_metadata = {
            "model_provider": lab_exec_result.actual_provider,
            "model_name": lab_exec_result.actual_model,
        }
        eff_transcription_metadata = {
            "provider": lab_exec_result.stt_provider,
            "model": lab_exec_result.stt_model,
        }
        lab_meta_dict = {
            "requested_model_key": lab_exec_result.requested_model_key,
            "actual_provider": lab_exec_result.actual_provider,
            "actual_model": lab_exec_result.actual_model,
            "pipeline_mode": lab_exec_result.pipeline_mode,
            "stt_provider": lab_exec_result.stt_provider,
            "stt_model": lab_exec_result.stt_model,
            "analysis_provider": lab_exec_result.analysis_provider,
            "analysis_model": lab_exec_result.analysis_model,
            "estimated_cost_usd": lab_exec_result.estimated_cost_usd,
            "stt_cost_usd": lab_exec_result.stt_cost_usd,
            "llm_cost_usd": lab_exec_result.llm_cost_usd,
            "pricing_status": lab_exec_result.pricing_status,
            "latency_ms": lab_exec_result.latency_ms,
            "stt_latency_ms": lab_exec_result.stt_latency_ms,
            "llm_latency_ms": lab_exec_result.llm_latency_ms,
        }
        eff_payload = {
            "transcription": transcription,
            "raw_response": raw_response,
            **lab_meta_dict,
            "lab_model_metadata": lab_meta_dict,
        }
    else:
        default_provider = settings.ai_provider
        default_model = (
            settings.gemini_analysis_model
            if settings.ai_provider == "gemini"
            else settings.azure_openai_text_deployment
        )
        eff_model_metadata = {
            "model_provider": default_provider,
            "model_name": default_model,
        }
        eff_transcription_metadata = None
        eff_payload = {
            "transcription": transcription,
            "raw_response": raw_response,
        }

    try:
        analysis_record = await save_analysis(
            db=db,
            analysis_type=analysis_type,
            call_metadata=call_metadata,
            prompt_metadata={
                "prompt_id": resolved_prompt_id,
                "prompt_version_id": resolved_version_id,
            },
            model_metadata=eff_model_metadata,
            result_json=parsed,
            payload=eff_payload,
            transcription=transcription,
            transcription_metadata=eff_transcription_metadata,
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
    resolved_service_name = None
    if analysis_record.service_id:
        from app.models.services import Service
        s_obj = await db.get(Service, analysis_record.service_id)
        if s_obj:
            resolved_service_name = s_obj.service_name

    resolved_prompt_name = None
    if analysis_record.prompt_id:
        from app.models.prompts import Prompt
        p_obj = await db.get(Prompt, analysis_record.prompt_id)
        if p_obj:
            resolved_prompt_name = p_obj.prompt_name

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

    # Build items_json and items_visual for contract parity with Mass Evaluations
    items_json = []
    try:
        from app.services.criteria_service import get_active_criteria
        from app.models.criteria import PromptCriterionTypology
        from app.services.analysis_results_mapper import map_criterion_value
        from app.utils.visual_formatters import build_items_visual
        from sqlalchemy import select
        
        active_crits = await get_active_criteria(db, resolved_prompt_id) if resolved_prompt_id else []
        c_ids = [c.criterion_id for c in active_crits]
        assoc_map = {}
        if c_ids:
            assoc_res = await db.execute(
                select(PromptCriterionTypology).where(PromptCriterionTypology.criterion_id.in_(c_ids))
            )
            for assoc in assoc_res.scalars().all():
                if assoc.criterion_id not in assoc_map:
                    assoc_map[assoc.criterion_id] = set()
                assoc_map[assoc.criterion_id].add(assoc.typology_id)
                
        matched_t = typology_by_key.get(parsed.get("tipo_llamada")) if 'typology_by_key' in locals() and typology_by_key else None
        
        for c in active_crits:
            out_k = c.output_key
            feed_k = c.feed_key
            is_app = True
            if c.criterion_key != "tipo_llamada" and matched_t:
                allowed_t = assoc_map.get(c.criterion_id, set())
                if allowed_t:
                    is_app = (getattr(matched_t, "typology_id", None) in allowed_t)
            
            raw_val = parsed.get(out_k) if out_k else None
            feed_val = parsed.get(feed_k) if feed_k else None
            typed = map_criterion_value(raw_val, c.criterion_type or "text") if is_app else {
                "raw_value": None, "value_number": None, "value_text": None,
                "value_boolean": None, "value_category": None
            }
            
            resolved_val = None
            if is_app:
                if c.criterion_type == "number":
                    resolved_val = float(typed["value_number"]) if typed["value_number"] is not None else None
                elif c.criterion_type == "boolean":
                    resolved_val = typed["value_boolean"]
                else:
                    resolved_val = typed["value_text"] or typed["value_category"] or typed["raw_value"]
                    
            items_json.append({
                "criterion_id": c.criterion_id,
                "criterion_key": c.criterion_key,
                "name": c.criterion_name,
                "type": c.criterion_type,
                "output_key": out_k,
                "value": resolved_val,
                "feed": str(feed_val) if feed_val is not None else None,
                "not_applicable": not is_app,
                "numeric_value": typed["value_number"],
                "text_value": typed["value_text"],
                "boolean_value": typed["value_boolean"],
                "category_value": typed["value_category"],
                "percentage_value": typed["value_number"] if c.criterion_type == "percentage" else None,
                "raw_value": typed["raw_value"],
            })
    except Exception as e_items:
        logger.warning("Error building items_json/items_visual in transcription analysis: %s", e_items)
        items_json = []

    items_visual = build_items_visual(items_json) if items_json else []

    response_dict: dict[str, Any] = {
        "ok": True,
        "status": "completed",
        "message": "Análisis por transcripción completado y guardado en base de datos",
        "call_id": call_id,
        "analysis_id": analysis_record.analysis_id,
        "service_id": analysis_record.service_id,
        "service_name": resolved_service_name,
        "prompt_id": analysis_record.prompt_id,
        "prompt_name": resolved_prompt_name,
        "requested_model_key": lab_exec_result.requested_model_key if lab_exec_result else None,
        "actual_model": analysis_record.model_name,
        "provider": analysis_record.model_provider,
        "pipeline_mode": lab_exec_result.pipeline_mode if lab_exec_result else None,
        "stt_provider": lab_exec_result.stt_provider if lab_exec_result else analysis_record.transcription_provider,
        "stt_model": lab_exec_result.stt_model if lab_exec_result else analysis_record.transcription_model,
        "analysis_provider": lab_exec_result.analysis_provider if lab_exec_result else analysis_record.model_provider,
        "analysis_model": lab_exec_result.analysis_model if lab_exec_result else analysis_record.model_name,
        "estimated_cost_usd": lab_exec_result.estimated_cost_usd if lab_exec_result else None,
        "stt_cost_usd": lab_exec_result.stt_cost_usd if lab_exec_result else None,
        "llm_cost_usd": lab_exec_result.llm_cost_usd if lab_exec_result else None,
        "pricing_status": lab_exec_result.pricing_status if lab_exec_result else None,
        "summary": summary,
        "result": parsed,
        "items_json": items_json,
        "items_visual": items_visual,
    }
    if transcription is not None:
        response_dict["transcription"] = transcription

    return response_dict
