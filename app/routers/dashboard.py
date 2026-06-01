"""Dashboard and advanced analytics router."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.services.dashboard_service import (
    get_dashboard_summary,
    get_agents_list,
    get_agent_evolution,
    get_objections_breakdown,
    get_mass_result_detail,
    get_agents_comparison,
)
from app.schemas.dashboard import AgentComparisonResponse
from app.utils.hubspot_owners import resolve_owner_id_by_email, resolve_owner_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Dashboard & Analytics"])


@router.get("/dashboard/summary")
async def dashboard_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="24h | 7d | 30d")] = "24h",
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
):
    """
    Get dashboard summary metrics including KPIs, evolution charts,
    agent rankings, and latest analyses.
    """
    try:
        data = await get_dashboard_summary(
            db,
            analysis_type=type,
            period=period,
            service_id=service_id,
            service_key=service_key,
            date_from=date_from,
            date_to=date_to,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve dashboard summary")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard/agents-comparison", response_model=AgentComparisonResponse)
async def agents_comparison(
    db: Annotated[AsyncSession, Depends(get_db)],
    hubspot_owner_ids: Annotated[str | None, Query(description="Comma-separated HubSpot owner IDs")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    typology_id: Annotated[int | None, Query(description="Filter by typology ID")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    period: Annotated[str | None, Query(description="24h | 7d | 30d | 90d | all")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    bucket: Annotated[str | None, Query(description="hour | day | week")] = None,
):
    """
    Get multi-agent comparison analytics for dashboard reporting.
    """
    owner_ids = None
    if hubspot_owner_ids and hubspot_owner_ids.strip():
        owner_ids = [oid.strip() for oid in hubspot_owner_ids.split(",") if oid.strip()]
        
    try:
        data = await get_agents_comparison(
            db,
            hubspot_owner_ids=owner_ids,
            service_id=service_id,
            service_key=service_key,
            typology_id=typology_id,
            typology_key=typology_key,
            period=period,
            date_from=date_from,
            date_to=date_to,
            bucket=bucket,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve agent comparison metrics")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents")
async def list_agents(
    db: Annotated[AsyncSession, Depends(get_db)],
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
):
    """
    Get all active call center agents with their accumulated real metrics.
    """
    try:
        data = await get_agents_list(
            db,
            service_id=service_id,
            service_key=service_key,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve agents list")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/{hubspot_owner_id}/evolution")
async def agent_evolution(
    hubspot_owner_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="24h | 7d | 30d | 90d | all")] = "30d",
    bucket: Annotated[str | None, Query(description="hour | day | week")] = None,
    prompt_version_id: Annotated[int | None, Query(description="Filter by prompt version")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
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
            service_id=service_id,
            service_key=service_key,
            date_from=date_from,
            date_to=date_to,
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
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
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
            service_id=service_id,
            service_key=service_key,
            date_from=date_from,
            date_to=date_to,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve objections breakdown")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/me/evolution")
async def get_my_evolution(
    email: Annotated[str, Query(description="Email of logged-in agent")],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="7d | 30d | 90d | all")] = "30d",
    bucket: Annotated[str | None, Query(description="day | week")] = None,
):
    """
    Get chronological performance evolution metrics specifically for the logged-in agent.
    """
    owner_id = resolve_owner_id_by_email(email)
    if not owner_id:
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "status": "forbidden",
                "error_message": "No hay agente asociado a este usuario."
            }
        )

    try:
        data = await get_agent_evolution(
            db,
            hubspot_owner_id=owner_id,
            analysis_type=type,
            period=period,
            bucket_param=bucket,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve logged-in agent performance evolution")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/me/agent")
async def get_my_agent_details(
    email: Annotated[str, Query(description="Email of logged-in agent")],
):
    """
    Verify and retrieve details of the agent associated with the provided email.
    """
    owner_id = resolve_owner_id_by_email(email)
    if not owner_id:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "status": "not_found",
                "error_message": "No hay agente asociado a este email."
            }
        )

    agent_name = resolve_owner_name(owner_id) or owner_id
    return {
        "ok": True,
        "email": email.strip().lower(),
        "hubspot_owner_id": owner_id,
        "agent_name": agent_name
    }


@router.get("/dashboard/latest-analyses/{identifier}")
async def get_latest_analysis_detail(
    identifier: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Get the full detail of a single MassEvaluationResult by ID or call_id.
    """
    try:
        data = await get_mass_result_detail(db, identifier)
        if not data:
            raise HTTPException(
                status_code=404,
                detail=f"Mass evaluation result with identifier '{identifier}' not found."
            )
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to retrieve mass analysis detail")
        raise HTTPException(status_code=500, detail=str(e))

