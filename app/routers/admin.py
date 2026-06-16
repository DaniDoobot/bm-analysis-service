"""
Admin router — administrative operations (environment cleanup).
User management has been moved to /bm/users router (Bearer auth + admin role).
"""
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_admin
from app.models.users import User

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


class CleanupTypologyRequest(BaseModel):
    typology_key: str = Field(default="informacion", description="Typology key to target for cleanup")
    mode: Literal["dry_run", "execute"] = Field(default="dry_run", description="dry_run to preview, execute to apply deletion")
    performed_by_email: str | None = Field(default=None, description="Email of user performing the cleanup")


@router.post("/cleanup-typology-results")
async def cleanup_typology_results(
    body: CleanupTypologyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Delete all manual and mass evaluation analysis results belonging to a specific typology (e.g. 'informacion').
    """
    logger.info(
        "Admin cleanup-typology-results called: key=%s mode=%s performed_by=%s",
        body.typology_key, body.mode, body.performed_by_email,
    )
    try:
        from app.services.cleanup_service import cleanup_typology_results as _cleanup_typology
        result = await _cleanup_typology(
            db=db,
            typology_key=body.typology_key,
            mode=body.mode,
            performed_by_email=body.performed_by_email,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("Error durante la limpieza de tipologia %s: %s", body.typology_key, e)
        raise HTTPException(
            status_code=400,
            detail=f"Error durante la limpieza de tipología: {str(e)}",
        )


class SyncCriteriaNamesRequest(BaseModel):
    prompt_id: int | None = Field(default=None, description="Optional prompt ID to restrict synchronization")
    expected_individual_results_to_update: int | None = Field(default=None, description="Expected individual results count to prevent concurrent updates conflict")
    expected_mass_results_to_update: int | None = Field(default=None, description="Expected mass results count to prevent concurrent updates conflict")


class CriterionSyncDetail(BaseModel):
    prompt_id: int | None
    criterion_key: str | None
    old_name: str
    new_name: str | None
    individual_rows_affected: int
    mass_rows_affected: int


class SyncCriteriaNamesPreviewResponse(BaseModel):
    total_criteria_to_sync: int
    individual_results_to_update: int
    mass_results_to_update: int
    details: list[CriterionSyncDetail]


class SyncCriteriaNamesExecuteResponse(BaseModel):
    ok: bool
    individual_criteria_rows_updated: int
    mass_criteria_rows_updated: int
    mass_results_rows_updated: int


@router.post("/sync-criteria-names/preview", response_model=SyncCriteriaNamesPreviewResponse)
async def sync_criteria_names_preview(
    body: SyncCriteriaNamesRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_admin)],
):
    """
    Preview the synchronization of visible names in historical results.
    Does not modify any database records.
    """
    from app.services.criteria_sync_service import preview_sync_criteria_names
    try:
        res = await preview_sync_criteria_names(db, prompt_id=body.prompt_id)
        return res
    except ValueError as ve:
        raise HTTPException(
            status_code=422,
            detail=str(ve),
        )
    except Exception as e:
        logger.exception("Error during sync-criteria-names preview: %s", e)
        raise HTTPException(
            status_code=500,
            detail="No se ha podido calcular la previsualización de sincronización."
        )


@router.post("/sync-criteria-names/execute", response_model=SyncCriteriaNamesExecuteResponse)
async def sync_criteria_names_execute(
    body: SyncCriteriaNamesRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_admin)],
):
    """
    Execute the synchronization of visible names in historical results.
    Performs updates inside a single transaction.
    """
    from app.services.criteria_sync_service import execute_sync_criteria_names, ConcurrencyConflictError
    try:
        res = await execute_sync_criteria_names(
            db,
            prompt_id=body.prompt_id,
            performed_by_email=current_user.email,
            expected_individual_results_to_update=body.expected_individual_results_to_update,
            expected_mass_results_to_update=body.expected_mass_results_to_update
        )
        await db.commit()
        return res
    except ConcurrencyConflictError as cce:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=str(cce),
        )
    except ValueError as ve:
        await db.rollback()
        raise HTTPException(
            status_code=422,
            detail=str(ve),
        )
    except Exception as e:
        await db.rollback()
        logger.exception("Error during sync-criteria-names execute: %s", e)
        raise HTTPException(
            status_code=500,
            detail="No se ha podido ejecutar la sincronización de nombres."
        )

