"""
Criteria service — business logic for bm_prompt_criteria.
"""
import logging
from sqlalchemy import select, update, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.prompts import Prompt, PromptVersion
from app.models.typologies import Typology
from app.schemas.criteria import CriteriaGroupedOut, SaveCriterionRequest
from app.schemas.typologies import CriterionTypologyAssociation
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# Valid criterion types
CRITERION_TYPES = ["score_1_10", "percentage", "boolean", "text", "category", "number"]


async def _ensure_typology_associations(db: AsyncSession, criterion: PromptCriterion):
    """Ensure the criterion is associated with the active typologies for the prompt's service."""
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
        
        # Get existing associations to avoid duplicates
        existing_stmt = select(PromptCriterionTypology.typology_id).where(PromptCriterionTypology.criterion_id == criterion.criterion_id)
        existing_res = await db.execute(existing_stmt)
        existing_ids = set(existing_res.scalars().all())
        
        for t_id in typology_ids:
            if t_id not in existing_ids:
                new_assoc = PromptCriterionTypology(
                    criterion_id=criterion.criterion_id,
                    typology_id=t_id
                )
                db.add(new_assoc)


async def get_criteria_grouped(db: AsyncSession, prompt_id: int, include_deleted: bool = False) -> CriteriaGroupedOut:
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
    query = select(PromptCriterion).where(PromptCriterion.prompt_id == prompt_id)
    if not include_deleted:
        query = query.where(PromptCriterion.deleted_at.is_(None))
    
    query = query.order_by(PromptCriterion.order_index.asc().nullslast(), PromptCriterion.criterion_id.asc())

    criteria_result = await db.execute(query)
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
            PromptCriterion.deleted_at.is_(None),
        )
        .order_by(PromptCriterion.order_index.asc().nullslast(), PromptCriterion.criterion_id.asc())
    )
    return result.scalars().all()


async def save_criterion(db: AsyncSession, body: SaveCriterionRequest) -> PromptCriterion:
    """Create, restore, or update a criterion."""
    # Gather potential conflicting criteria in the same prompt
    conflict_conditions = [PromptCriterion.criterion_key == body.criterion_key]
    if body.output_key:
        conflict_conditions.append(PromptCriterion.output_key == body.output_key)
    if body.feed_key:
        conflict_conditions.append(PromptCriterion.feed_key == body.feed_key)
        
    query = select(PromptCriterion).where(
        PromptCriterion.prompt_id == body.prompt_id,
        or_(*conflict_conditions)
    )
    res = await db.execute(query)
    conflicting_items = res.scalars().all()

    # --- Case A: Updating existing by explicit ID ---
    if body.criterion_id:
        result = await db.execute(
            select(PromptCriterion).where(PromptCriterion.criterion_id == body.criterion_id)
        )
        criterion = result.scalars().first()
        if not criterion:
            logger.warning(f"Criterion with ID {body.criterion_id} not found.")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Criterio con ID {body.criterion_id} no encontrado."
            )
            
        # Check conflicts with OTHER criteria
        other_conflicts = [c for c in conflicting_items if c.criterion_id != body.criterion_id]
        if other_conflicts:
            # Active conflict
            active_conflicts = [c for c in other_conflicts if c.deleted_at is None]
            if active_conflicts:
                conflict_desc = []
                for c in active_conflicts:
                    if c.criterion_key == body.criterion_key:
                        conflict_desc.append(f"clave '{body.criterion_key}'")
                    if body.output_key and c.output_key == body.output_key:
                        conflict_desc.append(f"output_key '{body.output_key}'")
                    if body.feed_key and c.feed_key == body.feed_key:
                        conflict_desc.append(f"feed_key '{body.feed_key}'")
                msg = f"Conflicto: Ya existe otro criterio activo con {', '.join(conflict_desc)}."
                logger.warning(msg)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=msg
                )
            
            # Soft-deleted conflict
            deleted_conflicts = [c for c in other_conflicts if c.deleted_at is not None]
            if deleted_conflicts:
                conflict_desc = []
                for c in deleted_conflicts:
                    if c.criterion_key == body.criterion_key:
                        conflict_desc.append(f"clave '{body.criterion_key}' (eliminado)")
                    if body.output_key and c.output_key == body.output_key:
                        conflict_desc.append(f"output_key '{body.output_key}' (eliminado)")
                    if body.feed_key and c.feed_key == body.feed_key:
                        conflict_desc.append(f"feed_key '{body.feed_key}' (eliminado)")
                msg = f"Conflicto: Existe un criterio eliminado con {', '.join(conflict_desc)}. Restaure ese criterio o use otras claves."
                logger.warning(msg)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=msg
                )
                
        # Update existing
        logger.info(f"Updating existing active criterion (ID: {body.criterion_id}, key: '{body.criterion_key}').")
        for field, value in body.model_dump(exclude={"criterion_id"}).items():
            setattr(criterion, field, value)
        criterion.deleted_at = None
        criterion.deleted_by_email = None
        await db.commit()
        await db.refresh(criterion)
        return criterion

    # --- Case B: Creating new (no ID passed) ---
    if conflicting_items:
        # Check active conflicts
        active_items = [c for c in conflicting_items if c.deleted_at is None]
        if active_items:
            conflict_desc = []
            for c in active_items:
                if c.criterion_key == body.criterion_key:
                    conflict_desc.append(f"clave '{body.criterion_key}'")
                if body.output_key and c.output_key == body.output_key:
                    conflict_desc.append(f"output_key '{body.output_key}'")
                if body.feed_key and c.feed_key == body.feed_key:
                    conflict_desc.append(f"feed_key '{body.feed_key}'")
            msg = f"Conflicto: Ya existe un criterio activo con {', '.join(conflict_desc)} para este prompt."
            logger.warning(msg)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=msg
            )
            
        # Check soft-deleted conflicts -> RESTORE
        soft_deleted_items = [c for c in conflicting_items if c.deleted_at is not None]
        if soft_deleted_items:
            criterion = soft_deleted_items[0]
            logger.info(f"Restoring soft-deleted criterion (ID: {criterion.criterion_id}, key: '{criterion.criterion_key}') to active state.")
            
            # Restore & update
            criterion.is_active = True
            criterion.deleted_at = None
            criterion.deleted_by_email = None
            for field, value in body.model_dump(exclude={"criterion_id"}).items():
                setattr(criterion, field, value)
                
            await _ensure_typology_associations(db, criterion)
            await db.commit()
            await db.refresh(criterion)
            return criterion
            
    # Create new
    logger.info(f"Creating new criterion (key: '{body.criterion_key}').")
    criterion = PromptCriterion(**body.model_dump(exclude={"criterion_id"}))
    db.add(criterion)
    await db.flush() # Flush to generate criterion_id
    
    await _ensure_typology_associations(db, criterion)
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


