"""
Admin router — administrative operations (environment cleanup).
User management has been moved to /bm/users router (Bearer auth + admin role).
"""
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bm/admin", tags=["Admin"])


class CleanupRequest(BaseModel):
    keep_prompt_ids: list[int] = Field(default=[1], description="Prompt IDs to keep untouched")
    keep_base_structure_ids: list[int] = Field(default=[6], description="Base structure IDs to keep untouched")
    mode: Literal["dry_run", "execute"] = Field(default="dry_run", description="dry_run to preview, execute to apply")
    delete_physical_if_safe: bool = Field(default=False, description="Allow physical deletes if no dependencies exist")
    performed_by_email: str | None = Field(default=None, description="Email of user performing the cleanup")


@router.post("/cleanup-structures")
async def cleanup_structures(
    body: CleanupRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Administrative cleanup of stale prompts and base structures.

    - mode=dry_run: Returns what WOULD be archived/deleted without modifying any data.
    - mode=execute: Performs soft-delete/archive on all structures not in keep lists.

    Protections:
    - prompt_ids in keep_prompt_ids are never touched.
    - base_structure_ids in keep_base_structure_ids are never touched.
    - Structures referenced in mass evaluation jobs/results are archived, never physically deleted.
    - Historical results and jobs remain intact.
    """
    safe_prompt_ids = list(set(body.keep_prompt_ids))
    safe_base_ids = list(set(body.keep_base_structure_ids))

    if not safe_prompt_ids:
        raise HTTPException(status_code=400, detail="keep_prompt_ids cannot be empty.")
    if not safe_base_ids:
        raise HTTPException(status_code=400, detail="keep_base_structure_ids cannot be empty.")

    logger.info(
        "Admin cleanup-structures called: mode=%s keep_prompts=%s keep_bases=%s",
        body.mode, safe_prompt_ids, safe_base_ids,
    )

    try:
        from app.services.cleanup_service import run_cleanup
        result = await run_cleanup(
            db=db,
            keep_prompt_ids=safe_prompt_ids,
            keep_base_structure_ids=safe_base_ids,
            mode=body.mode,
            delete_physical_if_safe=body.delete_physical_if_safe,
            performed_by_email=body.performed_by_email,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("Error during cleanup-structures: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Error durante la limpieza: {str(e)}",
        )


class CleanupVersionsRequest(BaseModel):
    keep_prompt_ids: list[int] = Field(default=[1], description="Prompt IDs whose versions will be cleaned")
    keep_current_versions_only: bool = Field(default=True, description="Archive all non-current versions")
    mode: Literal["dry_run", "execute"] = Field(default="dry_run", description="dry_run to preview, execute to apply")
    delete_physical_if_safe: bool = Field(default=False, description="Allow physical deletes of unreferenced versions")
    performed_by_email: str | None = Field(default=None, description="Email of user performing the cleanup")


@router.post("/cleanup-prompt-versions")
async def cleanup_prompt_versions(
    body: CleanupVersionsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Archive (hide) all non-current versions of the specified prompts.

    - mode=dry_run: Returns what WOULD be archived without modifying any data.
    - mode=execute: Archives all non-current versions. Versions referenced in
      mass evaluation results are archived (not deleted) to preserve traceability.

    The current version (is_current=True) is always kept untouched.
    """
    if not body.keep_prompt_ids:
        raise HTTPException(status_code=400, detail="keep_prompt_ids cannot be empty.")

    logger.info(
        "Admin cleanup-prompt-versions called: mode=%s keep_prompts=%s",
        body.mode, body.keep_prompt_ids,
    )

    try:
        from app.services.cleanup_service import cleanup_prompt_versions as _cleanup_versions
        result = await _cleanup_versions(
            db=db,
            keep_prompt_ids=body.keep_prompt_ids,
            keep_current_versions_only=body.keep_current_versions_only,
            mode=body.mode,
            delete_physical_if_safe=body.delete_physical_if_safe,
            performed_by_email=body.performed_by_email,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("Error during cleanup-prompt-versions: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Error durante la limpieza de versiones: {str(e)}",
        )


class CleanupMassEvaluationsRequest(BaseModel):
    mode: Literal["dry_run", "execute"] = Field(default="dry_run", description="dry_run to preview, execute to delete all")
    performed_by_email: str | None = Field(default=None, description="Email of user performing the cleanup")


@router.post("/cleanup-mass-evaluations")
async def cleanup_mass_evaluations(
    body: CleanupMassEvaluationsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Delete ALL mass evaluation data (jobs, runs, results).

    - mode=dry_run: Returns counts and details without modifying any data.
    - mode=execute: Deletes in FK-safe order: results → runs → jobs.

    This operation is IRREVERSIBLE in execute mode.
    Does NOT touch prompts, criteria, services, typologies or manual analyses.
    """
    logger.info(
        "Admin cleanup-mass-evaluations called: mode=%s performed_by=%s",
        body.mode, body.performed_by_email,
    )
    try:
        from app.services.cleanup_service import cleanup_mass_evaluations as _cleanup_mass
        result = await _cleanup_mass(
            db=db,
            mode=body.mode,
            performed_by_email=body.performed_by_email,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("Error durante la limpieza de evaluaciones masivas: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"Error durante la limpieza de evaluaciones masivas: {str(e)}",
        )
