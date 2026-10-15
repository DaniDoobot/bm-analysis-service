"""Archive service for prompts."""
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationResult

async def archive_prompt(db: AsyncSession, prompt_id: int, user_email: str | None = None) -> Prompt:
    """Mark a prompt structure as archived if it is not active in production."""
    # 1. Load prompt
    stmt = select(Prompt).where(Prompt.prompt_id == prompt_id, Prompt.deleted_at == None)
    result = await db.execute(stmt)
    prompt = result.scalars().first()
    if not prompt:
        raise ValueError(f"Estructura con ID {prompt_id} no encontrada.")

    # 2. Check if active in production
    if prompt.is_active:
        raise ValueError(
            "No se puede archivar la estructura activa en producción. Activa otra estructura primero."
        )

    # 3. Archive
    prompt.is_archived = True
    prompt.archived_at = datetime.now(timezone.utc)
    prompt.archived_by_email = user_email
    
    await db.commit()
    await db.refresh(prompt)
    return prompt

async def restore_prompt(db: AsyncSession, prompt_id: int) -> Prompt:
    """Restore an archived prompt as inactive/draft so it requires explicit publication."""
    # 1. Load prompt
    stmt = select(Prompt).where(Prompt.prompt_id == prompt_id, Prompt.deleted_at == None)
    result = await db.execute(stmt)
    prompt = result.scalars().first()
    if not prompt:
        raise ValueError(f"Estructura con ID {prompt_id} no encontrada.")

    # 2. Restore as inactive/draft
    prompt.is_archived = False
    prompt.archived_at = None
    prompt.archived_by_email = None
    prompt.is_active = False

    await db.commit()
    await db.refresh(prompt)
    return prompt

