"""
Admin router — administrative operations including environment cleanup.
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
    # Safety guard: always protect at minimum the defaults
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


class CleanupSpecificRequest(BaseModel):
    mode: Literal["dry_run", "execute"] = Field(default="dry_run", description="dry_run to preview, execute to delete")
    expected_count: int = Field(default=14, description="Expected number of results to delete for safety check")


@router.post("/cleanup-specific-evaluations")
async def cleanup_specific_evaluations(
    body: CleanupSpecificRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Temporary endpoint to safely and programmatically delete a specific set of mass evaluations
    strictly by call_timestamp in production.
    """
    from sqlalchemy import select, delete, func
    from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult, MassEvaluationJob, MassEvaluationRun
    from datetime import datetime, timezone
    
    start_ts = datetime(2026, 5, 25, 0, 0, 0, tzinfo=timezone.utc)
    end_ts = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    
    try:
        # 1. Total jobs and runs count before (to confirm no deletion)
        stmt_jobs_before = select(func.count(MassEvaluationJob.job_id))
        stmt_runs_before = select(func.count(MassEvaluationRun.run_id))
        jobs_count_before = (await db.execute(stmt_jobs_before)).scalar() or 0
        runs_count_before = (await db.execute(stmt_runs_before)).scalar() or 0
        
        # 2. Total results to delete before (WITHOUT status completed constraint to count all rows)
        stmt_res_before = select(func.count(MassEvaluationResult.mass_analysis_id)).where(
            MassEvaluationResult.call_timestamp >= start_ts,
            MassEvaluationResult.call_timestamp < end_ts
        )
        total_resultados_a_borrar = (await db.execute(stmt_res_before)).scalar() or 0
        
        # 3. Total criteria to delete before
        subq_ids = select(MassEvaluationResult.mass_analysis_id).where(
            MassEvaluationResult.call_timestamp >= start_ts,
            MassEvaluationResult.call_timestamp < end_ts
        )
        stmt_crit_before = select(func.count(MassEvaluationCriterionResult.id)).where(
            MassEvaluationCriterionResult.mass_analysis_id.in_(subq_ids)
        )
        total_criterios_asociados_a_borrar = (await db.execute(stmt_crit_before)).scalar() or 0
        
        deleted_criteria = 0
        deleted_results = 0
        
        # 4. If execute mode and count matches dynamically provided expected_count
        if body.mode == "execute":
            if total_resultados_a_borrar != body.expected_count:
                raise HTTPException(
                    status_code=400,
                    detail=f"Safety check failed: expected exactly {body.expected_count} results to delete, found {total_resultados_a_borrar}."
                )
                
            # Perform deletes
            stmt_del_crit = delete(MassEvaluationCriterionResult).where(
                MassEvaluationCriterionResult.mass_analysis_id.in_(subq_ids)
            )
            res_del_crit = await db.execute(stmt_del_crit)
            deleted_criteria = res_del_crit.rowcount
            
            stmt_del_res = delete(MassEvaluationResult).where(
                MassEvaluationResult.call_timestamp >= start_ts,
                MassEvaluationResult.call_timestamp < end_ts
            )
            res_del_res = await db.execute(stmt_del_res)
            deleted_results = res_del_res.rowcount
            
            await db.commit()
            
        # 5. Validation after
        stmt_res_after = select(func.count(MassEvaluationResult.mass_analysis_id)).where(
            MassEvaluationResult.call_timestamp >= start_ts,
            MassEvaluationResult.call_timestamp < end_ts
        )
        resultados_restantes = (await db.execute(stmt_res_after)).scalar() or 0
        
        # Validation orphans after (criteria whose mass_analysis_id is not in results)
        subq_all_res = select(MassEvaluationResult.mass_analysis_id)
        stmt_orphans = select(func.count(MassEvaluationCriterionResult.id)).where(
            ~MassEvaluationCriterionResult.mass_analysis_id.in_(subq_all_res)
        )
        criterios_huerfanos = (await db.execute(stmt_orphans)).scalar() or 0
        
        # Jobs and runs after
        jobs_count_after = (await db.execute(stmt_jobs_before)).scalar() or 0
        runs_count_after = (await db.execute(stmt_runs_before)).scalar() or 0
        
        jobs_runs_unchanged = (jobs_count_before == jobs_count_after) and (runs_count_before == runs_count_after)
        
        return {
            "ok": True,
            "mode": body.mode,
            "total_resultados_a_borrar_previo": total_resultados_a_borrar,
            "total_criterios_asociados_a_borrar_previo": total_criterios_asociados_a_borrar,
            "filas_borradas_criterios": deleted_criteria,
            "filas_borradas_resultados": deleted_results,
            "resultados_restantes_despues": resultados_restantes,
            "criterios_huerfanos_despues": criterios_huerfanos,
            "jobs_count_before": jobs_count_before,
            "jobs_count_after": jobs_count_after,
            "runs_count_before": runs_count_before,
            "runs_count_after": runs_count_after,
            "confirmacion_jobs_y_runs_no_afectados": jobs_runs_unchanged,
            "sql_ejecutado": (
                "BEGIN;\n"
                "DELETE FROM bm_mass_evaluation_criterion_results "
                "WHERE mass_analysis_id IN ("
                "  SELECT mass_analysis_id FROM bm_mass_evaluation_results "
                "  WHERE call_timestamp >= '2026-05-25 00:00:00+00' AND call_timestamp < '2026-06-01 00:00:00+00'"
                ");\n"
                "DELETE FROM bm_mass_evaluation_results "
                "WHERE call_timestamp >= '2026-05-25 00:00:00+00' AND call_timestamp < '2026-06-01 00:00:00+00';\n"
                "COMMIT;"
            )
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error during cleanup-specific-evaluations: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Internal error during cleanup: {str(e)}"
        )

