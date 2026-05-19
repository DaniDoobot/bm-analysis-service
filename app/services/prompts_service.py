"""
Prompts service — business logic for prompts and versions.
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompts import Prompt, PromptVersion, PromptBaseStructure
from app.schemas.prompts import (
    SavePromptRequest,
    PromptBaseStructureCreate,
    PromptBaseStructureUpdate,
    CreateFromBaseRequest,
)


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
    version_name = body.version_name or body.generated_name

    new_version = PromptVersion(
        prompt_id=body.prompt_id,
        prompt=body.prompt,
        version_label=version_label,
        version_name=version_name,
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


# ── Prompt Base Structures CRUD ───────────────────────────────────────────────

async def list_base_structures(db: AsyncSession, prompt_type: str | None = None) -> list[PromptBaseStructure]:
    """Return all active base structures, optionally filtered by prompt_type."""
    stmt = select(PromptBaseStructure).where(PromptBaseStructure.is_active == True)
    if prompt_type:
        stmt = stmt.where(PromptBaseStructure.prompt_type == prompt_type)
    stmt = stmt.order_by(PromptBaseStructure.id.asc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_base_structure(db: AsyncSession, structure_id: int) -> PromptBaseStructure | None:
    """Get a base structure by ID."""
    result = await db.execute(
        select(PromptBaseStructure).where(PromptBaseStructure.id == structure_id)
    )
    return result.scalars().first()


async def create_base_structure(db: AsyncSession, body: PromptBaseStructureCreate) -> PromptBaseStructure:
    """Create a new prompt base structure."""
    new_struct = PromptBaseStructure(
        structure_key=body.structure_key,
        structure_name=body.structure_name,
        description=body.description,
        prompt_type=body.prompt_type,
        base_prompt=body.base_prompt,
        default_criteria=body.default_criteria,
        is_active=True,
        created_by=body.created_by,
        created_by_email=body.created_by_email,
    )
    db.add(new_struct)
    await db.commit()
    await db.refresh(new_struct)
    return new_struct


async def update_base_structure(
    db: AsyncSession, structure_id: int, body: PromptBaseStructureUpdate
) -> PromptBaseStructure | None:
    """Update an existing prompt base structure."""
    result = await db.execute(
        select(PromptBaseStructure).where(PromptBaseStructure.id == structure_id)
    )
    struct = result.scalars().first()
    if not struct:
        return None

    if body.structure_name is not None:
        struct.structure_name = body.structure_name
    if body.description is not None:
        struct.description = body.description
    if body.prompt_type is not None:
        struct.prompt_type = body.prompt_type
    if body.base_prompt is not None:
        struct.base_prompt = body.base_prompt
    if body.default_criteria is not None:
        struct.default_criteria = body.default_criteria
    if body.is_active is not None:
        struct.is_active = body.is_active

    await db.commit()
    await db.refresh(struct)
    return struct


async def create_prompt_from_base(db: AsyncSession, body: CreateFromBaseRequest) -> dict[str, Any]:
    """
    Create a new prompt from a base structure.
    Returns prompt detail including prompt_id, prompt_version_id, criteria count.
    """
    # 1. Fetch base structure
    struct = await get_base_structure(db, body.base_structure_id)
    if not struct:
        raise ValueError(f"Base structure with ID {body.base_structure_id} not found.")

    # 2. Create the prompt record
    new_prompt = Prompt(
        prompt_name=body.prompt_name,
        prompt_type=body.prompt_type,
        description=struct.description,
        is_active=True,
        created_by=body.created_by,
        created_by_email=body.created_by_email,
    )
    db.add(new_prompt)
    await db.commit()
    await db.refresh(new_prompt)

    # 3. Create the first prompt version
    version_label = _generate_label()
    new_version = PromptVersion(
        prompt_id=new_prompt.prompt_id,
        prompt=struct.base_prompt,
        version_label=version_label,
        version_name="Versión Inicial",
        updated_by=body.created_by,
        updated_by_email=body.created_by_email,
        change_note=f"Creado desde estructura base: {struct.structure_name}",
        source="from_base",
        is_current=True,
    )
    db.add(new_version)
    
    # 4. Copy default criteria if copy_default_criteria=True and structure is not blank
    criteria_count = 0
    if body.copy_default_criteria and struct.structure_key != "blank" and struct.default_criteria:
        from app.models.criteria import PromptCriterion
        for c in struct.default_criteria:
            new_crit = PromptCriterion(
                prompt_id=new_prompt.prompt_id,
                criterion_key=c.get("criterion_key"),
                criterion_name=c.get("criterion_name"),
                criterion_description=c.get("criterion_description"),
                criterion_type=c.get("criterion_type"),
                output_key=c.get("output_key"),
                feed_key=c.get("feed_key"),
                allowed_values=c.get("allowed_values"),
                applies_to_types=c.get("applies_to_types"),
                order_index=c.get("order_index", 100),
                is_required=c.get("is_required", False),
                is_active=c.get("is_active", True)
            )
            db.add(new_crit)
            criteria_count += 1

    await db.commit()
    await db.refresh(new_version)

    return {
        "ok": True,
        "prompt_id": new_prompt.prompt_id,
        "prompt_version_id": new_version.id,
        "prompt_name": new_prompt.prompt_name,
        "prompt_type": new_prompt.prompt_type,
        "prompt": new_version.prompt,
        "criteria_count": criteria_count,
    }


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

