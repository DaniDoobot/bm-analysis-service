"""Prompts router — thin layer delegating to service functions."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.prompts import (
    ActivateVersionRequest,
    ActivePromptOut,
    PromptVersionOut,
    PromptWithCurrentVersion,
    SavePromptRequest,
)
from app.services import prompts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Prompts"])


@router.get("/prompts", response_model=list[PromptWithCurrentVersion])
async def list_prompts(db: Annotated[AsyncSession, Depends(get_db)]):
    """Return all prompts with their current version (if any)."""
    return await prompts_service.list_prompts(db)


@router.get("/prompts/active", response_model=ActivePromptOut)
async def get_active_prompt(
    type: Annotated[str, Query(description="audio | text")],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the active prompt for the given type including current version text."""
    result = await prompts_service.get_active_prompt(db, prompt_type=type)
    if not result:
        raise HTTPException(status_code=404, detail=f"No active prompt found for type '{type}'")
    return result


@router.get("/prompt-versions", response_model=list[PromptVersionOut])
async def list_prompt_versions(
    prompt_id: Annotated[int, Query()],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return all versions of a prompt ordered by created_at desc."""
    return await prompts_service.list_versions(db, prompt_id=prompt_id)


@router.post("/save-prompt")
async def save_prompt(
    body: SavePromptRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new prompt version and mark it as current."""
    version = await prompts_service.save_prompt_version(db, body)
    return {"ok": True, "status": "created", "version": PromptVersionOut.model_validate(version)}


@router.post("/activate-prompt-version")
async def activate_prompt_version(
    body: ActivateVersionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Mark a specific version as current for its prompt."""
    version = await prompts_service.activate_version(db, version_id=body.id)
    if not version:
        raise HTTPException(status_code=404, detail=f"Version {body.id} not found")
    return {"ok": True, "status": "activated", "version": PromptVersionOut.model_validate(version)}
