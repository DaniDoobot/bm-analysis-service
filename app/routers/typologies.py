"""FastAPI router for Typologies."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.models.typologies import Typology
from app.models.services import Service
from app.schemas.typologies import TypologyCreate, TypologyOut, TypologyUpdate

router = APIRouter(prefix="/bm/typologies", tags=["Typologies"])


@router.get("", response_model=list[TypologyOut])
async def list_typologies(service_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """Retrieve all typologies, optionally filtered by service_id."""
    stmt = select(Typology)
    if service_id is not None:
        stmt = stmt.where(Typology.service_id == service_id)
    stmt = stmt.order_by(Typology.sort_order.asc(), Typology.typology_id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{typology_id}", response_model=TypologyOut)
async def get_typology(typology_id: int, db: AsyncSession = Depends(get_db)):
    """Retrieve details of a specific typology."""
    stmt = select(Typology).where(Typology.typology_id == typology_id)
    result = await db.execute(stmt)
    typology = result.scalars().first()
    if not typology:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tipología con ID {typology_id} no encontrada."
        )
    return typology


@router.post("", response_model=TypologyOut, status_code=status.HTTP_201_CREATED)
async def create_typology(payload: TypologyCreate, db: AsyncSession = Depends(get_db)):
    """Create a new typology. Verifies service exists and key is unique within service."""
    # Verify service exists
    s_stmt = select(Service).where(Service.service_id == payload.service_id)
    s_res = await db.execute(s_stmt)
    if not s_res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Servicio con ID {payload.service_id} no existe."
        )

    # Verify key uniqueness within the service
    t_stmt = select(Typology).where(
        Typology.service_id == payload.service_id,
        Typology.typology_key == payload.typology_key
    )
    t_res = await db.execute(t_stmt)
    if t_res.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ya existe una tipología con la clave '{payload.typology_key}' en el servicio {payload.service_id}."
        )

    typology = Typology(
        service_id=payload.service_id,
        typology_key=payload.typology_key,
        typology_name=payload.typology_name,
        description=payload.description,
        sort_order=payload.sort_order,
        is_active=payload.is_active
    )
    db.add(typology)
    await db.commit()
    await db.refresh(typology)
    return typology


@router.put("/{typology_id}", response_model=TypologyOut)
async def update_typology(
    typology_id: int, payload: TypologyUpdate, db: AsyncSession = Depends(get_db)
):
    """Update details of an existing typology."""
    stmt = select(Typology).where(Typology.typology_id == typology_id)
    result = await db.execute(stmt)
    typology = result.scalars().first()
    if not typology:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tipología con ID {typology_id} no encontrada."
        )

    if payload.typology_name is not None:
        typology.typology_name = payload.typology_name
    if payload.description is not None:
        typology.description = payload.description
    if payload.sort_order is not None:
        typology.sort_order = payload.sort_order
    if payload.is_active is not None:
        typology.is_active = payload.is_active

    await db.commit()
    await db.refresh(typology)
    return typology


@router.delete("/{typology_id}", status_code=status.HTTP_200_OK)
async def delete_typology(typology_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a typology."""
    stmt = select(Typology).where(Typology.typology_id == typology_id)
    result = await db.execute(stmt)
    typology = result.scalars().first()
    if not typology:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tipología con ID {typology_id} no encontrada."
        )

    await db.delete(typology)
    await db.commit()
    return {"ok": True, "detail": f"Tipología {typology_id} eliminada exitosamente."}
