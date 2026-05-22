"""
cleanup_service.py — Administrative cleanup for prompts, base structures and prompt versions.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationResult
from app.models.prompts import Prompt, PromptBaseStructure, PromptVersion

logger = logging.getLogger(__name__)

LEGACY_KEYS_FORBIDDEN = [
    "informacion_sin_cita",
    "falta_con_reagendo",
    "falta_sin_reagendo",
    "no_interesado",
    "no_apto",
]


async def run_cleanup(
    db: AsyncSession,
    keep_prompt_ids: list[int],
    keep_base_structure_ids: list[int],
    mode: str,  # "dry_run" | "execute"
    delete_physical_if_safe: bool = False,
    performed_by_email: str | None = None,
) -> dict[str, Any]:
    """
    Main cleanup entry point.
    In dry_run, returns a plan without mutating data.
    In execute, applies soft-deletes/archives.
    """
    logger.info(
        "Cleanup started — mode=%s keep_prompts=%s keep_base_structures=%s",
        mode, keep_prompt_ids, keep_base_structure_ids,
    )

    now = datetime.now(timezone.utc)
    warnings: list[str] = []

    # ── 1. Prompts to archive ─────────────────────────────────────────────────
    all_prompts_res = await db.execute(
        select(Prompt).where(Prompt.deleted_at == None, Prompt.is_archived == False)
    )
    all_prompts = all_prompts_res.scalars().all()

    prompts_to_archive = [p for p in all_prompts if p.prompt_id not in keep_prompt_ids]

    # Which prompts are referenced in mass eval jobs or results
    blocked_prompt_ids: set[int] = set()
    for p in prompts_to_archive:
        job_res = await db.execute(
            select(MassEvaluationJob.job_id).where(MassEvaluationJob.prompt_id == p.prompt_id).limit(1)
        )
        if job_res.scalar():
            blocked_prompt_ids.add(p.prompt_id)
            warnings.append(
                f"prompt_id={p.prompt_id} ('{p.prompt_name}') está referenciado en jobs de evaluación masiva. "
                "Se archivará en lugar de eliminarse físicamente."
            )
        result_res = await db.execute(
            select(MassEvaluationResult.mass_analysis_id)
            .where(MassEvaluationResult.prompt_id == p.prompt_id)
            .limit(1)
        )
        if result_res.scalar():
            blocked_prompt_ids.add(p.prompt_id)

    # ── 2. Base structures to archive ─────────────────────────────────────────
    all_structs_res = await db.execute(
        select(PromptBaseStructure).where(PromptBaseStructure.is_active == True)
    )
    all_structs = all_structs_res.scalars().all()

    structs_to_archive = [s for s in all_structs if s.id not in keep_base_structure_ids]

    # ── 3. Prompt versions affected ───────────────────────────────────────────
    archive_prompt_ids = [p.prompt_id for p in prompts_to_archive]
    versions_affected: list[dict] = []
    if archive_prompt_ids:
        vers_res = await db.execute(
            select(PromptVersion).where(PromptVersion.prompt_id.in_(archive_prompt_ids))
        )
        for v in vers_res.scalars().all():
            # Check if this version is referenced in mass eval results
            used_res = await db.execute(
                select(MassEvaluationResult.mass_analysis_id)
                .where(MassEvaluationResult.prompt_version_id == v.id)
                .limit(1)
            )
            used = used_res.scalar() is not None
            versions_affected.append({
                "version_id": v.id,
                "prompt_id": v.prompt_id,
                "version_name": v.version_name,
                "is_current": v.is_current,
                "used_in_mass_results": used,
                "action": "keep_historical" if used else "cascade_archive",
            })

    # ── 4. Criteria affected ──────────────────────────────────────────────────
    criteria_affected: list[dict] = []
    if archive_prompt_ids:
        criteria_res = await db.execute(
            select(PromptCriterion).where(
                PromptCriterion.prompt_id.in_(archive_prompt_ids),
                PromptCriterion.is_active == True,
            )
        )
        for c in criteria_res.scalars().all():
            criteria_affected.append({
                "criterion_id": c.criterion_id,
                "prompt_id": c.prompt_id,
                "criterion_key": c.criterion_key,
                "output_key": c.output_key,
                "action": "deactivate",
            })

    # ── 5. Criterion-typology relations affected ───────────────────────────────
    criterion_ids_affected = [c["criterion_id"] for c in criteria_affected]
    criterion_typologies_affected: list[dict] = []
    if criterion_ids_affected:
        ct_res = await db.execute(
            select(PromptCriterionTypology).where(
                PromptCriterionTypology.criterion_id.in_(criterion_ids_affected)
            )
        )
        for ct in ct_res.scalars().all():
            criterion_typologies_affected.append({
                "id": ct.id,
                "criterion_id": ct.criterion_id,
                "typology_id": ct.typology_id,
                "action": "cascade_delete_if_physical" if delete_physical_if_safe else "keep_historical",
            })

    blocked_physical_deletes = [
        {
            "prompt_id": pid,
            "reason": "Referenced in mass evaluation jobs or results. Will be archived instead.",
        }
        for pid in sorted(blocked_prompt_ids)
    ]

    plan = {
        "prompts_to_archive": [
            {"prompt_id": p.prompt_id, "prompt_name": p.prompt_name, "action": "archive"}
            for p in prompts_to_archive
        ],
        "base_structures_to_archive": [
            {"id": s.id, "structure_key": s.structure_key, "structure_name": s.structure_name, "action": "deactivate"}
            for s in structs_to_archive
        ],
        "prompt_versions_affected": versions_affected,
        "criteria_affected": criteria_affected,
        "criterion_typologies_affected": criterion_typologies_affected,
        "blocked_physical_deletes": blocked_physical_deletes,
        "warnings": warnings,
    }

    if mode == "dry_run":
        logger.info("Cleanup dry_run completed — no data modified.")
        return {"mode": "dry_run", "plan": plan}

    # ── EXECUTE MODE ──────────────────────────────────────────────────────────
    archived_prompts: list[int] = []
    archived_structs: list[int] = []
    deactivated_criteria: list[int] = []

    # Archive prompts
    for p in prompts_to_archive:
        p.is_archived = True
        p.archived_at = now
        p.archived_by_email = performed_by_email
        p.is_active = False
        db.add(p)
        archived_prompts.append(p.prompt_id)
        logger.info("Archived prompt_id=%d ('%s')", p.prompt_id, p.prompt_name)

    # Deactivate base structures
    for s in structs_to_archive:
        s.is_active = False
        db.add(s)
        archived_structs.append(s.id)
        logger.info("Deactivated base_structure id=%d ('%s')", s.id, s.structure_key)

    # Deactivate criteria of archived prompts
    if archive_prompt_ids:
        criteria_res2 = await db.execute(
            select(PromptCriterion).where(
                PromptCriterion.prompt_id.in_(archive_prompt_ids),
                PromptCriterion.is_active == True,
            )
        )
        for c in criteria_res2.scalars().all():
            c.is_active = False
            db.add(c)
            deactivated_criteria.append(c.criterion_id)

    await db.commit()

    logger.info(
        "Cleanup execute completed — archived_prompts=%s deactivated_structs=%s deactivated_criteria=%s",
        archived_prompts, archived_structs, deactivated_criteria,
    )

    return {
        "mode": "execute",
        "ok": True,
        "archived_prompts": archived_prompts,
        "archived_base_structures": archived_structs,
        "deactivated_criteria_count": len(deactivated_criteria),
        "blocked_physical_deletes": blocked_physical_deletes,
        "warnings": warnings,
        "plan_preview": plan,
    }


async def cleanup_prompt_versions(
    db: AsyncSession,
    keep_prompt_ids: list[int],
    keep_current_versions_only: bool = True,
    mode: str = "dry_run",
    delete_physical_if_safe: bool = False,
    performed_by_email: str | None = None,
) -> dict[str, Any]:
    """
    Archive (hide) all non-current versions for the given prompts.
    Versions referenced in bm_mass_evaluation_results are never physically deleted.
    Returns a dry_run plan or executes the operation.
    """
    logger.info(
        "Version cleanup started — mode=%s keep_prompts=%s keep_current_only=%s",
        mode, keep_prompt_ids, keep_current_versions_only,
    )

    now = datetime.now(timezone.utc)
    warnings: list[str] = []

    versions_to_archive: list[dict] = []
    versions_to_delete: list[dict] = []
    versions_blocked: list[dict] = []
    current_versions_kept: list[dict] = []

    for prompt_id in keep_prompt_ids:
        # Fetch all versions for this prompt
        vers_res = await db.execute(
            select(PromptVersion)
            .where(PromptVersion.prompt_id == prompt_id)
            .order_by(PromptVersion.id.desc())
        )
        all_versions = vers_res.scalars().all()

        for v in all_versions:
            if v.is_current:
                current_versions_kept.append({
                    "version_id": v.id,
                    "prompt_id": v.prompt_id,
                    "version_name": v.version_name,
                    "version_label": v.version_label,
                    "action": "keep",
                })
                continue

            # Check if already archived
            if v.is_archived:
                continue

            # Check if referenced in mass eval results
            used_res = await db.execute(
                select(MassEvaluationResult.mass_analysis_id)
                .where(MassEvaluationResult.prompt_version_id == v.id)
                .limit(1)
            )
            used_in_results = used_res.scalar() is not None

            if used_in_results:
                versions_blocked.append({
                    "version_id": v.id,
                    "prompt_id": v.prompt_id,
                    "version_name": v.version_name,
                    "reason": "Referenced in bm_mass_evaluation_results. Will be archived, not deleted.",
                    "action": "archive_only",
                })
                warnings.append(
                    f"Version id={v.id} ('{v.version_name}') of prompt_id={prompt_id} "
                    "is referenced in mass evaluation results. Will be archived, not physically deleted."
                )
                versions_to_archive.append({
                    "version_id": v.id,
                    "prompt_id": v.prompt_id,
                    "version_name": v.version_name,
                    "action": "archive",
                })
            elif delete_physical_if_safe:
                versions_to_delete.append({
                    "version_id": v.id,
                    "prompt_id": v.prompt_id,
                    "version_name": v.version_name,
                    "action": "delete_physical",
                })
            else:
                versions_to_archive.append({
                    "version_id": v.id,
                    "prompt_id": v.prompt_id,
                    "version_name": v.version_name,
                    "action": "archive",
                })

    plan = {
        "current_versions_kept": current_versions_kept,
        "versions_to_archive": versions_to_archive,
        "versions_to_delete_if_safe": versions_to_delete,
        "versions_blocked_by_results": versions_blocked,
        "warnings": warnings,
    }

    if mode == "dry_run":
        logger.info("Version cleanup dry_run completed — no data modified.")
        return {"mode": "dry_run", "plan": plan}

    # ── EXECUTE ───────────────────────────────────────────────────────────────
    archived_version_ids: list[int] = []
    deleted_version_ids: list[int] = []

    # Archive versions (both blocked and safe-to-archive)
    all_to_archive_ids = [v["version_id"] for v in versions_to_archive]
    if all_to_archive_ids:
        arch_res = await db.execute(
            select(PromptVersion).where(PromptVersion.id.in_(all_to_archive_ids))
        )
        for v in arch_res.scalars().all():
            v.is_archived = True
            v.archived_at = now
            v.archived_by_email = performed_by_email
            db.add(v)
            archived_version_ids.append(v.id)
            logger.info("Archived version id=%d (prompt_id=%d, '%s')", v.id, v.prompt_id, v.version_name)

    # Physically delete safe versions (if requested and not blocked)
    safe_to_delete_ids = [v["version_id"] for v in versions_to_delete]
    if safe_to_delete_ids and delete_physical_if_safe:
        for vid in safe_to_delete_ids:
            v_res = await db.execute(select(PromptVersion).where(PromptVersion.id == vid))
            v_obj = v_res.scalars().first()
            if v_obj:
                await db.delete(v_obj)
                deleted_version_ids.append(vid)
                logger.info("Physically deleted version id=%d (prompt_id=%d)", vid, v_obj.prompt_id)

    await db.commit()

    logger.info(
        "Version cleanup execute completed — archived=%s deleted=%s",
        archived_version_ids, deleted_version_ids,
    )

    return {
        "mode": "execute",
        "ok": True,
        "archived_version_ids": archived_version_ids,
        "deleted_version_ids": deleted_version_ids,
        "current_versions_kept": current_versions_kept,
        "warnings": warnings,
        "plan_preview": plan,
    }


async def cleanup_mass_evaluations(
    db: AsyncSession,
    mode: str,
    performed_by_email: str | None = None
) -> dict[str, Any]:
    """
    Clean up mass evaluation results, runs, and jobs.
    In dry_run mode, returns counts of what would be deleted.
    In execute mode, physically deletes them in order.
    """
    from sqlalchemy import func
    from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult

    logger.info("Mass evaluation cleanup started — mode=%s performed_by=%s", mode, performed_by_email)

    warnings: list[str] = []

    # Count existing
    res_count = await db.execute(select(func.count(MassEvaluationResult.mass_analysis_id)))
    results_count = res_count.scalar() or 0
    
    run_count = await db.execute(select(func.count(MassEvaluationRun.run_id)))
    runs_count = run_count.scalar() or 0
    
    job_count = await db.execute(select(func.count(MassEvaluationJob.job_id)))
    jobs_count = job_count.scalar() or 0

    if mode == "dry_run":
        jobs_res = await db.execute(select(MassEvaluationJob.job_id))
        jobs_to_delete = jobs_res.scalars().all()
        
        runs_res = await db.execute(select(MassEvaluationRun.run_id))
        runs_to_delete = runs_res.scalars().all()
        
        warnings.append("This is a dry run. No data will be modified.")
        return {
            "jobs_count": jobs_count,
            "runs_count": runs_count,
            "results_count": results_count,
            "jobs_to_delete": list(jobs_to_delete),
            "runs_to_delete": list(runs_to_delete),
            "results_to_delete_count": results_count,
            "warnings": warnings,
        }

    # Execute
    try:
        # Delete results
        res_del = await db.execute(sa_delete(MassEvaluationResult))
        deleted_results = res_del.rowcount
        
        # Delete runs
        run_del = await db.execute(sa_delete(MassEvaluationRun))
        deleted_runs = run_del.rowcount
        
        # Delete jobs
        job_del = await db.execute(sa_delete(MassEvaluationJob))
        deleted_jobs = job_del.rowcount
        
        await db.commit()
        
        logger.info(
            "Mass evaluations cleanup completed. Deleted jobs=%s, runs=%s, results=%s",
            deleted_jobs, deleted_runs, deleted_results
        )
        
        return {
            "ok": True,
            "deleted_results": deleted_results,
            "deleted_runs": deleted_runs,
            "deleted_jobs": deleted_jobs,
            "warnings": warnings,
        }
    except Exception as e:
        await db.rollback()
        logger.exception("Error deleting mass evaluations: %s", e)
        raise
