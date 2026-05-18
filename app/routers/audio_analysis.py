"""
Audio analysis router.
Skeleton for Phase 2 — HubSpot + OpenAI audio integration.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.analyses import AnalyzeAudioRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Audio Analysis"])


@router.post("/analyze-audio")
async def analyze_audio(
    body: AnalyzeAudioRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Analyze a call audio from HubSpot using an audio model.

    Phase 2 — Not yet implemented. Returns 501 until credentials & services are wired.
    """
    raise HTTPException(
        status_code=501,
        detail={
            "ok": False,
            "status": "not_implemented",
            "error_message": "analyze-audio is scheduled for Phase 2. Wire HUBSPOT_ACCESS_TOKEN and AZURE_OPENAI_AUDIO_DEPLOYMENT first.",
        },
    )
