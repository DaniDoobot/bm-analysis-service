"""
Prompts service — business logic for prompts and versions.
"""
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompts import Prompt, PromptVersion
from app.schemas.prompts import SavePromptRequest


async def list_prompts(db: AsyncSession) -> list[dict]:
    """Return all prompts joined with their current version."""
    result = await db.execute(
        select(Prompt).order_by(Prompt.prompt_id)
    )
    prompts = result.scalars().all()

    out = []
    for p in prompts:
        current = await _get_current_version(db, p.prompt_id)
        row = {
            "prompt_id": p.prompt_id,
            "prompt_name": p.prompt_name,
            "prompt_type": p.prompt_type,
            "description": p.description,
            "is_active": p.is_active,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            "current_version_id": current.id if current else None,
            "version_label": current.version_label if current else None,
            "prompt": current.prompt if current else None,
        }
        out.append(row)
    return out


async def get_active_prompt(db: AsyncSession, prompt_type: str) -> dict | None:
    """Return the active prompt for a given type with its current version."""
    result = await db.execute(
        select(Prompt).where(
            Prompt.prompt_type == prompt_type,
            Prompt.is_active == True,
        ).order_by(Prompt.prompt_id.desc()).limit(1)
    )
    p = result.scalars().first()
    if not p:
        return None

    current = await _get_current_version(db, p.prompt_id)
    return {
        "prompt_id": p.prompt_id,
        "prompt_name": p.prompt_name,
        "prompt_type": p.prompt_type,
        "description": p.description,
        "prompt_version_id": current.id if current else None,
        "current_version_id": current.id if current else None,  # Keep for backwards compatibility just in case
        "version_label": current.version_label if current else None,
        "prompt": current.prompt if current else None,
    }


async def list_versions(db: AsyncSession, prompt_id: int) -> list[PromptVersion]:
    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.created_at.desc())
    )
    return result.scalars().all()


async def save_prompt_version(db: AsyncSession, body: SavePromptRequest) -> PromptVersion:
    """Create a new version and mark it as current."""
    # Unset is_current for all previous versions of this prompt
    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.prompt_id == body.prompt_id)
        .values(is_current=False)
    )

    # Build version label if not provided
    version_label = body.version_label or _generate_label()

    new_version = PromptVersion(
        prompt_id=body.prompt_id,
        prompt=body.prompt,
        version_label=version_label,
        updated_by=body.updated_by,
        updated_by_email=body.updated_by_email,
        change_note=body.change_note,
        source=body.source or "manual",
        is_current=True,
    )
    db.add(new_version)
    await db.commit()
    await db.refresh(new_version)
    return new_version


async def activate_version(db: AsyncSession, version_id: int) -> PromptVersion | None:
    """Set a version as current and unset others of the same prompt."""
    result = await db.execute(
        select(PromptVersion).where(PromptVersion.id == version_id)
    )
    version = result.scalars().first()
    if not version:
        return None

    # Unset others
    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.prompt_id == version.prompt_id)
        .values(is_current=False)
    )
    version.is_current = True
    await db.commit()
    await db.refresh(version)
    return version


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_current_version(db: AsyncSession, prompt_id: int) -> PromptVersion | None:
    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id, PromptVersion.is_current == True)
        .limit(1)
    )
    return result.scalars().first()


def _generate_label() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("v%Y%m%d-%H%M")
