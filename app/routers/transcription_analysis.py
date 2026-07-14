"""
Transcription & text-analysis router.
"""
import logging
from typing import Annotated
from fastapi import APIRouter, Depends
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

