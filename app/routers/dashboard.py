"""Dashboard and advanced analytics router."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, get_tenant_context
from app.core.tenant_context import TenantContext, InternalRole
from app.models.users import User
from app.services.dashboard_service import (
    get_dashboard_summary,
    get_agents_list,
    get_agent_evolution,
    get_objections_breakdown,
    get_mass_result_detail,
    get_agents_comparison,
)
from app.schemas.dashboard import AgentComparisonResponse, AgentEvolutionResponse
from app.utils.hubspot_owners import resolve_owner_id_by_email, resolve_owner_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Dashboard & Analytics"])


@router.get("/dashboard/summary")
async def dashboard_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="24h | 7d | 30d")] = "24h",
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
):
    """
    Get dashboard summary metrics including KPIs, evolution charts,
    agent rankings, and latest analyses.
    """
    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

    if service_id is not None and not context.is_super_admin:
        if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado: No tienes permisos para este servicio."
            )

    try:
        data = await get_dashboard_summary(
            db,
            analysis_type=type,
            period=period,
            service_id=service_id,
            service_key=service_key,
            date_from=date_from,
            date_to=date_to,
            typology_ids=typo_ids,
            duration_min_seconds=duration_min_seconds,
            duration_max_seconds=duration_max_seconds,
            avg_score_min=avg_score_min,
            avg_score_max=avg_score_max,
            context=context,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve dashboard summary")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard/agents-comparison", response_model=AgentComparisonResponse)
async def agents_comparison(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    hubspot_owner_ids: Annotated[str | None, Query(description="Comma-separated HubSpot owner IDs")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    typology_id: Annotated[int | None, Query(description="Filter by typology ID")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    period: Annotated[str | None, Query(description="24h | 7d | 30d | 90d | all")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    bucket: Annotated[str | None, Query(description="hour | day | week")] = None,
    metric_key: Annotated[str | None, Query(description="Selected metric key to compare")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
):
    """
    Get multi-agent comparison analytics for dashboard reporting.
    """
    owner_ids = None
    if hubspot_owner_ids and hubspot_owner_ids.strip():
        owner_ids = [oid.strip() for oid in hubspot_owner_ids.split(",") if oid.strip()]
        
    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]
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
            metric_key=metric_key,
            typology_ids=typo_ids,
            duration_min_seconds=duration_min_seconds,
            duration_max_seconds=duration_max_seconds,
            avg_score_min=avg_score_min,
            avg_score_max=avg_score_max,
            context=context,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve agent comparison metrics")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents")
async def list_agents(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    period: Annotated[str | None, Query(description="Filter by period (e.g., 24h, 7d, 30d)")] = None,
    date_from: Annotated[str | None, Query(description="Start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="End date (ISO or YYYY-MM-DD)")] = None,
    type: Annotated[str | None, Query(description="Filter by analysis type (kept for compatibility)")] = None,
):
    """
    Get all active call center agents with their accumulated real metrics.
    """
    try:
        data = await get_agents_list(
            db,
            service_id=service_id,
            service_key=service_key,
            period=period,
            date_from=date_from,
            date_to=date_to,
            type=type,
            context=context,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve agents list")
        raise HTTPException(status_code=500, detail=str(e))


def resolve_agent_owner_id(user: User) -> str | None:
    if user.hubspot_owner_id:
        return user.hubspot_owner_id
    return resolve_owner_id_by_email(user.email)


@router.get("/agents/{hubspot_owner_id}/evolution", response_model=AgentEvolutionResponse)
async def agent_evolution(
    hubspot_owner_id: str,
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="24h | 7d | 30d | 90d | all")] = "30d",
    bucket: Annotated[str | None, Query(description="hour | day | week")] = None,
    prompt_version_id: Annotated[int | None, Query(description="Filter by prompt version")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
):
    """
    Get chronological performance, trends, strengths, weaknesses,
    and evolution timelines for a specific agent.
    """
    if context.allowed_agent_ids is not None and hubspot_owner_id not in context.allowed_agent_ids:
        raise HTTPException(
            status_code=403,
            detail="No tienes permiso para consultar la evolución de este agente."
        )
            
    try:
        typo_ids = None
        if typology_ids and typology_ids.strip():
            typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]
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
            typology_ids=typo_ids,
            duration_min_seconds=duration_min_seconds,
            duration_max_seconds=duration_max_seconds,
            avg_score_min=avg_score_min,
            avg_score_max=avg_score_max,
            context=context,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve agent performance evolution")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard/objections")
async def objections_breakdown(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="24h | 7d | 30d | 90d | all")] = "7d",
    agent_id: Annotated[str | None, Query(description="hubspot_owner_id")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Type of call")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
):
    """
    Get categorized objection lists, agent-specific counts,
    and a chronological list of calls that raised objections.
    """
    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]
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
            typology_ids=typo_ids,
            duration_min_seconds=duration_min_seconds,
            duration_max_seconds=duration_max_seconds,
            avg_score_min=avg_score_min,
            avg_score_max=avg_score_max,
            context=context,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve objections breakdown")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/me/evolution", response_model=AgentEvolutionResponse)
async def get_my_evolution(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    email: Annotated[str | None, Query(description="For backwards compatibility, ignored for agents")] = None,
    type: Annotated[str, Query(description="audio | text")] = "audio",
    period: Annotated[str, Query(description="24h | 7d | 30d | 90d | all")] = "30d",
    bucket: Annotated[str | None, Query(description="hour | day | week")] = None,
    prompt_version_id: Annotated[int | None, Query(description="Filter by prompt version")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
):
    """
    Get chronological performance evolution metrics specifically for the logged-in agent.
    """
    # Use context's normalized role and fields instead of legacy strings
    is_admin = context.is_super_admin or context.normalized_role == InternalRole.COMPANY_ADMIN
    
    # We resolve the email owner_id if it's admin, else we use context.allowed_agent_ids
    if is_admin:
        if email:
            owner_id = resolve_owner_id_by_email(email)
        else:
            # Fallback to current user's owner ID
            current_user = await db.get(User, context.user_id)
            owner_id = resolve_agent_owner_id(current_user) if current_user else None
        if not owner_id:
            raise HTTPException(
                status_code=400,
                detail="Debes especificar un agente válido (vía email u owner_id asignado)."
            )
    else:
        # For restricted roles (managers, coordinators, agents), we use their own assigned owner_id
        current_user = await db.get(User, context.user_id)
        owner_id = resolve_agent_owner_id(current_user) if current_user else None
        if not owner_id:
            raise HTTPException(
                status_code=403,
                detail="No hay agente asociado a este usuario."
            )
        # Check permissions explicitly
        if context.allowed_agent_ids is not None and owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=403,
                detail="No tienes permiso para consultar la evolución de este agente."
            )

    try:
        typo_ids = None
        if typology_ids and typology_ids.strip():
            typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]
        data = await get_agent_evolution(
            db,
            hubspot_owner_id=owner_id,
            analysis_type=type,
            period=period,
            bucket_param=bucket,
            prompt_version_id=prompt_version_id,
            service_id=service_id,
            service_key=service_key,
            date_from=date_from,
            date_to=date_to,
            typology_ids=typo_ids,
            duration_min_seconds=duration_min_seconds,
            duration_max_seconds=duration_max_seconds,
            avg_score_min=avg_score_min,
            avg_score_max=avg_score_max,
            context=context,
        )
        return data
    except Exception as e:
        logger.exception("Failed to retrieve logged-in agent performance evolution")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/me/agent")
async def get_my_agent_details(
    current_user: Annotated[User, Depends(get_current_user)],
    email: Annotated[str | None, Query(description="For backwards compatibility, ignored for agents")] = None,
):
    """
    Verify and retrieve details of the agent associated with the logged-in user.
    """
    normalized_role = (current_user.role or "").strip().lower()
    is_admin = normalized_role in {"admin", "administrador"}
    is_agent = normalized_role in {"agent", "agente"}

    if not is_admin and not is_agent:
        raise HTTPException(
            status_code=403,
            detail="No autorizado para este rol."
        )

    if is_admin and email:
        owner_id = resolve_owner_id_by_email(email)
    else: # is_agent
        owner_id = resolve_agent_owner_id(current_user)
        
    if not owner_id:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "status": "not_found",
                "error_message": "No hay agente asociado a este usuario."
            }
        )

    agent_name = resolve_owner_name(owner_id) or owner_id
    effective_email = email if (is_admin and email) else current_user.email
    return {
        "ok": True,
        "email": effective_email.strip().lower(),
        "hubspot_owner_id": owner_id,
        "agent_name": agent_name
    }



@router.get("/dashboard/latest-analyses/{identifier}")
async def get_latest_analysis_detail(
    identifier: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
):
    """
    Get the full detail of a single MassEvaluationResult by ID or call_id.
    """
    try:
        data = await get_mass_result_detail(db, identifier, context=context)
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

