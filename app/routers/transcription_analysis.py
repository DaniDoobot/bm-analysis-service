"""
Transcription & text-analysis router (including Laboratory / Test Analysis endpoints).
"""
import logging
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, File, UploadFile, Form, Query, status, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.analyses import AnalyzeTranscriptionRequest, TranscribeRequest, TestAnalysisByCallIdRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Transcription Analysis"])


def _map_pipeline_stage_to_status_code(stage: str) -> int:
    """Map pipeline error stage to HTTP status code."""
    if stage in ("validation", "context_limit"):
        return 422
    if stage in ("model_validation", "model_config", "model_not_available", "model_unauthorized", "unsupported_audio"):
        return 400
    if stage in ("azure", "llm"):
        return 502
    if stage in ("save_analysis", "internal"):
        return 500
    return 400


@router.get("/test-analysis/models")
async def get_test_analysis_models(
    service_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the catalog of enabled AI models exclusively available for Laboratory / Test Analysis.
    Includes `configured: true/false` per model without exposing any API keys or secrets.
    """
    from app.services.laboratory_ai_service import list_laboratory_models

    resolved_cid = company_id
    if service_id and not resolved_cid:
        try:
            from app.core.side_effects import resolve_company_demo_status
            resolved_cid, _ = await resolve_company_demo_status(db, service_id=service_id)
        except Exception:
            pass

    models = list_laboratory_models(company_id=resolved_cid, service_id=service_id)
    return JSONResponse(status_code=200, content=jsonable_encoder(models))


@router.post("/test-analysis/models/{model_key}/check")
async def check_test_analysis_model(
    model_key: str,
    service_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Perform a lightweight, non-costly check to verify if the selected Laboratory model
    is configured and reachable with the current API key/project.
    Never performs silent fallback.
    """
    from app.services.laboratory_ai_service import (
        check_model_availability,
        LaboratoryExecutionError,
    )

    resolved_cid = company_id
    if service_id and not resolved_cid:
        try:
            from app.core.side_effects import resolve_company_demo_status
            resolved_cid, _ = await resolve_company_demo_status(db, service_id=service_id)
        except Exception:
            pass

    try:
        res = await check_model_availability(
            model_key=model_key,
            company_id=resolved_cid,
            service_id=service_id,
        )
        return JSONResponse(status_code=200, content=jsonable_encoder(res))
    except LaboratoryExecutionError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "model_key": model_key,
                "configured": False,
                "reachable": False,
                "detail": exc.message,
            },
        )


@router.post("/transcribe")
async def transcribe(
    body: TranscribeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Download audio from HubSpot/Twilio and transcribe it.

    Phase 2 — Not yet implemented.
    """
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "status": "not_implemented",
            "stage": "transcribe",
            "error_message": (
                "transcribe is scheduled for Phase 2. "
                "Wire TWILIO_ACCOUNT_SID and AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT first."
            ),
        },
    )


@router.post("/analyze-transcription")
async def analyze_transcription(
    body: AnalyzeTranscriptionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Analyze an existing transcription using the active prompt.

    Always returns a JSON body — never an empty response.
    Error shape: {"ok": false, "status": "error", "stage": "...", "error_message": "..."}
    """
    from app.services.transcription_analysis_service import analyze_transcription_pipeline

    try:
        result = await analyze_transcription_pipeline(
            db=db,
            call_id=body.call_id,
            transcription=body.transcription,
            analysis_type=body.analysis_type,
            prompt_id=body.effective_prompt_id,
            prompt_version_id=body.prompt_version_id,
            service_id=body.effective_service_id,
            metadata=body.metadata,
            model_key=body.model_key,
        )
    except Exception as exc:
        logger.error(
            "Unhandled exception in analyze_transcription (call_id=%s): %s",
            getattr(body, "call_id", "unknown"),
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "status": "error",
                "stage": "internal",
                "error_message": f"Internal server error: {str(exc)}",
            },
        )

    if not result.get("ok"):
        status_code = _map_pipeline_stage_to_status_code(result.get("stage", ""))
        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(result),
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(result),
    )


