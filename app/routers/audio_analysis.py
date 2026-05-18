"""
Audio analysis router.
Handles POST /bm/analyze-audio via the audio_analysis_service.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.analyses import AnalyzeAudioRequest
from app.services.audio_analysis_service import process_audio_analysis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Audio Analysis"])


@router.post("/analyze-audio")
async def analyze_audio(
    body: AnalyzeAudioRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Analyze a call audio from HubSpot (or direct URL) using Azure OpenAI multimodal audio model.
    """
    try:
        result = await process_audio_analysis(db, body)
    except Exception as e:
        logger.exception("Unhandled exception in analyze_audio: %s", e)
        result = {
            "ok": False,
            "status": "error",
            "stage": "internal",
            "error_message": f"Internal server error: {str(e)}",
        }

    if not result.get("ok"):
        stage = result.get("stage", "")
        if stage in ("validation", "audio_validation", "prompt_resolution"):
            status_code = 422
        elif stage in ("azure", "download_audio", "hubspot"):
            status_code = 502
        elif stage in ("save_analysis", "internal", "azure_config"):
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
