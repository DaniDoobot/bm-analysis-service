"""
cleanup_service.py — Administrative cleanup for prompts and base structures.

Logic:
- Keeps specified prompt_ids and base_structure_ids untouched.
- Soft-deletes (is_active=False) or archives (is_archived=True) everything else.
- Never physically deletes data used in mass evaluation results or jobs.
- Supports dry_run mode: returns what WOULD happen without modifying data.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
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
