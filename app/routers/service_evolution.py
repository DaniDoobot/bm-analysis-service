"""FastAPI router for Service Evolution dashboard."""
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
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
    company_id: int | None = Query(None, description="Filtrar por empresa"),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve all active services with unique evaluated calls counts and date bounds.
    Useful for populating service selectors.
    """
    if company_id is not None and not context.is_super_admin:
        if company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado a otra empresa."
            )
    eff_company_id = company_id if company_id is not None else (None if context.is_super_admin else context.company_id)

    norm_status = normalize_status(status or result_status)
    try:
        return await ServiceEvolutionService.get_services(db, date_from=date_from, date_to=date_to, status=norm_status, context=context, company_id=eff_company_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching services for evolution dashboard: %s", e, exc_info=True)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno del servidor al recuperar servicios."
        )


@router.get("/criteria", response_model=list[CriterionListItem])
async def get_criteria(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: int | None = Query(None, description="Filtrar criterios aplicados a un servicio específico"),
    service_key: str | None = Query(None, description="Filtrar criterios por clave del servicio"),
    service: str | None = Query(None, description="Filtrar criterios por ID o clave del servicio"),
    date_from: str | None = Query(None, description="Fecha de inicio (ISO 8601 o YYYY-MM-DD) para filtrar recuento de criterios"),
    date_to: str | None = Query(None, description="Fecha de fin (ISO 8601 o YYYY-MM-DD) para filtrar recuento de criterios"),
    status: str | None = Query(None, description="Filter by evaluation status: completed | failed | all"),
    result_status: str | None = Query(None, description="Alias for status"),
    team_id: int | None = Query(None, description="Filtrar por equipo"),
    company_id: int | None = Query(None, description="Filtrar por empresa"),
    company_key: str | None = Query(None, description="Filtrar por clave de empresa"),
    company: str | None = Query(None, description="Filtrar por ID o slug de empresa"),
    typology_id: int | None = Query(None, description="Filtrar por ID de tipología"),
    typology_key: str | None = Query(None, description="Filtrar por clave de tipología"),
    typology: str | None = Query(None, description="Filtrar por clave o nombre de tipología"),
    tipo_llamada: str | None = Query(None, description="Alias para tipología"),
    call_type: str | None = Query(None, description="Alias para tipología"),
    agent_id: str | None = Query(None, description="Filtrar por agente"),
    agent_owner_id: str | None = Query(None, description="Filtrar por ID de HubSpot del agente"),
    hubspot_owner_id: str | None = Query(None, description="Filtrar por ID de HubSpot del agente"),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve available criteria keys with counts of applicable entries.
    Useful for selecting criteria to graph/analyze.
    """
    def _extract_val(val, default=None):
        if val is None or hasattr(val, "default"):
            return default if val is None else (getattr(val, "default", default) if getattr(val, "default", None) is not Ellipsis else default)
        return val

    c_id = _extract_val(company_id)
    c_key = _extract_val(company_key)
    raw_comp = _extract_val(company)
    t_id = _extract_val(team_id)
    s_id = _extract_val(service_id)
    s_key = _extract_val(service_key)
    raw_svc = _extract_val(service)
    d_from = _extract_val(date_from)
    d_to = _extract_val(date_to)
    raw_st = _extract_val(status) or _extract_val(result_status)

    from app.utils.service_resolvers import resolve_company_id, resolve_service_id
    eff_company_id = await resolve_company_id(db, company_id=c_id, company_key=c_key, company_param=raw_comp)
    if eff_company_id is None and not context.is_super_admin:
        eff_company_id = context.company_id

    eff_service_id = s_id
    if eff_service_id is None and (raw_svc or s_key):
        resolved_id, _ = await resolve_service_id(
            db,
            service_key=s_key,
            service_param=raw_svc,
            company_ids=[eff_company_id] if eff_company_id is not None else (None if context.is_super_admin else context.allowed_company_ids)
        )
        if resolved_id is not None:
            eff_service_id = resolved_id

    # Deduce company from service if not specified
    if eff_company_id is None and eff_service_id is not None:
        from app.models.services import Service
        svc_row = await db.get(Service, eff_service_id)
        if svc_row and svc_row.company_id:
            eff_company_id = svc_row.company_id

    if eff_company_id is not None and not context.is_super_admin:
        if eff_company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado a otra empresa."
            )

    raw_typo = _extract_val(typology_key) or _extract_val(typology) or _extract_val(tipo_llamada) or _extract_val(call_type)
    eff_typology_id = _extract_val(typology_id)
    eff_typology_key = str(raw_typo).strip() if raw_typo else None

    if eff_typology_id is None and eff_typology_key and eff_typology_key.lower() not in ("all", "todas", "total", "*"):
        from app.models.typologies import Typology
        from sqlalchemy import select, or_
        stmt_t = select(Typology).where(Typology.typology_key == eff_typology_key, Typology.is_active == True)
        if eff_service_id is not None:
            stmt_t = stmt_t.where(Typology.service_id == eff_service_id)
        if eff_company_id is not None:
            if eff_company_id == 1:
                stmt_t = stmt_t.where(or_(Typology.company_id == 1, Typology.company_id.is_(None)))
            else:
                stmt_t = stmt_t.where(Typology.company_id == eff_company_id)
        res_t = await db.execute(stmt_t)
        t_match = res_t.scalars().first()
        if t_match:
            eff_typology_id = t_match.typology_id

    if eff_typology_id is not None and not eff_typology_key:
        from app.models.typologies import Typology
        from sqlalchemy import select
        res_tk = await db.execute(select(Typology.typology_key).where(Typology.typology_id == eff_typology_id))
        eff_typology_key = res_tk.scalar_one_or_none()

    clean_agent_id = str(_extract_val(hubspot_owner_id) or _extract_val(agent_owner_id) or _extract_val(agent_id)).strip() if (_extract_val(hubspot_owner_id) or _extract_val(agent_owner_id) or _extract_val(agent_id)) else None
    norm_status = normalize_status(raw_st)

    try:
        return await ServiceEvolutionService.get_criteria(
            db,
            service_id=eff_service_id,
            date_from=d_from,
            date_to=d_to,
            status=norm_status,
            context=context,
            team_id=t_id,
            company_id=eff_company_id,
            agent_owner_id=clean_agent_id,
            typology_id=eff_typology_id,
            typology_key=eff_typology_key,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching criteria for evolution dashboard: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Error interno del servidor al recuperar criterios."
        )


