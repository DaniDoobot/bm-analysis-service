"""Criteria router."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.criteria import (
    CriteriaGroupedOut,
    SaveCriterionRequest,
    ToggleCriterionRequest,
    DeleteCriterionRequest,
)
from app.schemas.typologies import CriterionTypologyAssociation
from app.services import criteria_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Criteria"])


@router.get("/prompt-criteria", response_model=CriteriaGroupedOut)
async def get_prompt_criteria(
    prompt_id: Annotated[int, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_deleted: bool = False,
):
    """Return active criteria for a prompt, grouped by criterion_type."""
    return await criteria_service.get_criteria_grouped(db, prompt_id=prompt_id, include_deleted=include_deleted)


@router.post("/prompt-criteria/save")
async def save_criterion(
    body: SaveCriterionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create or update a criterion."""
    criterion = await criteria_service.save_criterion(db, body)
    return {"ok": True, "status": "saved", "criterion_id": criterion.criterion_id}


@router.post("/prompt-criteria/toggle")
async def toggle_criterion(
    body: ToggleCriterionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Activate or deactivate a criterion."""
    await criteria_service.toggle_criterion(db, body.criterion_id, body.is_active)
    return {"ok": True, "status": "updated", "criterion_id": body.criterion_id, "is_active": body.is_active}


@router.get("/prompt-criteria/{criterion_id}/typologies", response_model=list[CriterionTypologyAssociation])
async def get_criterion_typologies(
    criterion_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Retrieve all active typologies of the service and whether they are associated with the criterion."""
    return await criteria_service.get_criterion_typologies(db, criterion_id=criterion_id)


@router.put("/prompt-criteria/{criterion_id}/typologies")
async def update_criterion_typologies(
    criterion_id: int,
    typology_ids: list[int],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update typology associations for a specific criterion."""
    return await criteria_service.update_criterion_typologies(db, criterion_id=criterion_id, typology_ids=typology_ids)


@router.delete("/prompt-criteria/{criterion_id}")
async def delete_criterion(
    criterion_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: DeleteCriterionRequest | None = None,
):
    """Delete or soft-delete a criterion."""
    try:
        email = body.performed_by_email if body else None
        return await criteria_service.delete_criterion(db, criterion_id=criterion_id, performed_by_email=email)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error deleting criterion %s: %s", criterion_id, e)
        raise HTTPException(status_code=400, detail=f"Error eliminando el criterio: {str(e)}")

