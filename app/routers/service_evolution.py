"""FastAPI router for Service Evolution dashboard."""
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_tenant_context
from app.core.tenant_context import TenantContext
from app.schemas.service_evolution import (
    ServiceEvolutionResponse,
    ServiceListItem,
    CriterionListItem,
)
from app.services.service_evolution_service import ServiceEvolutionService
from app.utils.normalizers import normalize_typology, normalize_direction, normalize_status
from app.utils.item_score_filters import parse_item_score_filters_detailed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bm/service-evolution", tags=["Service Evolution"])


@router.get("/services", response_model=list[ServiceListItem])
async def get_services(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    date_from: str | None = Query(None, description="Fecha de inicio (ISO 8601 o YYYY-MM-DD) para filtrar recuento de llamadas"),
    date_to: str | None = Query(None, description="Fecha de fin (ISO 8601 o YYYY-MM-DD) para filtrar recuento de llamadas"),
    status: str | None = Query(None, description="Filter by evaluation status: completed | failed | all"),
    result_status: str | None = Query(None, description="Alias for status"),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve all active services with unique evaluated calls counts and date bounds.
    Useful for populating service selectors.
    """
    norm_status = normalize_status(status or result_status)
    try:
        return await ServiceEvolutionService.get_services(db, date_from=date_from, date_to=date_to, status=norm_status, context=context)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching services for evolution dashboard: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR if hasattr(status, "HTTP_500_INTERNAL_SERVER_ERROR") else 500,
            detail="Error interno del servidor al recuperar servicios."
        )


@router.get("/criteria", response_model=list[CriterionListItem])
async def get_criteria(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: int | None = Query(None, description="Filtrar criterios aplicados a un servicio específico"),
    date_from: str | None = Query(None, description="Fecha de inicio (ISO 8601 o YYYY-MM-DD) para filtrar recuento de criterios"),
    date_to: str | None = Query(None, description="Fecha de fin (ISO 8601 o YYYY-MM-DD) para filtrar recuento de criterios"),
    status: str | None = Query(None, description="Filter by evaluation status: completed | failed | all"),
    result_status: str | None = Query(None, description="Alias for status"),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve available criteria keys with counts of applicable entries.
    Useful for selecting criteria to graph/analyze.
    """
    norm_status = normalize_status(status or result_status)
    try:
        return await ServiceEvolutionService.get_criteria(db, service_id=service_id, date_from=date_from, date_to=date_to, status=norm_status, context=context)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching criteria for evolution dashboard: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Error interno del servidor al recuperar criterios."
        )


