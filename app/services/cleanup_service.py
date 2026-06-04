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
    mode: str = "dry_run",
    performed_by_email: str | None = None,
) -> dict[str, Any]:
    """
    Delete all mass evaluation data (jobs, runs, results) in FK-safe order.
    dry_run: returns counts without modifying anything.
    execute: deletes results → runs → jobs.
    Never touches prompts, criteria, services, typologies or manual analyses.
    """
    from app.models.mass_evaluations import MassEvaluationRun
    from sqlalchemy import func

    now = datetime.now(timezone.utc)
    warnings: list[str] = []

    logger.info(
        "Mass evaluation cleanup started — mode=%s performed_by=%s ts=%s",
        mode, performed_by_email, now.isoformat(),
    )

    # ── Count current state ───────────────────────────────────────────────────
    jobs_count_res = await db.execute(select(func.count()).select_from(MassEvaluationJob))
    jobs_count = jobs_count_res.scalar() or 0

    runs_count_res = await db.execute(select(func.count()).select_from(MassEvaluationRun))
    runs_count = runs_count_res.scalar() or 0

    results_count_res = await db.execute(select(func.count()).select_from(MassEvaluationResult))
    results_count = results_count_res.scalar() or 0

    logger.info(
        "Current counts — jobs=%d runs=%d results=%d",
        jobs_count, runs_count, results_count,
    )

    # ── Collect summary for dry_run ───────────────────────────────────────────
    jobs_res = await db.execute(
        select(MassEvaluationJob.job_id, MassEvaluationJob.job_name).order_by(MassEvaluationJob.job_id)
    )
    jobs_to_delete = [{"job_id": r[0], "job_name": r[1]} for r in jobs_res.all()]

    runs_res = await db.execute(
        select(MassEvaluationRun.run_id, MassEvaluationRun.job_id, MassEvaluationRun.status)
        .order_by(MassEvaluationRun.run_id)
    )
    runs_to_delete = [{"run_id": r[0], "job_id": r[1], "status": r[2]} for r in runs_res.all()]

    plan = {
        "jobs_count": jobs_count,
        "runs_count": runs_count,
        "results_count": results_count,
        "jobs_to_delete": jobs_to_delete,
        "runs_to_delete": runs_to_delete,
        "results_to_delete_count": results_count,
        "warnings": warnings,
    }

    if mode == "dry_run":
        logger.info("Mass evaluation cleanup dry_run completed — no data modified.")
        return {"mode": "dry_run", "plan": plan}

    # ── EXECUTE — FK-safe delete order: results → runs → jobs ─────────────────
    # 1. Delete all results
    del_results = await db.execute(sa_delete(MassEvaluationResult))
    deleted_results = del_results.rowcount
    logger.info("Deleted %d mass evaluation results.", deleted_results)

    # 2. Delete all runs
    del_runs = await db.execute(sa_delete(MassEvaluationRun))
    deleted_runs = del_runs.rowcount
    logger.info("Deleted %d mass evaluation runs.", deleted_runs)

    # 3. Delete all jobs
    del_jobs = await db.execute(sa_delete(MassEvaluationJob))
    deleted_jobs = del_jobs.rowcount
    logger.info("Deleted %d mass evaluation jobs.", deleted_jobs)

    await db.commit()

    logger.info(
        "Mass evaluation cleanup execute completed — deleted: results=%d runs=%d jobs=%d performed_by=%s",
        deleted_results, deleted_runs, deleted_jobs, performed_by_email,
    )

    return {
        "mode": "execute",
        "ok": True,
        "deleted_results": deleted_results,
        "deleted_runs": deleted_runs,
        "deleted_jobs": deleted_jobs,
        "warnings": warnings,
        "plan_preview": plan,
    }


