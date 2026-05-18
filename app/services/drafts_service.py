"""
Drafts service.
"""
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drafts import PromptDraft


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
    await db.execute(
        update(PromptDraft).where(PromptDraft.draft_id == draft_id).values(status=status)
    )
    await db.commit()