async def delete_prompt(db: AsyncSession, prompt_id: int, user_email: str | None = None) -> dict:
    """
    Safely delete a prompt structure:
    - If it has associated analyses, jobs, automations, or trainer configs:
      retire/archive it safely (is_active=False, is_archived=True, deleted_at=now)
      preserving historical versions, criteria, and analysis references.
    - If it has NO historical data:
      hard-delete it cleanly.
    """
    from sqlalchemy import delete, update
    from app.models.analyses import Analysis
    from app.models.criteria import PromptCriterion, PromptCriterionTypology
    from app.models.personalized_training import TrainingCallEvaluation
    from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationResult, MassAnalysisAutomation
    from app.models.prompts import StructurePermission
    from app.models.trainer import TrainerEvaluationConfig

    # 1. Load prompt
    stmt = select(Prompt).where(Prompt.prompt_id == prompt_id)
    result = await db.execute(stmt)
    prompt = result.scalars().first()
    if not prompt:
        raise ValueError(f"Estructura con ID {prompt_id} no encontrada.")

    # Idempotent: already soft-deleted
    if prompt.deleted_at is not None:
        return {
            "ok": True,
            "status": "deleted",
            "action": "already_deleted",
            "archived": True,
            "prompt_id": prompt_id,
            "message": "La estructura ya había sido retirada previamente."
        }

    # Fetch prompt versions
    version_ids_stmt = select(PromptVersion.id).where(PromptVersion.prompt_id == prompt_id)
    version_ids_res = await db.execute(version_ids_stmt)
    version_ids = version_ids_res.scalars().all()

    # 2. Check for historical usage
    has_history = False

    # A. Referenced in Analysis directly?
    analysis_stmt = select(Analysis.analysis_id).where(Analysis.prompt_id == prompt_id).limit(1)
    if (await db.execute(analysis_stmt)).scalar() is not None:
        has_history = True

    # B. Referenced in mass jobs?
    if not has_history:
        jobs_stmt = select(MassEvaluationJob.job_id).where(MassEvaluationJob.prompt_id == prompt_id).limit(1)
        if (await db.execute(jobs_stmt)).scalar() is not None:
            has_history = True

    # C. Referenced in mass results?
    if not has_history:
        results_stmt = select(MassEvaluationResult.mass_analysis_id).where(MassEvaluationResult.prompt_id == prompt_id).limit(1)
        if (await db.execute(results_stmt)).scalar() is not None:
            has_history = True

    # D. Referenced in automations?
    if not has_history:
        auto_stmt = select(MassAnalysisAutomation.automation_id).where(MassAnalysisAutomation.prompt_id == prompt_id).limit(1)
        if (await db.execute(auto_stmt)).scalar() is not None:
            has_history = True

    # E. Versions used in analyses or trainer evaluations?
    if not has_history and version_ids:
        v_analysis_stmt = select(Analysis.analysis_id).where(Analysis.prompt_version_id.in_(version_ids)).limit(1)
        if (await db.execute(v_analysis_stmt)).scalar() is not None:
            has_history = True
        if not has_history:
            v_train_stmt = select(TrainingCallEvaluation.evaluation_id).where(TrainingCallEvaluation.prompt_version_id.in_(version_ids)).limit(1)
            if (await db.execute(v_train_stmt)).scalar() is not None:
                has_history = True

    # F. Referenced in Trainer evaluation configs?
    if not has_history:
        trainer_stmt = select(TrainerEvaluationConfig.config_id).where(TrainerEvaluationConfig.speech_structure_id == prompt_id).limit(1)
        if (await db.execute(trainer_stmt)).scalar() is not None:
            has_history = True

    now = datetime.now(timezone.utc)

    # 3. If it has history -> SAFE SOFT-DELETE / ARCHIVAL
    if has_history:
        prompt.is_active = False
        prompt.is_archived = True
        prompt.deleted_at = now
        prompt.archived_at = now
        prompt.archived_by_email = user_email

        # Mark versions as archived
        if version_ids:
            await db.execute(
                update(PromptVersion)
                .where(PromptVersion.prompt_id == prompt_id)
                .values(is_archived=True, archived_at=now, archived_by_email=user_email)
            )

        # Deactivate active automations pointing to this prompt
        await db.execute(
            update(MassAnalysisAutomation)
            .where(MassAnalysisAutomation.prompt_id == prompt_id, MassAnalysisAutomation.is_active == True)
            .values(is_active=False)
        )

        # Deactivate active mass evaluation jobs pointing to this prompt
        await db.execute(
            update(MassEvaluationJob)
            .where(MassEvaluationJob.prompt_id == prompt_id, MassEvaluationJob.is_active == True)
            .values(is_active=False)
        )

        # Clean up structure permissions for this prompt
        await db.execute(delete(StructurePermission).where(
            StructurePermission.structure_type == "specific",
            StructurePermission.structure_id == prompt_id
        ))

        await db.commit()
        return {
            "ok": True,
            "status": "deleted",
            "action": "archived",
            "archived": True,
            "prompt_id": prompt_id,
            "message": "Estructura retirada del servicio y archivada para preservar el histórico de análisis."
        }

    # 4. If NO history -> CLEAN PHYSICAL DELETE
    criteria_ids_stmt = select(PromptCriterion.criterion_id).where(PromptCriterion.prompt_id == prompt_id)
    criteria_ids_res = await db.execute(criteria_ids_stmt)
    criteria_ids = criteria_ids_res.scalars().all()
    if criteria_ids:
        await db.execute(delete(PromptCriterionTypology).where(PromptCriterionTypology.criterion_id.in_(criteria_ids)))
        await db.execute(delete(PromptCriterion).where(PromptCriterion.prompt_id == prompt_id))

    if version_ids:
        await db.execute(delete(PromptVersion).where(PromptVersion.prompt_id == prompt_id))

    await db.execute(delete(StructurePermission).where(
        StructurePermission.structure_type == "specific",
        StructurePermission.structure_id == prompt_id
    ))

    await db.delete(prompt)
    await db.commit()
    return {
        "ok": True,
        "status": "deleted",
        "action": "hard_deleted",
        "deleted": True,
        "prompt_id": prompt_id,
        "message": "Estructura sin histórico eliminada definitivamente."
    }