@router.get("", response_model=ServiceEvolutionResponse)
@router.get("/", response_model=ServiceEvolutionResponse, include_in_schema=False)
async def get_evolution(
    context: Annotated[TenantContext, Depends(get_tenant_context)],
    service_id: int | None = Query(None, description="Filtrar por ID del servicio"),
    service_key: str | None = Query(None, description="Filtrar por clave del servicio"),
    service: str | None = Query(None, description="Filtrar por ID o clave del servicio"),
    date_from: str | None = Query(None, description="Fecha de inicio (ISO 8601 o YYYY-MM-DD)"),
    date_to: str | None = Query(None, description="Fecha de fin (ISO 8601 o YYYY-MM-DD)"),
    granularity: Annotated[str, Query(description="Granularidad de agrupación: auto | hour | day | week | month")] = "auto",
    bucket: Annotated[str | None, Query(description="Alias para granularity")] = None,
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
    team_id: int | None = Query(None, description="Filtrar por equipo"),
    company_id: int | None = Query(None, description="Filtrar por empresa"),
    company_key: str | None = Query(None, description="Filtrar por clave de empresa"),
    company: str | None = Query(None, description="Filtrar por ID o slug de empresa"),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve main dashboard KPIs and daily/weekly/monthly evolution series for a given service.
    If no service filter is set, retrieves all services combined or unclassified.
    """
    # Validation: granularity
    effective_gran = bucket or granularity or "auto"
    granularity_val = getattr(effective_gran, "default", effective_gran) if not isinstance(effective_gran, str) else effective_gran
    granularity_str = str(granularity_val).lower().strip() if granularity_val else "auto"
    valid_granularities = {"auto", "hour", "day", "week", "month"}
    if granularity_str not in valid_granularities:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"La granularidad '{granularity_val}' no es válida. Use: auto | hour | day | week | month"
        )

    def _extract_val(val, default=None):
        if val is None or hasattr(val, "default"):
            return default if val is None else (getattr(val, "default", default) if getattr(val, "default", None) is not Ellipsis else default)
        return val

    s_id = _extract_val(service_id)
    s_key = _extract_val(service_key)
    raw_svc = _extract_val(service)
    d_from = _extract_val(date_from)
    d_to = _extract_val(date_to)
    ag_owner = _extract_val(agent_owner_id)
    crit = _extract_val(criteria)
    t_ids_raw = _extract_val(typology_ids)
    dur_min = _extract_val(duration_min_seconds)
    dur_max = _extract_val(duration_max_seconds)
    sc_min = _extract_val(avg_score_min)
    sc_max = _extract_val(avg_score_max)

    c_id = _extract_val(company_id)
    c_key = _extract_val(company_key)
    raw_comp = _extract_val(company)
    t_id = _extract_val(team_id)

    from app.utils.service_resolvers import resolve_company_id, resolve_service_id
    eff_company_id = await resolve_company_id(db, company_id=c_id, company_key=c_key, company_param=raw_comp)
    if eff_company_id is None and not context.is_super_admin:
        eff_company_id = context.company_id

    if raw_svc is not None and s_id is None and s_key is None:
        resolved_s_id, resolved_s_key = await resolve_service_id(
            db,
            service_param=str(raw_svc),
            company_ids=[eff_company_id] if eff_company_id is not None else (None if context.is_super_admin else context.allowed_company_ids)
        )
        if resolved_s_id is not None:
            s_id = resolved_s_id
            s_key = resolved_s_key

    # Deduce company from service if not yet known
    if eff_company_id is None and s_id is not None:
        from app.models.services import Service
        svc_row = await db.get(Service, s_id)
        if svc_row and svc_row.company_id:
            eff_company_id = svc_row.company_id

    if eff_company_id is not None and not context.is_super_admin:
        if eff_company_id not in context.allowed_company_ids:
            raise HTTPException(
                status_code=403,
                detail="Acceso denegado a otra empresa."
            )


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

    if ag_owner and str(ag_owner).strip().isdigit():
        from app.utils.agent_resolvers import resolve_agent_identifiers_to_owner_ids
        resolved = await resolve_agent_identifiers_to_owner_ids(db, [ag_owner], company_id=eff_company_id)
        if resolved:
            ag_owner = resolved[0]

    if context.allowed_agent_ids is not None:
        if ag_owner:
            if ag_owner not in context.allowed_agent_ids and str(_extract_val(agent_owner_id)).strip() not in context.allowed_agent_ids:
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
            team_id=t_id,
            company_id=eff_company_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error generating service evolution: %s", e, exc_info=True)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno del servidor al generar la evolución del servicio."
        )
