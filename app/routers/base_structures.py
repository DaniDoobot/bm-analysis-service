"""FastAPI router for Base Structures typology associations."""
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user
from app.models.users import User
from app.models.prompts import PromptBaseStructure, BaseStructureTypology
from app.models.typologies import Typology
from app.schemas.prompts import UpdateTypologiesRequest, PromptBaseStructureNestedDetailOut
from app.routers.prompts import _get_base_structure_nested_dict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/base-structures", tags=["Base Structures Typologies"])


@router.get("/{id}", response_model=PromptBaseStructureNestedDetailOut)
async def get_base_structure_with_typologies(
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)]
):
    """Retrieve detailed base structure by ID including associated and available typologies."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los agentes no tienen acceso a las estructuras."
        )

    # 1. Fetch base structure
    struct = await db.get(PromptBaseStructure, id)
    if not struct:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Estructura base con ID {id} no encontrada."
        )

    return await _get_base_structure_nested_dict(db, struct, current_user)


@router.patch("/{id}/typologies", response_model=PromptBaseStructureNestedDetailOut)
async def update_base_structure_typologies(
    id: int,
    payload: UpdateTypologiesRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)]
):
    """Replace all typology associations of a base structure with the specified typology IDs."""
    if getattr(current_user, "role", "agent").lower() == "agent":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Los agentes no tienen acceso a las estructuras."
        )

    # 1. Fetch base structure
    struct = await db.get(PromptBaseStructure, id)
    if not struct:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Estructura base con ID {id} no encontrada."
        )

    if not struct.service_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La estructura base no tiene un servicio asignado."
        )

    # Validate that all typology IDs exist and belong to the same service
    unique_ids = list(dict.fromkeys(payload.typology_ids or []))
    if unique_ids:
        t_stmt = select(Typology).where(
            Typology.typology_id.in_(unique_ids)
        )
        t_res = await db.execute(t_stmt)
        typos = t_res.scalars().all()
        
        if len(typos) != len(unique_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Una o más tipologías especificadas no existen."
            )

        for t in typos:
            if t.service_id != struct.service_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"La tipología '{t.typology_name}' pertenece al servicio {t.service_id}, pero la estructura pertenece al servicio {struct.service_id}."
                )

    # Clear existing associations
    await db.execute(
        delete(BaseStructureTypology).where(BaseStructureTypology.base_structure_id == id)
    )

    # Insert new associations
    for tid in unique_ids:
        db.add(
            BaseStructureTypology(
                base_structure_id=id,
                typology_id=tid
            )
        )

    await db.commit()
    await db.refresh(struct)
    
    return await _get_base_structure_nested_dict(db, struct, current_user)
