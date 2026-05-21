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
    """Hard delete a prompt structure if it has no associated jobs or results and is not active."""
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

    # B. Referenced in mass jobs?
    jobs_stmt = select(MassEvaluationJob).where(MassEvaluationJob.prompt_id == prompt_id)
    jobs_res = await db.execute(jobs_stmt)
    if jobs_res.scalars().first():
        raise ValueError(
            "No se puede borrar esta estructura porque tiene histórico asociado. Puedes archivarla."
        )

    # C. Referenced in mass results?
    results_stmt = select(MassEvaluationResult).where(MassEvaluationResult.prompt_id == prompt_id)
    results_res = await db.execute(results_stmt)
    if results_res.scalars().first():
        raise ValueError(
            "No se puede borrar esta estructura porque tiene histórico asociado. Puedes archivarla."
        )

    # D. Perform hard delete
    await db.delete(prompt)
    await db.commit()
    return {"ok": True, "status": "deleted", "prompt_id": prompt_id}
