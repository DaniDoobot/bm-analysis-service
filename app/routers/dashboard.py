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
from app.utils.normalizers import normalize_typology, normalize_direction, normalize_status

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
    service: Annotated[str | None, Query(description="Filter by service ID, key, or slug name")] = None,
    hubspot_owner_ids: Annotated[str | None, Query(description="Comma-separated HubSpot owner IDs")] = None,
    hubspot_owner_id: Annotated[str | None, Query(description="Filter by HubSpot owner ID")] = None,
    agent_id: Annotated[str | None, Query(description="Alias for hubspot_owner_id")] = None,
    agent_owner_id: Annotated[str | None, Query(description="Alias for hubspot_owner_id")] = None,
    agent: Annotated[str | None, Query(description="Alias for hubspot_owner_id")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    typology: Annotated[str | None, Query(description="Filter by typology key/name")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Filter by call type")] = None,
    call_type: Annotated[str | None, Query(description="Filter by call type")] = None,
    selected_typology: Annotated[str | None, Query(description="Filter by selected typology")] = None,
    typologies: Annotated[str | None, Query(description="Filter by typology")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_min: Annotated[int | None, Query(description="Alias for duration_min_seconds")] = None,
    min_duration: Annotated[int | None, Query(description="Alias for duration_min_seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    duration_max: Annotated[int | None, Query(description="Alias for duration_max_seconds")] = None,
    max_duration: Annotated[int | None, Query(description="Alias for duration_max_seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    score_min: Annotated[float | None, Query(description="Alias for avg_score_min")] = None,
    eval_min: Annotated[float | None, Query(description="Alias for avg_score_min")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
    score_max: Annotated[float | None, Query(description="Alias for avg_score_max")] = None,
    eval_max: Annotated[float | None, Query(description="Alias for avg_score_max")] = None,
    status: Annotated[str | None, Query(description="Filter by evaluation status: completed | failed | all")] = None,
    result_status: Annotated[str | None, Query(description="Alias for status")] = None,
    item_filters: Annotated[str | None, Query(description="JSON url-encoded item score filters")] = None,
    criterion_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    item_score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
):
    """
    Get dashboard summary metrics including KPIs, evolution charts,
    agent rankings, and latest analyses.
    """
    effective_item_filters = item_filters or criterion_filters or score_filters or item_score_filters
    raw_status = status or result_status
    norm_status = normalize_status(raw_status)

    if service and not service_id and not service_key:
        from app.utils.service_resolvers import resolve_service_id
        resolved_id, resolved_key = await resolve_service_id(db, service_param=service)
        service_id = resolved_id or service_id
        service_key = resolved_key or service_key

    raw_owner_id = hubspot_owner_id or agent_id or agent_owner_id or agent
    owner_ids = None
    if hubspot_owner_ids and hubspot_owner_ids.strip():
        owner_ids = [oid.strip() for oid in hubspot_owner_ids.split(",") if oid.strip()]
    elif raw_owner_id:
        owner_ids = [raw_owner_id.strip()]

    eff_score_min = avg_score_min if avg_score_min is not None else (score_min if score_min is not None else eval_min)
    eff_score_max = avg_score_max if avg_score_max is not None else (score_max if score_max is not None else eval_max)
    eff_dur_min = duration_min_seconds if duration_min_seconds is not None else (duration_min if duration_min is not None else min_duration)
    eff_dur_max = duration_max_seconds if duration_max_seconds is not None else (duration_max if duration_max is not None else max_duration)

    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

    raw_typology = typology or typology_key or tipo_llamada or call_type or selected_typology or typologies
    norm_typology_key = normalize_typology(raw_typology)
    analysis_type = type

    if type and type not in ("audio", "text") and not raw_typology:
        norm_typology_key = normalize_typology(type)
        analysis_type = "audio"

    raw_direction = direction or call_direction or inbound_outbound
    norm_direction = normalize_direction(raw_direction)

    if service_id is not None and not context.is_super_admin:
        if context.allowed_service_ids is not None and service_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado: No tienes permisos para este servicio."
            )

    from app.utils.memory_utils import track_memory_async

    try:
        async with track_memory_async("dashboard_summary"):
            data = await get_dashboard_summary(
                db,
                analysis_type=analysis_type,
                period=period,
                service_id=service_id,
                service_key=service_key,
                date_from=date_from,
                date_to=date_to,
                typology_ids=typo_ids,
                typology_key=norm_typology_key,
                direction=norm_direction,
                duration_min_seconds=eff_dur_min,
                duration_max_seconds=eff_dur_max,
                avg_score_min=eff_score_min,
                avg_score_max=eff_score_max,
                hubspot_owner_id=raw_owner_id,
                hubspot_owner_ids=owner_ids,
                item_filters=effective_item_filters,
                status=norm_status,
                context=context,
            )
            return data
    except HTTPException:
        raise
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
    typology: Annotated[str | None, Query(description="Filter by typology key/name")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Filter by call type")] = None,
    call_type: Annotated[str | None, Query(description="Filter by call type")] = None,
    selected_typology: Annotated[str | None, Query(description="Filter by selected typology")] = None,
    typologies: Annotated[str | None, Query(description="Filter by typology")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
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
    status: Annotated[str | None, Query(description="Filter by evaluation status: completed | failed | all")] = None,
    result_status: Annotated[str | None, Query(description="Alias for status")] = None,
    item_filters: Annotated[str | None, Query(description="JSON url-encoded item score/boolean filters")] = None,
    criterion_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    item_score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
):
    """
    Get multi-agent comparison analytics for dashboard reporting.
    """
    effective_item_filters = item_filters or criterion_filters or score_filters or item_score_filters
    raw_status = status or result_status
    norm_status = normalize_status(raw_status)
    owner_ids = None
    if hubspot_owner_ids and hubspot_owner_ids.strip():
        owner_ids = [oid.strip() for oid in hubspot_owner_ids.split(",") if oid.strip()]
        
    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

    raw_typology = typology or typology_key or tipo_llamada or call_type or selected_typology or typologies
    norm_typology_key = normalize_typology(raw_typology)
    raw_direction = direction or call_direction or inbound_outbound
    norm_direction = normalize_direction(raw_direction)

    try:
        data = await get_agents_comparison(
            db,
            hubspot_owner_ids=owner_ids,
            service_id=service_id,
            service_key=service_key,
            typology_id=typology_id,
            typology_key=norm_typology_key,
            direction=norm_direction,
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
            item_filters=effective_item_filters,
            status=norm_status,
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
    service: Annotated[str | None, Query(description="Filter by service ID, key, or slug name")] = None,
    period: Annotated[str | None, Query(description="Filter by period (e.g., 24h, 7d, 30d)")] = None,
    date_from: Annotated[str | None, Query(description="Start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="End date (ISO or YYYY-MM-DD)")] = None,
    type: Annotated[str | None, Query(description="Filter by analysis type (kept for compatibility)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    typology: Annotated[str | None, Query(description="Filter by typology key/name")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Filter by call type")] = None,
    call_type: Annotated[str | None, Query(description="Filter by call type")] = None,
    selected_typology: Annotated[str | None, Query(description="Filter by selected typology")] = None,
    typologies: Annotated[str | None, Query(description="Filter by typology")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
    duration_min_seconds: Annotated[int | None, Query(alias="duration_min", description="Min duration in seconds")] = None,
    min_duration: Annotated[int | None, Query(description="Min duration in seconds (alias)")] = None,
    duration_max_seconds: Annotated[int | None, Query(alias="duration_max", description="Max duration in seconds")] = None,
    max_duration: Annotated[int | None, Query(description="Max duration in seconds (alias)")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    score_min: Annotated[float | None, Query(description="Alias for avg_score_min")] = None,
    eval_min: Annotated[float | None, Query(description="Alias for avg_score_min")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
    score_max: Annotated[float | None, Query(description="Alias for avg_score_max")] = None,
    eval_max: Annotated[float | None, Query(description="Alias for avg_score_max")] = None,
    status: Annotated[str | None, Query(description="Filter by evaluation status: completed | failed | all")] = None,
    result_status: Annotated[str | None, Query(description="Alias for status")] = None,
    item_filters: Annotated[str | None, Query(description="JSON url-encoded item score/boolean filters")] = None,
    criterion_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    item_score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
):
    """
    Get all active call center agents with their accumulated real metrics.
    """
    effective_item_filters = item_filters or criterion_filters or score_filters or item_score_filters
    raw_status = status or result_status
    norm_status = normalize_status(raw_status)
    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

    raw_typology = typology or typology_key or tipo_llamada or call_type or selected_typology or typologies
    norm_typology_key = normalize_typology(raw_typology)

    if type and type not in ("audio", "text") and not raw_typology:
        norm_typology_key = normalize_typology(type)
        type = None

    raw_direction = direction or call_direction or inbound_outbound
    norm_direction = normalize_direction(raw_direction)

    dur_min = duration_min_seconds if duration_min_seconds is not None else min_duration
    dur_max = duration_max_seconds if duration_max_seconds is not None else max_duration
    sc_min = avg_score_min if avg_score_min is not None else (score_min if score_min is not None else eval_min)
    sc_max = avg_score_max if avg_score_max is not None else (score_max if score_max is not None else eval_max)

    try:
        data = await get_agents_list(
            db,
            service_id=service_id,
            service_key=service_key,
            service=service,
            period=period,
            date_from=date_from,
            date_to=date_to,
            type=type,
            typology_ids=typo_ids,
            typology_key=norm_typology_key,
            direction=norm_direction,
            duration_min_seconds=dur_min,
            duration_max_seconds=dur_max,
            avg_score_min=sc_min,
            avg_score_max=sc_max,
            item_filters=effective_item_filters,
            status=norm_status,
            context=context,
        )
        return data
    except HTTPException:
        raise
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
    service: Annotated[str | None, Query(description="Filter by service ID, key, or slug name")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    typology: Annotated[str | None, Query(description="Filter by typology key/name")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Filter by call type")] = None,
    call_type: Annotated[str | None, Query(description="Filter by call type")] = None,
    selected_typology: Annotated[str | None, Query(description="Filter by selected typology")] = None,
    typologies: Annotated[str | None, Query(description="Filter by typology")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
    duration_min_seconds: Annotated[int | None, Query(alias="duration_min", description="Min duration in seconds")] = None,
    min_duration: Annotated[int | None, Query(description="Min duration in seconds (alias)")] = None,
    duration_max_seconds: Annotated[int | None, Query(alias="duration_max", description="Max duration in seconds")] = None,
    max_duration: Annotated[int | None, Query(description="Max duration in seconds (alias)")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    score_min: Annotated[float | None, Query(description="Alias for avg_score_min")] = None,
    eval_min: Annotated[float | None, Query(description="Alias for avg_score_min")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
    score_max: Annotated[float | None, Query(description="Alias for avg_score_max")] = None,
    eval_max: Annotated[float | None, Query(description="Alias for avg_score_max")] = None,
    status: Annotated[str | None, Query(description="Filter by evaluation status: completed | failed | all")] = None,
    result_status: Annotated[str | None, Query(description="Alias for status")] = None,
    item_filters: Annotated[str | None, Query(description="JSON url-encoded item score/boolean filters")] = None,
    criterion_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    item_score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
):
    """
    Get chronological performance, trends, strengths, weaknesses,
    and evolution timelines for a specific agent.
    """
    effective_item_filters = item_filters or criterion_filters or score_filters or item_score_filters
    raw_status = status or result_status
    norm_status = normalize_status(raw_status)
    if context.allowed_agent_ids is not None and hubspot_owner_id not in context.allowed_agent_ids:
        raise HTTPException(
            status_code=403,
            detail="No tienes permiso para consultar la evolución de este agente."
        )
            
    try:
        typo_ids = None
        if typology_ids and typology_ids.strip():
            typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

        raw_typology = typology or typology_key or tipo_llamada or call_type or selected_typology or typologies
        norm_typology_key = normalize_typology(raw_typology)
        analysis_type = type

        if type and type not in ("audio", "text") and not raw_typology:
            norm_typology_key = normalize_typology(type)
            analysis_type = "audio"

        raw_direction = direction or call_direction or inbound_outbound
        norm_direction = normalize_direction(raw_direction)

        dur_min = duration_min_seconds if duration_min_seconds is not None else min_duration
        dur_max = duration_max_seconds if duration_max_seconds is not None else max_duration
        sc_min = avg_score_min if avg_score_min is not None else (score_min if score_min is not None else eval_min)
        sc_max = avg_score_max if avg_score_max is not None else (score_max if score_max is not None else eval_max)

        data = await get_agent_evolution(
            db,
            hubspot_owner_id=hubspot_owner_id,
            analysis_type=analysis_type,
            period=period,
            bucket_param=bucket,
            prompt_version_id=prompt_version_id,
            service_id=service_id,
            service_key=service_key,
            service=service,
            date_from=date_from,
            date_to=date_to,
            typology_ids=typo_ids,
            typology_key=norm_typology_key,
            direction=norm_direction,
            duration_min_seconds=dur_min,
            duration_max_seconds=dur_max,
            avg_score_min=sc_min,
            avg_score_max=sc_max,
            item_filters=effective_item_filters,
            status=norm_status,
            context=context,
        )
        return data
    except HTTPException:
        raise
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
    typology: Annotated[str | None, Query(description="Filter by typology key/name")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    call_type: Annotated[str | None, Query(description="Filter by call type")] = None,
    selected_typology: Annotated[str | None, Query(description="Filter by selected typology")] = None,
    typologies: Annotated[str | None, Query(description="Filter by typology")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
    service_id: Annotated[int | None, Query(description="Filter by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter by service key")] = None,
    date_from: Annotated[str | None, Query(description="Custom start date (ISO or YYYY-MM-DD)")] = None,
    date_to: Annotated[str | None, Query(description="Custom end date (ISO or YYYY-MM-DD)")] = None,
    typology_ids: Annotated[str | None, Query(description="Comma-separated typology IDs")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
    status: Annotated[str | None, Query(description="Filter by evaluation status: completed | failed | all")] = None,
    result_status: Annotated[str | None, Query(description="Alias for status")] = None,
    item_filters: Annotated[str | None, Query(description="JSON url-encoded item score/boolean filters")] = None,
    criterion_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    item_score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
):
    """
    Get categorized objection lists, agent-specific counts,
    and a chronological list of calls that raised objections.
    """
    effective_item_filters = item_filters or criterion_filters or score_filters or item_score_filters
    raw_status = status or result_status
    norm_status = normalize_status(raw_status)
    typo_ids = None
    if typology_ids and typology_ids.strip():
        typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

    raw_typology = typology or typology_key or tipo_llamada or call_type or selected_typology or typologies
    norm_typology_key = normalize_typology(raw_typology)
    analysis_type = type

    if type and type not in ("audio", "text") and not raw_typology:
        norm_typology_key = normalize_typology(type)
        analysis_type = "audio"

    raw_direction = direction or call_direction or inbound_outbound
    norm_direction = normalize_direction(raw_direction)

    try:
        data = await get_objections_breakdown(
            db,
            analysis_type=analysis_type,
            period=period,
            agent_id=agent_id,
            tipo_llamada=norm_typology_key,
            service_id=service_id,
            service_key=service_key,
            date_from=date_from,
            date_to=date_to,
            typology_ids=typo_ids,
            typology_key=norm_typology_key,
            direction=norm_direction,
            duration_min_seconds=duration_min_seconds,
            duration_max_seconds=duration_max_seconds,
            avg_score_min=avg_score_min,
            avg_score_max=avg_score_max,
            item_filters=effective_item_filters,
            status=norm_status,
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
    typology: Annotated[str | None, Query(description="Filter by typology key/name")] = None,
    typology_key: Annotated[str | None, Query(description="Filter by typology key")] = None,
    tipo_llamada: Annotated[str | None, Query(description="Filter by call type")] = None,
    call_type: Annotated[str | None, Query(description="Filter by call type")] = None,
    selected_typology: Annotated[str | None, Query(description="Filter by selected typology")] = None,
    typologies: Annotated[str | None, Query(description="Filter by typology")] = None,
    direction: Annotated[str | None, Query(description="all | inbound | outbound")] = None,
    call_direction: Annotated[str | None, Query(description="Filter by call direction")] = None,
    inbound_outbound: Annotated[str | None, Query(description="Filter by inbound/outbound")] = None,
    duration_min_seconds: Annotated[int | None, Query(description="Min duration in seconds")] = None,
    duration_max_seconds: Annotated[int | None, Query(description="Max duration in seconds")] = None,
    avg_score_min: Annotated[float | None, Query(description="Min average score")] = None,
    score_min: Annotated[float | None, Query(description="Alias for avg_score_min")] = None,
    eval_min: Annotated[float | None, Query(description="Alias for avg_score_min")] = None,
    avg_score_max: Annotated[float | None, Query(description="Max average score")] = None,
    score_max: Annotated[float | None, Query(description="Alias for avg_score_max")] = None,
    eval_max: Annotated[float | None, Query(description="Alias for avg_score_max")] = None,
    status: Annotated[str | None, Query(description="Filter by evaluation status: completed | failed | all")] = None,
    result_status: Annotated[str | None, Query(description="Alias for status")] = None,
    item_filters: Annotated[str | None, Query(description="JSON url-encoded item score/boolean filters")] = None,
    criterion_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    item_score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
):
    """
    Get chronological performance evolution metrics specifically for the logged-in agent.
    """
    effective_item_filters = item_filters or criterion_filters or score_filters or item_score_filters
    raw_status = status or result_status
    norm_status = normalize_status(raw_status)
    # Use context's normalized role and fields instead of legacy strings
    is_manager_or_admin = (
        context.is_super_admin or
        context.normalized_role in (InternalRole.COMPANY_ADMIN, InternalRole.SERVICE_MANAGER, InternalRole.TEAM_COORDINATOR)
    )
    
    if is_manager_or_admin and email:
        owner_id = resolve_owner_id_by_email(email)
        if not owner_id:
            raise HTTPException(
                status_code=400,
                detail="Debes especificar un agente válido (vía email u owner_id asignado)."
            )
        if context.allowed_agent_ids is not None and owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=403,
                detail="No tienes permiso para consultar la evolución de este agente."
            )
    else:
        # Fallback to current user's owner ID
        current_user = await db.get(User, context.user_id)
        owner_id = resolve_agent_owner_id(current_user) if current_user else None
        if not owner_id:
            raise HTTPException(
                status_code=403,
                detail="No hay agente asociado a este usuario."
            )
        if context.allowed_agent_ids is not None and owner_id not in context.allowed_agent_ids:
            raise HTTPException(
                status_code=403,
                detail="No tienes permiso para consultar la evolución de este agente."
            )

    try:
        typo_ids = None
        if typology_ids and typology_ids.strip():
            typo_ids = [int(tid.strip()) for tid in typology_ids.split(",") if tid.strip().isdigit()]

        raw_typology = typology or typology_key or tipo_llamada or call_type or selected_typology or typologies
        norm_typology_key = normalize_typology(raw_typology)
        analysis_type = type

        if type and type not in ("audio", "text") and not raw_typology:
            norm_typology_key = normalize_typology(type)
            analysis_type = "audio"

        raw_direction = direction or call_direction or inbound_outbound
        norm_direction = normalize_direction(raw_direction)

        sc_min = avg_score_min if avg_score_min is not None else (score_min if score_min is not None else eval_min)
        sc_max = avg_score_max if avg_score_max is not None else (score_max if score_max is not None else eval_max)

        data = await get_agent_evolution(
            db,
            hubspot_owner_id=owner_id,
            analysis_type=analysis_type,
            period=period,
            bucket_param=bucket,
            prompt_version_id=prompt_version_id,
            service_id=service_id,
            service_key=service_key,
            date_from=date_from,
            date_to=date_to,
            typology_ids=typo_ids,
            typology_key=norm_typology_key,
            direction=norm_direction,
            duration_min_seconds=duration_min_seconds,
            duration_max_seconds=duration_max_seconds,
            avg_score_min=sc_min,
            avg_score_max=sc_max,
            item_filters=effective_item_filters,
            status=norm_status,
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


@router.get("/evaluation-items/filter-options")
@router.get("/criteria/filter-options")
async def get_evaluation_items_filter_options(
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: Annotated[int | None, Query(description="Filter criteria by service ID")] = None,
    service_key: Annotated[str | None, Query(description="Filter criteria by service key")] = None,
    service: Annotated[str | None, Query(description="Filter criteria by service key, slug, or ID")] = None,
):
    """
    Retrieve available evaluation criteria item filter options dynamically for frontend UI.
    """
    from app.utils.item_score_filters import get_evaluation_item_filter_options
    from app.utils.service_resolvers import resolve_service_id

    eff_service_id, _ = await resolve_service_id(
        db,
        service_id=service_id,
        service_key=service_key,
        service_param=service,
        company_ids=None if context.is_super_admin else context.allowed_company_ids
    )

    eff_service_ids = [eff_service_id] if eff_service_id is not None else context.allowed_service_ids

    options = await get_evaluation_item_filter_options(
        db,
        company_ids=context.allowed_company_ids,
        service_ids=eff_service_ids
    )
    return {"items": options}


