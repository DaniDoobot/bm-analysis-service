"""Dashboard and advanced analytics router."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services.dashboard_service import (
    get_dashboard_summary,
    get_agents_list,
    get_agent_evolution,
    get_objections_breakdown,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Dashboard & Analytics"])


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


@router.get("/agents")
async def list_agents(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Get all active call center agents with their accumulated real metrics.
    """
    try:
        data = await get_agents_list(db)
        return data
    except Exception as e:
        logger.exception("Failed to retrieve agents list")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/{hubspot_owner_id}/evolution")
async def agent_evolution(
    hubspot_owner_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="7d | 30d | 90d | all")] = "30d",
    bucket: Annotated[str | None, Query(description="day | week")] = None,
    prompt_version_id: Annotated[int | None, Query(description="Filter by prompt version")] = None,
):
    """
    Get chronological performance, trends, strengths, weaknesses,
    and evolution timelines for a specific agent.
    """
    try:
        data = await get_agent_evolution(
            db,
            hubspot_owner_id=hubspot_owner_id,
            analysis_type=type,
            period=period,
            bucket_param=bucket,
            prompt_version_id=prompt_version_id,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve agent performance evolution")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard/objections")
async def objections_breakdown(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="24h | 7d | 30d | 90d | all")] = "7d",
    agent_id: Annotated[str | None, Query(description="hubspot_owner_id")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Type of call")] = None,
):
    """
    Get categorized objection lists, agent-specific counts,
    and a chronological list of calls that raised objections.
    """
    try:
        data = await get_objections_breakdown(
            db,
            analysis_type=type,
            period=period,
            agent_id=agent_id,
            tipo_llamada=tipo_llamada,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve objections breakdown")
        raise HTTPException(status_code=500, detail=str(e))
