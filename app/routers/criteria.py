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
)
from app.services import criteria_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Criteria"])


@router.get("/prompt-criteria", response_model=CriteriaGroupedOut)
async def get_prompt_criteria(
    prompt_id: Annotated[int, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return active criteria for a prompt, grouped by criterion_type."""
    return await criteria_service.get_criteria_grouped(db, prompt_id=prompt_id)


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
