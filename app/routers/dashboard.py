"""Dashboard router."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services.dashboard_service import get_dashboard_summary

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Dashboard"])


@router.get("/dashboard/summary")
async def dashboard_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="24h | 7d | 30d")] = "24h",
):
    """
    Get dashboard summary metrics including KPIs, evolution charts,
    agent rankings, and latest analyses.
    """
    try:
        data = await get_dashboard_summary(db, analysis_type=type, period=period)
        return data
    except Exception as e:
        logger.exception("Failed to retrieve dashboard summary")
        raise HTTPException(status_code=500, detail=str(e))
