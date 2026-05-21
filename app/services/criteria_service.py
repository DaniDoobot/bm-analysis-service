"""
Criteria service — business logic for bm_prompt_criteria.
"""
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.prompts import Prompt, PromptVersion
from app.models.typologies import Typology
from app.schemas.criteria import CriteriaGroupedOut, SaveCriterionRequest
from app.schemas.typologies import CriterionTypologyAssociation
from fastapi import HTTPException, status

# Valid criterion types
CRITERION_TYPES = ["score_1_10", "percentage", "boolean", "text", "category", "number"]


async def get_criteria_grouped(db: AsyncSession, prompt_id: int) -> CriteriaGroupedOut:
    """Return active criteria grouped by type, plus the current prompt text."""
    # Get current prompt text
    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id, PromptVersion.is_current == True)
        .limit(1)
    )
    version = result.scalars().first()
    prompt_text = version.prompt if version else None

    # Get criteria
    criteria_result = await db.execute(
        select(PromptCriterion)
        .where(PromptCriterion.prompt_id == prompt_id)
        .order_by(PromptCriterion.order_index.asc().nullslast(), PromptCriterion.criterion_id.asc())
    )
    all_criteria = criteria_result.scalars().all()

    # Group by type
    grouped: dict[str, list] = {t: [] for t in CRITERION_TYPES}
    for c in all_criteria:
        key = c.criterion_type or "text"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(c)

    return CriteriaGroupedOut(
        prompt=prompt_text,
        criteria=list(all_criteria),
        grouped=grouped,
    )


async def get_active_criteria(db: AsyncSession, prompt_id: int) -> list[PromptCriterion]:
    """Return only active criteria for a prompt, used during analysis."""
    result = await db.execute(
        select(PromptCriterion)
        .where(
            PromptCriterion.prompt_id == prompt_id,
            PromptCriterion.is_active == True,
        )
        .order_by(PromptCriterion.order_index.asc().nullslast(), PromptCriterion.criterion_id.asc())
    )
    return result.scalars().all()


async def save_criterion(db: AsyncSession, body: SaveCriterionRequest) -> PromptCriterion:
    """Create or update a criterion."""
    if body.criterion_id:
        result = await db.execute(
            select(PromptCriterion).where(PromptCriterion.criterion_id == body.criterion_id)
        )
        criterion = result.scalars().first()
        if criterion:
            # Update existing
            for field, value in body.model_dump(exclude={"criterion_id"}).items():
                setattr(criterion, field, value)
            await db.commit()
            await db.refresh(criterion)
            return criterion

    # Create new
    criterion = PromptCriterion(**body.model_dump(exclude={"criterion_id"}))
    db.add(criterion)
    await db.flush() # Flush to generate criterion_id

    # Auto-associate new criterion with all active typologies of the prompt's service
    service_id = None
    if criterion.prompt_id:
        p_stmt = select(Prompt).where(Prompt.prompt_id == criterion.prompt_id)
        p_res = await db.execute(p_stmt)
        prompt = p_res.scalars().first()
        if prompt:
            service_id = prompt.service_id

    # Fallback to 'front' service
    if not service_id:
        from app.models.services import Service
        s_stmt = select(Service.service_id).where(Service.service_key == "front")
        s_res = await db.execute(s_stmt)
        service_id = s_res.scalar()

    if service_id:
        t_stmt = select(Typology.typology_id).where(Typology.service_id == service_id, Typology.is_active == True)
        t_res = await db.execute(t_stmt)
        typology_ids = t_res.scalars().all()
        for t_id in typology_ids:
            new_assoc = PromptCriterionTypology(
                criterion_id=criterion.criterion_id,
                typology_id=t_id
            )
            db.add(new_assoc)

    await db.commit()
    await db.refresh(criterion)
    return criterion


async def toggle_criterion(db: AsyncSession, criterion_id: int, is_active: bool) -> None:
    await db.execute(
        update(PromptCriterion)
        .where(PromptCriterion.criterion_id == criterion_id)
        .values(is_active=is_active)
    )
    await db.commit()


async def get_criterion_typologies(db: AsyncSession, criterion_id: int) -> list[CriterionTypologyAssociation]:
    """Retrieve all active typologies for the criterion's service with association status."""
    # 1. Fetch criterion
    c_stmt = select(PromptCriterion).where(PromptCriterion.criterion_id == criterion_id)
    c_res = await db.execute(c_stmt)
    criterion = c_res.scalars().first()
    if not criterion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Criterio con ID {criterion_id} no encontrado."
        )

    # 2. Get prompt service
    service_id = None
    if criterion.prompt_id:
        p_stmt = select(Prompt).where(Prompt.prompt_id == criterion.prompt_id)
        p_res = await db.execute(p_stmt)
        prompt = p_res.scalars().first()
        if prompt:
            service_id = prompt.service_id

    # Fallback to 'front' service if not found
    if not service_id:
        from app.models.services import Service
        s_stmt = select(Service.service_id).where(Service.service_key == "front")
        s_res = await db.execute(s_stmt)
        service_id = s_res.scalar()

    if not service_id:
        return []

    # 3. Retrieve all active typologies of this service
    t_stmt = select(Typology).where(Typology.service_id == service_id, Typology.is_active == True).order_by(Typology.sort_order.asc())
    t_res = await db.execute(t_stmt)
    typologies = t_res.scalars().all()

    # 4. Retrieve currently associated typologies
    assoc_stmt = select(PromptCriterionTypology.typology_id).where(PromptCriterionTypology.criterion_id == criterion_id)
    assoc_res = await db.execute(assoc_stmt)
    associated_ids = set(assoc_res.scalars().all())

    # 5. Build associations list
    associations = []
    for t in typologies:
        associations.append(
            CriterionTypologyAssociation(
                typology_id=t.typology_id,
                typology_key=t.typology_key,
                typology_name=t.typology_name,
                is_associated=t.typology_id in associated_ids
            )
        )
    return associations


async def update_criterion_typologies(db: AsyncSession, criterion_id: int, typology_ids: list[int]) -> dict[str, Any]:
    """Update typology associations for a specific criterion."""
    # 1. Fetch criterion
    c_stmt = select(PromptCriterion).where(PromptCriterion.criterion_id == criterion_id)
    c_res = await db.execute(c_stmt)
    criterion = c_res.scalars().first()
    if not criterion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Criterio con ID {criterion_id} no encontrado."
        )

    # 2. In transaction: delete old associations and insert new ones
    await db.execute(
        delete(PromptCriterionTypology).where(PromptCriterionTypology.criterion_id == criterion_id)
    )

    # Adding new
    for t_id in typology_ids:
        new_assoc = PromptCriterionTypology(
            criterion_id=criterion_id,
            typology_id=t_id
        )
        db.add(new_assoc)

    await db.commit()
    return {"ok": True, "detail": f"Asociación de tipologías para el criterio {criterion_id} actualizada correctamente."}