async def delete_criterion(db: AsyncSession, criterion_id: int, performed_by_email: str | None = None) -> dict[str, Any]:
    """
    Delete or soft-delete a criterion.
    - If it's `tipo_llamada` and required, block.
    - If it's used in analysis results or mass evaluations, soft-delete.
    - Otherwise, hard-delete.
    """
    from datetime import datetime, timezone
    from app.models.analyses import AnalysisResult
    from app.models.mass_evaluations import MassEvaluationResult

    # Get criterion
    c_stmt = select(PromptCriterion).where(PromptCriterion.criterion_id == criterion_id)
    c_res = await db.execute(c_stmt)
    criterion = c_res.scalars().first()

    if not criterion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Criterio con ID {criterion_id} no encontrado."
        )

    # Protection for tipo_llamada
    if criterion.criterion_key == "tipo_llamada" and criterion.is_required:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede borrar el item tipo_llamada porque es necesario para clasificar la llamada."
        )

    # Check if used in AnalysisResult
    used_in_analysis_stmt = select(AnalysisResult.result_id).where(AnalysisResult.criterion_id == criterion_id).limit(1)
    used_in_analysis_res = await db.execute(used_in_analysis_stmt)
    is_used = used_in_analysis_res.scalar() is not None

    # Check if used in MassEvaluationResult
    if not is_used and criterion.prompt_id:
        used_in_mass_stmt = select(MassEvaluationResult.mass_analysis_id).where(MassEvaluationResult.prompt_id == criterion.prompt_id).limit(1)
        used_in_mass_res = await db.execute(used_in_mass_stmt)
        is_used = used_in_mass_res.scalar() is not None

    action = ""

    if is_used:
        # Soft delete
        criterion.is_active = False
        criterion.deleted_at = datetime.now(timezone.utc)
        criterion.deleted_by_email = performed_by_email
        db.add(criterion)
        
        # We must also clean up typologies relations logically or physically
        await db.execute(
            delete(PromptCriterionTypology).where(PromptCriterionTypology.criterion_id == criterion_id)
        )
        action = "soft_deleted"
    else:
        # Hard delete
        await db.delete(criterion)
        action = "deleted"

    await db.commit()

    return {
        "ok": True,
        "criterion_id": criterion_id,
        "action": action,
        "message": "Item eliminado correctamente"
    }
