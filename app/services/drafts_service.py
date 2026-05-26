"""
Drafts service.
"""
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drafts import PromptDraft


def _criteria_are_equal(db_criteria: list, draft_criteria: list) -> bool:
    if len(db_criteria) != len(draft_criteria):
        return False
        
    list_a = sorted(db_criteria, key=lambda x: (x.order_index or 0, x.output_key or ""))
    list_b = sorted(draft_criteria, key=lambda x: (x.get("order_index") or 0, x.get("output_key") or ""))
    
    fields_to_compare = [
        "criterion_key", "criterion_name", "criterion_description",
        "criterion_type", "output_key", "feed_key", "allowed_values",
        "applies_to_types", "is_required"
    ]
    
    for ca, cb in zip(list_a, list_b):
        for field in fields_to_compare:
            val_a = getattr(ca, field, None)
            val_b = cb.get(field, None)
            
            if isinstance(val_a, list) or isinstance(val_b, list):
                if list(val_a or []) != list(val_b or []):
                    return False
            else:
                if val_a != val_b:
                    if not val_a and not val_b:
                        continue
                    return False
    return True


async def get_draft(
    db: AsyncSession, prompt_id: int, user_email: str | None = None
) -> dict | None:
    """Return the most recent active draft for a prompt."""
    query = select(PromptDraft).where(
        PromptDraft.prompt_id == prompt_id,
        PromptDraft.status == "draft",
    )
    if user_email:
        query = query.where(PromptDraft.updated_by_email == user_email)

    query = query.order_by(PromptDraft.updated_at.desc()).limit(1)
    result = await db.execute(query)
    draft = result.scalars().first()
    if not draft:
        return None

    # Check if the draft is obsolete (older than the current version or has no real differences)
    from app.models.prompts import PromptVersion
    current_res = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id, PromptVersion.is_current == True)
        .order_by(PromptVersion.id.desc())
        .limit(1)
    )
    current = current_res.scalars().first()
    
    if current:
        # 1. Check if draft is older than or equal to current version
        if draft.updated_at <= current.created_at:
            from sqlalchemy import delete
            await db.execute(delete(PromptDraft).where(PromptDraft.draft_id == draft.draft_id))
            await db.commit()
            return None
            
        # 2. Check if the draft has no real changes compared to current version
        draft_data = draft.draft_data or {}
        draft_prompt = draft_data.get("prompt")
        current_prompt = current.prompt
        
        prompt_changed = (draft_prompt or "").strip() != (current_prompt or "").strip()
        
        from app.models.criteria import PromptCriterion
        crit_stmt = select(PromptCriterion).where(
            PromptCriterion.prompt_id == prompt_id,
            PromptCriterion.is_active == True
        )
        res_crit = await db.execute(crit_stmt)
        db_criteria = res_crit.scalars().all()
        draft_criteria = draft_data.get("criteria", []) or []
        
        criteria_changed = not _criteria_are_equal(db_criteria, draft_criteria)
        
        if not prompt_changed and not criteria_changed:
            from sqlalchemy import delete
            await db.execute(delete(PromptDraft).where(PromptDraft.draft_id == draft.draft_id))
            await db.commit()
            return None

    return {
        "draft_id": draft.draft_id,
        "prompt_id": draft.prompt_id,
        "draft_name": draft.draft_name,
        "draft_data": draft.draft_data,
        "updated_by": draft.updated_by,
        "updated_by_email": draft.updated_by_email,
        "status": draft.status,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


async def save_draft(db: AsyncSession, body) -> PromptDraft:
    """Save (upsert) a draft — creates new or updates existing active draft for that user."""
    # Look for existing draft to update
    query = select(PromptDraft).where(
        PromptDraft.prompt_id == body.prompt_id,
        PromptDraft.status == "draft",
    )
    if body.updated_by_email:
        query = query.where(PromptDraft.updated_by_email == body.updated_by_email)
    query = query.order_by(PromptDraft.updated_at.desc()).limit(1)

    result = await db.execute(query)
    draft = result.scalars().first()

    if draft:
        draft.draft_name = body.draft_name
        draft.draft_data = body.draft_data
        draft.updated_by = body.updated_by
        draft.updated_by_email = body.updated_by_email
    else:
        draft = PromptDraft(
            prompt_id=body.prompt_id,
            draft_name=body.draft_name,
            draft_data=body.draft_data,
            updated_by=body.updated_by,
            updated_by_email=body.updated_by_email,
            status="draft",
        )
        db.add(draft)

    await db.commit()
    await db.refresh(draft)
    return draft


async def set_draft_status(db: AsyncSession, draft_id: int, status: str) -> None:
    # Fetch draft to get metadata
    result = await db.execute(select(PromptDraft).where(PromptDraft.draft_id == draft_id))
    draft = result.scalars().first()
    if not draft:
        return
        
    # Clean up existing conflicts for this user/prompt and status to avoid unique constraints
    if status in ("discarded", "published") and draft.updated_by_email:
        from sqlalchemy import delete
        await db.execute(
            delete(PromptDraft).where(
                PromptDraft.prompt_id == draft.prompt_id,
                PromptDraft.updated_by_email == draft.updated_by_email,
                PromptDraft.status == status,
                PromptDraft.draft_id != draft_id
            )
        )
        
    draft.status = status
    await db.commit()


async def publish_draft(
    db: AsyncSession,
    draft_id: int,
    updated_by: str | None = None,
    updated_by_email: str | None = None,
):
    """Atomically publish a draft: compile it into a new active PromptVersion and mark previous versions as inactive."""
    # 1. Fetch the active draft
    result = await db.execute(
        select(PromptDraft).where(PromptDraft.draft_id == draft_id)
    )
    draft = result.scalars().first()
    if not draft:
        raise ValueError(f"Borrador con ID {draft_id} no encontrado.")

    # 2. Extract values from draft_data
    draft_data = draft.draft_data or {}
    prompt_text = draft_data.get("prompt")
    version_name = draft_data.get("version_name") or draft.draft_name or "Publicación desde borrador"
    change_note = draft_data.get("change_note") or "Publicado desde borrador"
    
    # 3. Import Prompts and PromptVersions models
    from app.models.prompts import Prompt, PromptVersion
    from datetime import datetime, timezone
    
    # 4. Generate label
    now = datetime.now(timezone.utc)
    version_label = now.strftime("v%Y%m%d-%H%M%S")
    
    # 5. Deactivate previous versions of this prompt
    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.prompt_id == draft.prompt_id)
        .values(is_current=False)
    )
    
    # 6. Create a new PromptVersion
    new_version = PromptVersion(
        prompt_id=draft.prompt_id,
        prompt=prompt_text,
        version_label=version_label,
        version_name=version_name,
        updated_by=updated_by or draft.updated_by,
        updated_by_email=updated_by_email or draft.updated_by_email,
        change_note=change_note,
        source="draft",
        is_current=True,
    )
    db.add(new_version)
    
    # 7. Synchronize criteria if provided in draft_data
    criteria_list = draft_data.get("criteria")
    if criteria_list and isinstance(criteria_list, list):
        from app.models.criteria import PromptCriterion
        # Mark old criteria of this prompt as inactive
        await db.execute(
            update(PromptCriterion)
            .where(PromptCriterion.prompt_id == draft.prompt_id)
            .values(is_active=False)
        )
        # Insert new criteria
        for idx, crit in enumerate(criteria_list):
            new_crit = PromptCriterion(
                prompt_id=draft.prompt_id,
                criterion_key=crit.get("criterion_key"),
                criterion_name=crit.get("criterion_name"),
                criterion_description=crit.get("criterion_description"),
                criterion_type=crit.get("criterion_type"),
                output_key=crit.get("output_key"),
                feed_key=crit.get("feed_key"),
                allowed_values=crit.get("allowed_values"),
                applies_to_types=crit.get("applies_to_types"),
                order_index=crit.get("order_index", idx),
                is_required=crit.get("is_required", False),
                is_active=True,
            )
            db.add(new_crit)
            
    # 8. Update parent Prompt to be active, and deactivate other prompts of the same type
    prompt_res = await db.execute(select(Prompt).where(Prompt.prompt_id == draft.prompt_id))
    prompt_obj = prompt_res.scalars().first()
    if prompt_obj:
        prompt_obj.is_active = True
        await db.execute(
            update(Prompt)
            .where(Prompt.prompt_type == prompt_obj.prompt_type, Prompt.prompt_id != prompt_obj.prompt_id)
            .values(is_active=False)
        )
        
    # 8.5. Archive previous 'published' drafts of this user/prompt to satisfy the unique constraint
    if draft.updated_by_email:
        await db.execute(
            update(PromptDraft)
            .where(
                PromptDraft.prompt_id == draft.prompt_id,
                PromptDraft.updated_by_email == draft.updated_by_email,
                PromptDraft.status == "published",
            )
            .values(status="discarded")
        )

    # 8.7. Discard (delete) all other active drafts for this prompt
    from sqlalchemy import delete
    await db.execute(
        delete(PromptDraft).where(
            PromptDraft.prompt_id == draft.prompt_id,
            PromptDraft.draft_id != draft_id,
            PromptDraft.status == "draft"
        )
    )

    # 9. Update draft status
    draft.status = "published"
    
    await db.commit()
    await db.refresh(new_version)
    return new_version
