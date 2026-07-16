"""
Transcription & text-analysis router.
"""
import logging
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, File, UploadFile, Form, status, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.analyses import AnalyzeTranscriptionRequest, TranscribeRequest, TestAnalysisByCallIdRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Transcription Analysis"])


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
            prompt_id=body.prompt_id,
            prompt_version_id=body.prompt_version_id,
            metadata=body.metadata,
        )
    except Exception as exc:
        # Catch any unexpected exception and return structured JSON (never empty body)
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
        # Map stage to HTTP status code:
        #   validation errors  → 422
        #   azure / parse      → 502
        #   save_analysis / DB → 500
        #   anything else      → 400
        stage = result.get("stage", "")
        if stage == "validation":
            status_code = 422
        elif stage in ("azure",):
            status_code = 502
        elif stage in ("save_analysis", "internal"):
            status_code = 500
        else:
            status_code = 400

        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(result),
        )

    # Encode the success response through jsonable_encoder so that any
    # Decimal / datetime values returned from the DB layer are serialized
    # correctly (Decimal → float, datetime → ISO string).
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
    Test direct analysis using call_id. Resolves audio from HubSpot/Twilio,
    transcribes it, and analyzes it. Can accept a custom prompt (as custom_prompt or prompt).
    """
    logger.info(f"Received request for test-analysis/by-call-id. Body: {body.model_dump()}")
    from app.services.transcription_analysis_service import analyze_transcription_pipeline

    custom_prompt = body.custom_prompt or body.prompt

    try:
        result = await analyze_transcription_pipeline(
            db=db,
            call_id=body.call_id,
            transcription=None,
            custom_prompt_text=custom_prompt,
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
        stage = result.get("stage", "")
        if stage == "validation":
            status_code = 422
        elif stage in ("azure",):
            status_code = 502
        elif stage in ("save_analysis", "internal"):
            status_code = 500
        else:
            status_code = 400

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
    db: AsyncSession = Depends(get_db),
):
    """
    Test direct analysis by uploading an audio file (MP3 or WAV).
    Transcribes the audio and runs the analysis pipeline.
    """
    import os
    import tempfile
    import uuid
    from datetime import datetime
    from app.services import openai_service
    from app.services.transcription_analysis_service import analyze_transcription_pipeline

    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    content_type = (file.content_type or "").lower()

    logger.info(
        "Initiating test-analysis/by-audio-upload. File: %s, size: %s, content_type: %s",
        filename,
        file.size if hasattr(file, "size") else "unknown",
        content_type
    )

    valid_extensions = {".mp3", ".wav"}
    valid_content_types = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/wave"}

    if ext not in valid_extensions and content_type not in valid_content_types:
        logger.error("Invalid file format uploaded: file=%s, content_type=%s", filename, content_type)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Archivo no válido. Solo se admiten archivos .mp3 y .wav con un content-type de audio adecuado."
        )

    # 1. Write the file to a secure temporary path
    suffix = ext if ext in valid_extensions else ".mp3"
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = temp_file.name
    try:
        contents = await file.read()
        temp_file.write(contents)
        temp_file.close()

        # 2. Transcribe using Whisper via openai_service
        logger.info("Transcribing uploaded audio file via Whisper for transcription analysis...")
        transcription_result = await openai_service.transcribe_audio(contents, filename=f"upload{suffix}")
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
        # Clean up temporary file
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                logger.info("Temporary audio file cleaned up successfully.")
            except Exception as e:
                logger.error("Failed to remove temporary file %s: %s", temp_path, e)

    # 3. Analyze using the pipeline
    dummy_call_id = f"upload-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    resolved_prompt = custom_prompt or prompt

    logger.info("Starting analysis pipeline with dummy call_id=%s...", dummy_call_id)
    try:
        result = await analyze_transcription_pipeline(
            db=db,
            call_id=dummy_call_id,
            transcription=transcription,
            custom_prompt_text=resolved_prompt,
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
        stage = result.get("stage", "")
        if stage == "validation":
            status_code = 422
        elif stage in ("azure",):
            status_code = 502
        elif stage in ("save_analysis", "internal"):
            status_code = 500
        else:
            status_code = 400

        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(result),
        )

    # 4. Success Response - Inject transcription
    result["transcription"] = transcription
    logger.info("Analysis completed and saved successfully for uploaded audio.")
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(result),
    )

