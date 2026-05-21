"""FastAPI router for Services."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.services import Service
from app.models.prompts import Prompt, PromptBaseStructure
from app.schemas.services import ServiceCreate, ServiceOut, ServiceUpdate

router = APIRouter(prefix="/bm/services", tags=["Services"])


@router.get("", response_model=list[ServiceOut])
async def list_services(db: AsyncSession = Depends(get_db)):
    """Retrieve all services, sorted by service_id."""
    stmt = select(Service).order_by(Service.service_id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{service_id}", response_model=ServiceOut)
async def get_service(service_id: int, db: AsyncSession = Depends(get_db)):
    """Retrieve details of a specific service."""
    stmt = select(Service).where(Service.service_id == service_id)
    result = await db.execute(stmt)
    service = result.scalars().first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Servicio con ID {service_id} no encontrado."
        )
    return service


@router.post("", response_model=ServiceOut, status_code=status.HTTP_201_CREATED)
async def create_service(payload: ServiceCreate, db: AsyncSession = Depends(get_db)):
    """Create a new service. Enforces unique service_key."""
    stmt = select(Service).where(Service.service_key == payload.service_key)
    res = await db.execute(stmt)
    if res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ya existe un servicio con la clave '{payload.service_key}'."
        )

    service = Service(
        service_key=payload.service_key,
        service_name=payload.service_name,
        description=payload.description,
        is_active=payload.is_active
    )
    db.add(service)
    await db.commit()
    await db.refresh(service)
    return service


@router.put("/{service_id}", response_model=ServiceOut)
async def update_service(
    service_id: int, payload: ServiceUpdate, db: AsyncSession = Depends(get_db)
):
    """Update details of an existing service."""
    stmt = select(Service).where(Service.service_id == service_id)
    result = await db.execute(stmt)
    service = result.scalars().first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Servicio con ID {service_id} no encontrado."
        )

    if payload.service_name is not None:
        service.service_name = payload.service_name
    if payload.description is not None:
        service.description = payload.description
    if payload.is_active is not None:
        service.is_active = payload.is_active

    await db.commit()
    await db.refresh(service)
    return service


@router.delete("/{service_id}", status_code=status.HTTP_200_OK)
async def delete_service(service_id: int, db: AsyncSession = Depends(get_db)):
    """
    Delete a service.
    Validates that no active prompts or base structures reference this service before deleting.
    """
    stmt = select(Service).where(Service.service_id == service_id)
    result = await db.execute(stmt)
    service = result.scalars().first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Servicio con ID {service_id} no encontrado."
        )

    # Check for prompts referencing this service
    p_stmt = select(func.count(Prompt.prompt_id)).where(
        Prompt.service_id == service_id,
        Prompt.is_active == True,
        Prompt.is_archived == False
    )
    p_res = await db.execute(p_stmt)
    if p_res.scalar() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede eliminar el servicio porque tiene prompts activos asociados."
        )

    # Check for base structures referencing this service
    bs_stmt = select(func.count(PromptBaseStructure.id)).where(
        PromptBaseStructure.service_id == service_id,
        PromptBaseStructure.is_active == True
    )
    bs_res = await db.execute(bs_stmt)
    if bs_res.scalar() > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede eliminar el servicio porque tiene estructuras base activas asociadas."
        )

    await db.delete(service)
    await db.commit()
    return {"ok": True, "detail": f"Servicio {service_id} eliminado exitosamente."}
