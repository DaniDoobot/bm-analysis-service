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

async def delete_prompt(db: AsyncSession, prompt_id: int) -> dict:
    """Hard delete a prompt structure if it has no associated analyses, jobs, evaluations or runs."""
    from sqlalchemy import delete
    from app.models.analyses import Analysis
    from app.models.criteria import PromptCriterion, PromptCriterionTypology
    from app.models.personalized_training import TrainingCallEvaluation
    from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationResult, MassAnalysisAutomation
    from app.models.prompts import StructurePermission

    # 1. Load prompt
    stmt = select(Prompt).where(Prompt.prompt_id == prompt_id, Prompt.deleted_at == None)
    result = await db.execute(stmt)
    prompt = result.scalars().first()
    if not prompt:
        raise ValueError(f"Estructura con ID {prompt_id} no encontrada.")

    # 2. Safeguard checks
    # A. Active in production?
    if prompt.is_active:
        raise ValueError("No se puede borrar la estructura activa en producción.")

    # B. Referenced in Analysis directly?
    analysis_stmt = select(Analysis.analysis_id).where(Analysis.prompt_id == prompt_id).limit(1)
    analysis_res = await db.execute(analysis_stmt)
    if analysis_res.scalar():
        raise ValueError("No se puede borrar porque tiene análisis asociados. Archívala en su lugar.")

    # C. Referenced in mass jobs?
    jobs_stmt = select(MassEvaluationJob).where(MassEvaluationJob.prompt_id == prompt_id)
    jobs_res = await db.execute(jobs_stmt)
    if jobs_res.scalars().first():
        raise ValueError("No se puede borrar porque tiene configuraciones de evaluaciones masivas asociadas. Archívala en su lugar.")

    # D. Referenced in mass results?
    results_stmt = select(MassEvaluationResult).where(MassEvaluationResult.prompt_id == prompt_id)
    results_res = await db.execute(results_stmt)
    if results_res.scalars().first():
        raise ValueError("No se puede borrar porque tiene resultados de evaluaciones masivas asociados. Archívala en su lugar.")

    # E. Referenced in automations?
    auto_stmt = select(MassAnalysisAutomation.automation_id).where(MassAnalysisAutomation.prompt_id == prompt_id).limit(1)
    auto_res = await db.execute(auto_stmt)
    if auto_res.scalar():
        raise ValueError("No se puede borrar porque tiene automaciones de análisis asociadas. Archívala en su lugar.")

    # Fetch prompt versions
    version_ids_stmt = select(PromptVersion.id).where(PromptVersion.prompt_id == prompt_id)
    version_ids_res = await db.execute(version_ids_stmt)
    version_ids = version_ids_res.scalars().all()

    if version_ids:
        # F. Version used in analyses?
        v_analysis_stmt = select(Analysis.analysis_id).where(Analysis.prompt_version_id.in_(version_ids)).limit(1)
        v_analysis_res = await db.execute(v_analysis_stmt)
        if v_analysis_res.scalar():
            raise ValueError("No se puede borrar porque tiene versiones usadas en análisis. Archívala en su lugar.")

        # G. Version used in training evaluations?
        v_train_stmt = select(TrainingCallEvaluation.evaluation_id).where(TrainingCallEvaluation.prompt_version_id.in_(version_ids)).limit(1)
        v_train_res = await db.execute(v_train_stmt)
        if v_train_res.scalar():
            raise ValueError("No se puede borrar porque tiene versiones usadas en entrenamientos de agentes. Archívala en su lugar.")

    # 3. Clean up child records
    # A. Criteria and their typology associations
    criteria_ids_stmt = select(PromptCriterion.criterion_id).where(PromptCriterion.prompt_id == prompt_id)
    criteria_ids_res = await db.execute(criteria_ids_stmt)
    criteria_ids = criteria_ids_res.scalars().all()
    if criteria_ids:
        await db.execute(delete(PromptCriterionTypology).where(PromptCriterionTypology.criterion_id.in_(criteria_ids)))
        await db.execute(delete(PromptCriterion).where(PromptCriterion.prompt_id == prompt_id))

    # B. Prompt versions
    await db.execute(delete(PromptVersion).where(PromptVersion.prompt_id == prompt_id))

    # C. Structure permissions
    await db.execute(delete(StructurePermission).where(
        StructurePermission.structure_type == "specific",
        StructurePermission.structure_id == prompt_id
    ))

    # D. Finally hard delete the prompt record itself
    await db.delete(prompt)
    await db.commit()
    return {"ok": True, "status": "deleted", "prompt_id": prompt_id}