@router.post("/test-analysis/by-call-id")
async def test_analysis_by_call_id(
    body: TestAnalysisByCallIdRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Test direct analysis using call_id. Resolves audio from HubSpot/Twilio/CloudTalk,
    transcribes/evaluates it using either the default model or the selected Laboratory `model_key`.
    """
    logger.info(f"Received request for test-analysis/by-call-id. Body: {body.model_dump()}")
    from app.services.transcription_analysis_service import analyze_transcription_pipeline

    custom_prompt = body.custom_prompt or body.prompt
    eff_service_id = body.effective_service_id
    eff_prompt_id = body.effective_prompt_id

    try:
        result = await analyze_transcription_pipeline(
            db=db,
            call_id=body.call_id,
            transcription=None,
            custom_prompt_text=custom_prompt,
            service_id=eff_service_id,
            prompt_id=eff_prompt_id,
            model_key=body.model_key,
        )
    except Exception as exc:
        logger.error(
            "Unhandled exception in test_analysis_by_call_id (call_id=%s): %s",
            body.call_id,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "status": "error",
                "stage": "internal",
                "error_message": f"Internal server error: {str(exc)}",
            },
        )

    if not result.get("ok"):
        status_code = _map_pipeline_stage_to_status_code(result.get("stage", ""))
        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(result),
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(result),
    )


@router.post("/test-analysis/by-audio-upload")
async def test_analysis_by_audio_upload(
    file: UploadFile = File(...),
    custom_prompt: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    service_id: Optional[int] = Form(None),
    selected_service_id: Optional[int] = Form(None),
    prompt_id: Optional[int] = Form(None),
    structure_id: Optional[int] = Form(None),
    evaluation_structure_id: Optional[int] = Form(None),
    model_key: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Test direct analysis by uploading an audio file (MP3 or WAV).
    If `model_key` is omitted, uses the default transcription + analysis pipeline.
    If `model_key` is provided, routes through the isolated Laboratory model selector.
    """
    import os
    import tempfile
    import uuid
    from datetime import datetime
    from app.services import openai_service
    from app.services.transcription_analysis_service import analyze_transcription_pipeline

    eff_service_id = service_id or selected_service_id
    eff_prompt_id = prompt_id or structure_id or evaluation_structure_id
    clean_model_key = model_key.strip() if isinstance(model_key, str) and model_key.strip() else None

    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    content_type = (file.content_type or "").lower()

    logger.info(
        "Initiating test-analysis/by-audio-upload. File: %s, size: %s, content_type: %s, service_id: %s, prompt_id: %s, model_key: %s",
        filename,
        file.size if hasattr(file, "size") else "unknown",
        content_type,
        eff_service_id,
        eff_prompt_id,
        clean_model_key,
    )

    valid_extensions = {".mp3", ".wav"}
    valid_content_types = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/wave"}

    if ext not in valid_extensions and content_type not in valid_content_types:
        logger.error("Invalid file format uploaded: file=%s, content_type=%s", filename, content_type)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Archivo no válido. Solo se admiten archivos .mp3 y .wav con un content-type de audio adecuado."
        )

    suffix = ext if ext in valid_extensions else ".mp3"
    audio_format = suffix.lstrip(".") or "mp3"
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = temp_file.name
    transcription: Optional[str] = None

    try:
        contents = await file.read()
        temp_file.write(contents)
        temp_file.close()

        if not clean_model_key:
            # Default Legacy Flow: Transcribe using Whisper via openai_service
            logger.info("Transcribing uploaded audio file via Whisper for transcription analysis...")
            resolved_cid = None
            if eff_service_id:
                try:
                    from app.core.side_effects import resolve_company_demo_status
                    resolved_cid, _ = await resolve_company_demo_status(db, service_id=eff_service_id)
                except Exception:
                    pass
            transcription_result = await openai_service.transcribe_audio(
                contents,
                filename=f"upload{suffix}",
                company_id=resolved_cid,
                service_id=eff_service_id,
                module="test_analysis",
            )
            transcription = transcription_result.get("text")
            if not transcription:
                logger.error("Whisper transcription returned empty text for uploaded file.")
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "status": "error",
                        "stage": "transcription",
                        "error_message": "No se pudo transcribir el audio. Whisper devolvió un texto vacío."
                    }
                )
            logger.info("Whisper transcription completed successfully.")

    except Exception as exc:
        logger.error("Error during file save or transcription: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "status": "error",
                "stage": "transcription",
                "error_message": f"Transcription failed: {str(exc)}",
            }
        )
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                logger.info("Temporary audio file cleaned up successfully.")
            except Exception as e:
                logger.error("Failed to remove temporary file %s: %s", temp_path, e)

    dummy_call_id = f"upload-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    resolved_prompt = custom_prompt or prompt

    logger.info(
        "Starting analysis pipeline with dummy call_id=%s, service_id=%s, prompt_id=%s, model_key=%s...",
        dummy_call_id, eff_service_id, eff_prompt_id, clean_model_key,
    )
    try:
        result = await analyze_transcription_pipeline(
            db=db,
            call_id=dummy_call_id,
            transcription=transcription,
            custom_prompt_text=resolved_prompt,
            service_id=eff_service_id,
            prompt_id=eff_prompt_id,
            model_key=clean_model_key,
            audio_bytes=contents if clean_model_key else None,
            audio_format=audio_format,
        )
    except Exception as exc:
        logger.error(
            "Unhandled exception in analyze_transcription_pipeline for uploaded audio (call_id=%s): %s",
            dummy_call_id,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "status": "error",
                "stage": "internal",
                "error_message": f"Internal server error during analysis: {str(exc)}",
            },
        )

    if not result.get("ok"):
        status_code = _map_pipeline_stage_to_status_code(result.get("stage", ""))
        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(result),
        )

    if transcription and not result.get("transcription"):
        result["transcription"] = transcription
    logger.info("Analysis completed and saved successfully for uploaded audio.")
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(result),
    )
