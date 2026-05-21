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
    include_archived: Annotated[bool, Query(description="Include archived structures")] = False,
):
    """Return all prompts with their current version (if any), with optional filtering."""
    return await prompts_service.list_prompts(
        db,
        prompt_type=type,
        base_structure_id=base_structure_id,
        base_structure_key=base_structure_key,
        is_active=active,
        include_archived=include_archived,
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
    include_archived: Annotated[bool, Query(description="Include archived/hidden versions (admin only)")] = False,
):
    """Return non-archived versions of a prompt by default. Use include_archived=true for full audit history."""
    return await prompts_service.list_versions(db, prompt_id=prompt_id, include_archived=include_archived)


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

def _base_structure_out(struct) -> dict:
    return {
        "id": struct.id,
        "structure_key": struct.structure_key,
        "structure_name": struct.structure_name,
        "description": struct.description,
        "prompt_type": "text",
        "is_active": struct.is_active,
        "created_at": struct.created_at,
        "updated_at": struct.updated_at,
        "created_by": struct.created_by,
        "created_by_email": struct.created_by_email,
        "service_id": struct.service_id,
        "service_key": struct.service.service_key if struct.service else None,
        "service_name": struct.service.service_name if struct.service else None,
    }


def _base_structure_detail_out(struct) -> dict:
    return {
        "id": struct.id,
        "structure_key": struct.structure_key,
        "structure_name": struct.structure_name,
        "description": struct.description,
        "prompt_type": "text",
        "is_active": struct.is_active,
        "created_at": struct.created_at,
        "updated_at": struct.updated_at,
        "created_by": struct.created_by,
        "created_by_email": struct.created_by_email,
        "base_prompt": struct.base_prompt,
        "default_criteria": [],
        "service_id": struct.service_id,
        "service_key": struct.service.service_key if struct.service else None,
        "service_name": struct.service.service_name if struct.service else None,
    }


@router.get("/prompt-base-structures", response_model=list[PromptBaseStructureOut])
async def list_prompt_base_structures(
    db: Annotated[AsyncSession, Depends(get_db)],
    type: Annotated[str | None, Query(description="audio | text")] = None,
    include_archived: Annotated[bool, Query(description="Include inactive/archived structures")] = False,
):
    """Return active base structures by default; pass include_archived=true to see all."""
    structures = await prompts_service.list_base_structures(db, prompt_type=type, include_archived=include_archived)
    return [_base_structure_out(s) for s in structures]


@router.get("/prompt-base-structures/{id}", response_model=PromptBaseStructureDetailOut)
async def get_prompt_base_structure(
    id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return detailed base structure by ID."""
    struct = await prompts_service.get_base_structure(db, structure_id=id)
    if not struct:
        raise HTTPException(status_code=404, detail=f"Base structure {id} not found.")
    return _base_structure_detail_out(struct)


@router.post("/prompt-base-structures", response_model=PromptBaseStructureDetailOut)
async def create_prompt_base_structure(
    body: PromptBaseStructureCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new prompt base structure."""
    struct = await prompts_service.create_base_structure(db, body)
    return _base_structure_detail_out(struct)


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
    return _base_structure_detail_out(struct)


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


class ArchiveRequest(BaseModel):
    user_email: str | None = None


class UpdateCurrentRequest(BaseModel):
    prompt: str
    prompt_name: str | None = None
    description: str | None = None
    updated_by: str | None = None
    updated_by_email: str | None = None


@router.put("/prompts/{prompt_id}/current")
async def update_prompt_current(
    prompt_id: int,
    body: UpdateCurrentRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Overwrite the current prompt content without creating a visible new version.
    This is the 'Save' / 'Edit in place' operation.
    """
    try:
        result = await prompts_service.update_prompt_current(
            db,
            prompt_id=prompt_id,
            prompt_text=body.prompt,
            prompt_name=body.prompt_name,
            description=body.description,
            updated_by=body.updated_by,
            updated_by_email=body.updated_by_email,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class DuplicatePromptRequest(BaseModel):
    prompt_name: str
    description: str | None = None
    created_by: str | None = None
    created_by_email: str | None = None


@router.post("/prompts/{prompt_id}/duplicate")
async def duplicate_prompt(
    prompt_id: int,
    body: DuplicatePromptRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Create a fully independent copy of an existing prompt with its content and criteria.
    The new prompt starts as inactive/unpublished.
    """
    try:
        result = await prompts_service.duplicate_prompt(
            db,
            source_prompt_id=prompt_id,
            prompt_name=body.prompt_name,
            description=body.description,
            created_by=body.created_by,
            created_by_email=body.created_by_email,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/prompts/{prompt_id}/archive")
async def archive_prompt(
    prompt_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: ArchiveRequest | None = None,
):
    """Archive a prompt structure."""
    from app.services import archive_service
    user_email = body.user_email if body else None
    try:
        prompt = await archive_service.archive_prompt(db, prompt_id, user_email=user_email)
        return {
            "ok": True,
            "status": "archived",
            "prompt_id": prompt.prompt_id,
            "is_archived": prompt.is_archived,
            "archived_at": prompt.archived_at,
            "archived_by_email": prompt.archived_by_email,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/prompts/{prompt_id}/restore")
async def restore_prompt(
    prompt_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Restore an archived prompt as inactive/draft."""
    from app.services import archive_service
    try:
        prompt = await archive_service.restore_prompt(db, prompt_id)
        return {
            "ok": True,
            "status": "restored",
            "prompt_id": prompt.prompt_id,
            "is_archived": prompt.is_archived,
            "is_active": prompt.is_active,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(
    prompt_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Hard delete a prompt structure if safeguards allow."""
    from app.services import archive_service
    try:
        res = await archive_service.delete_prompt(db, prompt_id)
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
