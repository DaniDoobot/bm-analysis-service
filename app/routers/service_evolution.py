"""FastAPI router for Service Evolution dashboard."""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.service_evolution import (
    ServiceEvolutionResponse,
    ServiceListItem,
    CriterionListItem,
)
from app.services.service_evolution_service import ServiceEvolutionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bm/service-evolution", tags=["Service Evolution"])


@router.get("/services", response_model=list[ServiceListItem])
async def get_services(
    date_from: str | None = Query(None, description="Fecha de inicio (ISO 8601 o YYYY-MM-DD) para filtrar recuento de llamadas"),
    date_to: str | None = Query(None, description="Fecha de fin (ISO 8601 o YYYY-MM-DD) para filtrar recuento de llamadas"),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve all active services with unique evaluated calls counts and date bounds.
    Useful for populating service selectors.
    """
    try:
        return await ServiceEvolutionService.get_services(db, date_from=date_from, date_to=date_to)
    except Exception as e:
        logger.error("Error fetching services for evolution dashboard: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno del servidor al recuperar servicios."
        )


@router.get("/criteria", response_model=list[CriterionListItem])
async def get_criteria(
    service_id: int | None = Query(None, description="Filtrar criterios aplicados a un servicio específico"),
    date_from: str | None = Query(None, description="Fecha de inicio (ISO 8601 o YYYY-MM-DD) para filtrar recuento de criterios"),
    date_to: str | None = Query(None, description="Fecha de fin (ISO 8601 o YYYY-MM-DD) para filtrar recuento de criterios"),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve available criteria keys with counts of applicable entries.
    Useful for selecting criteria to graph/analyze.
    """
    try:
        return await ServiceEvolutionService.get_criteria(db, service_id=service_id, date_from=date_from, date_to=date_to)
    except Exception as e:
        logger.error("Error fetching criteria for evolution dashboard: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno del servidor al recuperar criterios."
        )


@router.get("", response_model=ServiceEvolutionResponse)
async def get_evolution(
    service_id: int | None = Query(None, description="Filtrar por ID del servicio"),
    service_key: str | None = Query(None, description="Filtrar por clave del servicio"),
    date_from: str | None = Query(None, description="Fecha de inicio (ISO 8601 o YYYY-MM-DD)"),
    date_to: str | None = Query(None, description="Fecha de fin (ISO 8601 o YYYY-MM-DD)"),
    granularity: str = Query("day", description="Granularidad de agrupación: day | week | month"),
    typology_key: str | None = Query(None, description="Filtrar por clave de tipología"),
    agent_owner_id: str | None = Query(None, description="Filtrar por ID de HubSpot del agente"),
    criteria: str | None = Query(None, description="Lista de criterion_key separados por comas a filtrar en el ranking"),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve main dashboard KPIs and daily/weekly/monthly evolution series for a given service.
    If no service filter is set, retrieves all services combined or unclassified.
    """
    # Validation: granularity
    valid_granularities = {"day", "week", "month"}
    if granularity.lower() not in valid_granularities:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"La granularidad '{granularity}' no es válida. Use: day | week | month"
        )

    try:
        return await ServiceEvolutionService.get_evolution(
            db,
            service_id=service_id,
            service_key=service_key,
            date_from=date_from,
            date_to=date_to,
            granularity=granularity.lower(),
            typology_key=typology_key,
            agent_owner_id=agent_owner_id,
            criteria=criteria,
        )
    except Exception as e:
        logger.error("Error generating service evolution: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno del servidor al generar la evolución del servicio."
        )
