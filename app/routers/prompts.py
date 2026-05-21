"""Prompts router — thin layer delegating to service functions."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.schemas.prompts import (
    ActivateVersionRequest,
    ActivePromptOut,
    PromptVersionOut,
    PromptWithCurrentVersion,
    SavePromptRequest,
    PromptBaseStructureOut,
    PromptBaseStructureDetailOut,
    PromptBaseStructureCreate,
    PromptBaseStructureUpdate,
    CreateFromBaseRequest,
    CreateFromBaseResponse,
)
from app.services import prompts_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm", tags=["Prompts"])


class AssignBaseStructureRequest(BaseModel):
    base_structure_id: int


@router.get("/prompts", response_model=list[PromptWithCurrentVersion])
async def list_prompts(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    base_structure_id: Annotated[int | None, Query(description="Filter by base structure ID")] = None,
    base_structure_key: Annotated[str | None, Query(description="Filter by base structure Key")] = None,
    active: Annotated[bool | None, Query(description="Filter by active status")] = None,
):
    """Return all prompts with their current version (if any), with optional filtering."""
    return await prompts_service.list_prompts(
        db,
        prompt_type=type,
        base_structure_id=base_structure_id,
        base_structure_key=base_structure_key,
        is_active=active,
    )


@router.put("/prompts/{prompt_id}/base-structure")
async def assign_base_structure(
    prompt_id: int,
    body: AssignBaseStructureRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Assign a base structure to an existing prompt (metadata reference only).
    """
    try:
        return await prompts_service.assign_base_structure(
            db, prompt_id=prompt_id, base_structure_id=body.base_structure_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))



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


# ── Prompt Base Structures Endpoints ──────────────────────────────────────────

@router.get("/prompt-base-structures", response_model=list[PromptBaseStructureOut])
async def list_prompt_base_structures(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
):
    """Return active base structures, optionally filtered by type."""
    return await prompts_service.list_base_structures(db, prompt_type=type)


@router.get("/prompt-base-structures/{id}", response_model=PromptBaseStructureDetailOut)
async def get_prompt_base_structure(
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return detailed base structure by ID."""
    struct = await prompts_service.get_base_structure(db, structure_id=id)
    if not struct:
        raise HTTPException(status_code=404, detail=f"Base structure {id} not found.")
    # Force-clear criteria at the ORM level before serialization
    struct.default_criteria = None
    return struct


@router.post("/prompt-base-structures", response_model=PromptBaseStructureDetailOut)
async def create_prompt_base_structure(
    body: PromptBaseStructureCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new prompt base structure."""
    struct = await prompts_service.create_base_structure(db, body)
    struct.default_criteria = None
    return struct


@router.put("/prompt-base-structures/{id}", response_model=PromptBaseStructureDetailOut)
async def update_prompt_base_structure(
    id: int,
    body: PromptBaseStructureUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update an existing prompt base structure."""
    struct = await prompts_service.update_base_structure(db, structure_id=id, body=body)
    if not struct:
        raise HTTPException(status_code=404, detail=f"Base structure {id} not found.")
    # Force-clear criteria at the ORM level before serialization
    struct.default_criteria = None
    return struct


@router.post("/prompts/create-from-base", response_model=CreateFromBaseResponse)
async def create_prompt_from_base(
    body: CreateFromBaseRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new prompt and versions/criteria populated from base structure."""
    try:
        res = await prompts_service.create_prompt_from_base(db, body=body)
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/prompt-base-structures/boston-medical/refresh")
async def refresh_boston_medical_base_structure(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Manually synchronizes the 'boston_medical_audio' structure from active prompt 1
    and its active criteria.
    """
    try:
        return await prompts_service.refresh_boston_medical_base_structure(db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/prompt-base-structures/backfill-clear-criteria")
async def backfill_clear_criteria(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Emergency backfill: sets default_criteria = NULL for all prompt base structures.
    Idempotent and safe — use to force-clean legacy data without a restart.
    """
    from sqlalchemy import text
    result = await db.execute(
        text("UPDATE bm_prompt_base_structures SET default_criteria = NULL WHERE default_criteria IS NOT NULL;")
    )
    await db.commit()
    return {
        "ok": True,
        "rows_updated": result.rowcount,
        "message": "All base structures cleared of default_criteria.",
    }