@router.get("", response_model=ServiceEvolutionResponse)
async def get_evolution(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: int | None = Query(None, description="Filtrar por ID del servicio"),
    service_key: str | None = Query(None, description="Filtrar por clave del servicio"),
    date_from: str | None = Query(None, description="Fecha de inicio (ISO 8601 o YYYY-MM-DD)"),
    date_to: str | None = Query(None, description="Fecha de fin (ISO 8601 o YYYY-MM-DD)"),
    granularity: Annotated[str, Query(description="Granularidad de agrupación: day | week | month")] = "day",
    typology_key: str | None = Query(None, description="Filtrar por clave de tipología"),
    typology: str | None = Query(None, description="Filtrar por clave/nombre de tipología"),
    tipo_llamada: str | None = Query(None, description="Filtrar por tipo de llamada"),
    call_type: str | None = Query(None, description="Filtrar por tipo de llamada"),
    selected_typology: str | None = Query(None, description="Filtrar por tipología seleccionada"),
    typologies: str | None = Query(None, description="Filtrar por tipología"),
    direction: str | None = Query(None, description="all | inbound | outbound"),
    call_direction: str | None = Query(None, description="Filtrar por dirección de llamada"),
    inbound_outbound: str | None = Query(None, description="Filtrar por dirección de llamada"),
    agent_owner_id: str | None = Query(None, description="Filtrar por ID de HubSpot del agente"),
    criteria: str | None = Query(None, description="Lista de criterion_key separados por comas a filtrar en el ranking"),
    typology_ids: str | None = Query(None, description="Comma-separated typology IDs to filter"),
    duration_min_seconds: int | None = Query(None, description="Min duration in seconds"),
    duration_max_seconds: int | None = Query(None, description="Max duration in seconds"),
    avg_score_min: float | None = Query(None, description="Min average score"),
    avg_score_max: float | None = Query(None, description="Max average score"),
    status: str | None = Query(None, description="Filter by evaluation status: completed | failed | all"),
    result_status: str | None = Query(None, description="Alias for status"),
    item_filters: Annotated[str | None, Query(description="JSON url-encoded item score/boolean filters")] = None,
    criterion_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    item_score_filters: Annotated[str | None, Query(description="Alias for item_filters")] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve main dashboard KPIs and daily/weekly/monthly evolution series for a given service.
    If no service filter is set, retrieves all services combined or unclassified.
    """
    # Validation: granularity
    valid_granularities = {"day", "week", "month"}
    granularity_val = getattr(granularity, "default", granularity) if not isinstance(granularity, str) else granularity
    granularity_str = str(granularity_val).lower() if granularity_val else "day"
    if granularity_str not in valid_granularities:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"La granularidad '{granularity_val}' no es válida. Use: day | week | month"
        )

    def _extract_val(val, default=None):
        if val is None or hasattr(val, "default"):
            return default if val is None else (getattr(val, "default", default) if getattr(val, "default", None) is not Ellipsis else default)
        return val

    s_id = _extract_val(service_id)
    s_key = _extract_val(service_key)
    d_from = _extract_val(date_from)
    d_to = _extract_val(date_to)
    ag_owner = _extract_val(agent_owner_id)
    crit = _extract_val(criteria)
    t_ids_raw = _extract_val(typology_ids)
    dur_min = _extract_val(duration_min_seconds)
    dur_max = _extract_val(duration_max_seconds)
    sc_min = _extract_val(avg_score_min)
    sc_max = _extract_val(avg_score_max)

    typo_ids = None
    if t_ids_raw and str(t_ids_raw).strip():
        typo_ids = [int(tid.strip()) for tid in str(t_ids_raw).split(",") if tid.strip().isdigit()]

    raw_typology = (
        _extract_val(typology)
        or _extract_val(typology_key)
        or _extract_val(tipo_llamada)
        or _extract_val(call_type)
        or _extract_val(selected_typology)
        or _extract_val(typologies)
    )
    norm_typology_key = normalize_typology(raw_typology)
    raw_direction = (
        _extract_val(direction)
        or _extract_val(call_direction)
        or _extract_val(inbound_outbound)
    )
    norm_direction = normalize_direction(raw_direction)

    if s_id is not None and not context.is_super_admin:
        if context.allowed_service_ids is not None and s_id not in context.allowed_service_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado: No tienes permisos para este servicio."
            )

    if context.allowed_agent_ids is not None:
        if ag_owner:
            if ag_owner not in context.allowed_agent_ids:
                raise HTTPException(
                    status_code=403,
                    detail="No tienes permiso para consultar la evolución de este agente."
                )

    raw_st = _extract_val(status) or _extract_val(result_status)
    norm_status = normalize_status(raw_st)

    effective_item_filters = None
    for cand in (item_filters, criterion_filters, score_filters, item_score_filters):
        extracted_cand = _extract_val(cand)
        if extracted_cand is not None:
            effective_item_filters = extracted_cand
            break

    parsed_item_filters = parse_item_score_filters_detailed(effective_item_filters)
    active_item_filters = parsed_item_filters.get("active_filters", [])

    try:
        return await ServiceEvolutionService.get_evolution(
            db,
            service_id=s_id,
            service_key=s_key,
            date_from=d_from,
            date_to=d_to,
            granularity=granularity_str,
            typology_key=norm_typology_key,
            direction=norm_direction,
            agent_owner_id=ag_owner,
            criteria=crit,
            typology_ids=typo_ids,
            duration_min_seconds=dur_min,
            duration_max_seconds=dur_max,
            avg_score_min=sc_min,
            avg_score_max=sc_max,
            status=norm_status,
            context=context,
            item_filters=active_item_filters,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error generating service evolution: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno del servidor al generar la evolución del servicio."
        )