async def cleanup_typology_results(
    db: AsyncSession,
    typology_key: str = "informacion",
    mode: str = "dry_run",
    performed_by_email: str | None = None,
) -> dict[str, Any]:
    """
    Delete all manual and mass evaluation analysis results belonging to a specific typology (e.g. 'informacion').
    """
    from sqlalchemy import or_, select, delete as sa_delete, func
    from app.models.typologies import Typology
    from app.models.analyses import Analysis, AnalysisResult, AnalysisCriterionResult, CallAnalysisCurrent
    from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
    
    logger.info(
        "Typology cleanup started — key=%s mode=%s performed_by=%s",
        typology_key, mode, performed_by_email,
    )
    
    # 1. Resolve matching typologies
    stmt_typ = select(Typology).where(
        or_(
            Typology.typology_key == typology_key,
            Typology.typology_key.ilike(f"%{typology_key}%"),
            Typology.typology_name.ilike(f"%{typology_key}%"),
            # Also support accents if we have 'informacion' vs 'información'
            Typology.typology_key.ilike(f"%información%"),
            Typology.typology_name.ilike(f"%información%")
        )
    )
    typ_res = await db.execute(stmt_typ)
    typs = typ_res.scalars().all()
    
    typ_ids = [t.typology_id for t in typs]
    typ_keys = [t.typology_key for t in typs]
    typ_names = [t.typology_name for t in typs]
    
    # Add target strings in case there are orphaned records not joined with database entities
    target_keys = list(set([typology_key, "informacion", "información"] + typ_keys))
    target_names_patterns = ["%informacion%", "%información%"] + [f"%{name}%" for name in typ_names]
    
    # Construct conditions for mass evaluation results
    mass_eval_filter = or_(
        MassEvaluationResult.typology_id.in_(typ_ids) if typ_ids else False,
        MassEvaluationResult.typology_key.in_(target_keys),
        *[MassEvaluationResult.typology_name.ilike(p) for p in target_names_patterns]
    )
    
    # Construct conditions for manual analysis criterion results
    manual_criterion_filter = or_(
        AnalysisCriterionResult.typology_id.in_(typ_ids) if typ_ids else False,
        AnalysisCriterionResult.typology_key.in_(target_keys),
        *[AnalysisCriterionResult.typology_name.ilike(p) for p in target_names_patterns]
    )
    
    # 2. Query Mass Evaluation Results to Delete
    res_mass_ids = await db.execute(select(MassEvaluationResult.mass_analysis_id).where(mass_eval_filter))
    mass_eval_ids = list(res_mass_ids.scalars().all())
    
    # Count associated criteria records
    mass_criteria_count = 0
    if mass_eval_ids:
        cnt_mass_crit = await db.execute(
            select(func.count(MassEvaluationCriterionResult.id)).where(
                MassEvaluationCriterionResult.mass_analysis_id.in_(mass_eval_ids)
            )
        )
        mass_criteria_count = cnt_mass_crit.scalar() or 0
        
    # 3. Query Manual Analysis IDs to Delete
    # First, matching criteria
    res_manual_ids_crit = await db.execute(select(AnalysisCriterionResult.analysis_id).where(manual_criterion_filter))
    manual_ids_from_criteria = {r for r in res_manual_ids_crit.scalars().all()}
    
    # Second, matching by tipo_llamada directly in Analysis
    manual_analysis_filter = or_(
        *[Analysis.tipo_llamada.ilike(p) for p in target_names_patterns]
    )
    res_manual_ids_analysis = await db.execute(select(Analysis.analysis_id).where(manual_analysis_filter))
    manual_ids_from_analyses = {r for r in res_manual_ids_analysis.scalars().all()}
    
    # Union to get complete list of targeted manual analysis IDs
    all_manual_ids = list(manual_ids_from_criteria.union(manual_ids_from_analyses))
    
    # Count associated criteria records
    manual_criteria_count = 0
    if all_manual_ids:
        cnt_man_crit = await db.execute(
            select(func.count(AnalysisCriterionResult.id)).where(
                AnalysisCriterionResult.analysis_id.in_(all_manual_ids)
            )
        )
        manual_criteria_count = cnt_man_crit.scalar() or 0
        
    # Count associated legacy criteria results
    manual_results_count = 0
    if all_manual_ids:
        cnt_man_res = await db.execute(
            select(func.count(AnalysisResult.result_id)).where(
                AnalysisResult.analysis_id.in_(all_manual_ids)
            )
        )
        manual_results_count = cnt_man_res.scalar() or 0
        
    # Count associated current analysis records to delete
    current_analyses_count = 0
    current_filter = or_(
        CallAnalysisCurrent.latest_analysis_id.in_(all_manual_ids) if all_manual_ids else False,
        *[CallAnalysisCurrent.tipo_llamada.ilike(p) for p in target_names_patterns]
    )
    cnt_curr = await db.execute(select(func.count(CallAnalysisCurrent.call_id)).where(current_filter))
    current_analyses_count = cnt_curr.scalar() or 0
    
    # Summary of affected records
    plan = {
        "matched_typologies": [{"id": t.typology_id, "key": t.typology_key, "name": t.typology_name} for t in typs],
        "mass_evaluation_results_count": len(mass_eval_ids),
        "mass_evaluation_criterion_results_count": mass_criteria_count,
        "manual_analyses_count": len(all_manual_ids),
        "manual_analysis_criterion_results_count": manual_criteria_count,
        "manual_analysis_results_count": manual_results_count,
        "call_analysis_current_count": current_analyses_count,
    }
    
    if mode == "dry_run":
        logger.info("Typology cleanup dry_run completed — no data modified.")
        return {"mode": "dry_run", "plan": plan}
        
    # ── EXECUTE MODE ──────────────────────────────────────────────────────────
    deleted_mass_crit = 0
    deleted_mass_res = 0
    deleted_man_crit = 0
    deleted_man_res = 0
    deleted_man_main = 0
    deleted_curr = 0
    
    # 1. Delete Mass Evaluation Criterion Results
    if mass_eval_ids:
        res_del_mass_crit = await db.execute(
            sa_delete(MassEvaluationCriterionResult).where(
                MassEvaluationCriterionResult.mass_analysis_id.in_(mass_eval_ids)
            )
        )
        deleted_mass_crit = res_del_mass_crit.rowcount
        
    # 2. Delete Mass Evaluation Results
    if mass_eval_ids:
        res_del_mass_res = await db.execute(
            sa_delete(MassEvaluationResult).where(
                MassEvaluationResult.mass_analysis_id.in_(mass_eval_ids)
            )
        )
        deleted_mass_res = res_del_mass_res.rowcount
        
    # 3. Delete Manual Analysis Criterion Results
    if all_manual_ids:
        res_del_man_crit = await db.execute(
            sa_delete(AnalysisCriterionResult).where(
                AnalysisCriterionResult.analysis_id.in_(all_manual_ids)
            )
        )
        deleted_man_crit = res_del_man_crit.rowcount
        
    # 4. Delete Manual Analysis Results (Legacy pivot)
    if all_manual_ids:
        res_del_man_res = await db.execute(
            sa_delete(AnalysisResult).where(
                AnalysisResult.analysis_id.in_(all_manual_ids)
            )
        )
        deleted_man_res = res_del_man_res.rowcount
        
    # 5. Delete Call Analysis Current references or rows
    res_del_curr = await db.execute(
        sa_delete(CallAnalysisCurrent).where(current_filter)
    )
    deleted_curr = res_del_curr.rowcount
    
    # 6. Delete Main Analyses
    if all_manual_ids:
        res_del_man_main = await db.execute(
            sa_delete(Analysis).where(
                Analysis.analysis_id.in_(all_manual_ids)
            )
        )
        deleted_man_main = res_del_man_main.rowcount
        
    await db.commit()
    
    logger.info(
        "Typology cleanup executed — deleted mass: res=%d crit=%d, manual: main=%d crit=%d legacy_res=%d, current=%d",
        deleted_mass_res, deleted_mass_crit, deleted_man_main, deleted_man_crit, deleted_man_res, deleted_curr
    )
    
    return {
        "mode": "execute",
        "ok": True,
        "deleted_counts": {
            "mass_evaluation_results": deleted_mass_res,
            "mass_evaluation_criterion_results": deleted_mass_crit,
            "manual_analyses": deleted_man_main,
            "manual_analysis_criterion_results": deleted_man_crit,
            "manual_analysis_results": deleted_man_res,
            "call_analysis_current": deleted_curr,
        },
        "plan_preview": plan,
    }

