"""
Transcription & text-analysis router.
Skeleton for Phase 2.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.analyses import AnalyzeTranscriptionRequest, TranscribeRequest

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
    raise HTTPException(
        status_code=501,
        detail={
            "ok": False,
            "status": "not_implemented",
            "error_message": "transcribe is scheduled for Phase 2. Wire TWILIO_ACCOUNT_SID and AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT first.",
        },
    )


@router.post("/analyze-transcription")
async def analyze_transcription(
    body: AnalyzeTranscriptionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Analyze an existing transcription using an active prompt.
    """
    from app.services.transcription_analysis_service import analyze_transcription_pipeline

    result = await analyze_transcription_pipeline(
        db=db,
        call_id=body.call_id,
        transcription=body.transcription,
        analysis_type=body.analysis_type,
        prompt_id=body.prompt_id,
        prompt_version_id=body.prompt_version_id,
        metadata=body.metadata,
    )

    if not result.get("ok"):
        # The service returns ok: False strings if something fails gracefully
        from fastapi.encoders import jsonable_encoder
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content=jsonable_encoder(result))

    return result
