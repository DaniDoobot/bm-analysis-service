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


async def list_prompts(
    db: AsyncSession,
    prompt_type: str | None = None,
    base_structure_id: int | None = None,
    base_structure_key: str | None = None,
) -> list[dict]:
    """Return all prompts joined with their current version."""
    stmt = select(Prompt)
    if prompt_type:
        stmt = stmt.where(Prompt.prompt_type == prompt_type)
    if base_structure_id is not None:
        stmt = stmt.where(Prompt.base_structure_id == base_structure_id)
    if base_structure_key is not None:
        stmt = stmt.where(Prompt.base_structure_key == base_structure_key)
    stmt = stmt.order_by(Prompt.prompt_id)

    result = await db.execute(stmt)
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
            "base_structure_id": p.base_structure_id,
            "base_structure_key": p.base_structure_key,
            "base_structure_name": p.base_structure_name,
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
        "base_structure_id": p.base_structure_id,
        "base_structure_key": p.base_structure_key,
        "base_structure_name": p.base_structure_name,
    }


async def list_versions(db: AsyncSession, prompt_id: int) -> list[dict]:
    # Fetch prompt to include base structure in versions list
    prompt_res = await db.execute(select(Prompt).where(Prompt.prompt_id == prompt_id))
    p = prompt_res.scalars().first()

    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.created_at.desc())
    )
    versions = result.scalars().all()

    out = []
    for v in versions:
        out.append({
            "id": v.id,
            "prompt_id": v.prompt_id,
            "prompt": v.prompt,
            "version_label": v.version_label,
            "version_name": v.version_name,
            "updated_by": v.updated_by,
            "updated_by_email": v.updated_by_email,
            "change_note": v.change_note,
            "source": v.source,
            "is_current": v.is_current,
            "created_at": v.created_at,
            "base_structure_id": p.base_structure_id if p else None,
            "base_structure_key": p.base_structure_key if p else None,
            "base_structure_name": p.base_structure_name if p else None,
        })
    return out



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


async def assign_base_structure(db: AsyncSession, prompt_id: int, base_structure_id: int) -> dict[str, Any]:
    """
    Assign a base structure to an existing prompt (only updates the prompt metadata references).
    Does not modify versions, text, criteria or active prompt.
    """
    # 1. Fetch prompt
    result = await db.execute(select(Prompt).where(Prompt.prompt_id == prompt_id))
    prompt_obj = result.scalars().first()
    if not prompt_obj:
        raise ValueError(f"Prompt with ID {prompt_id} not found.")

    # 2. Fetch base structure
    struct = await get_base_structure(db, base_structure_id)
    if not struct:
        raise ValueError(f"Base structure with ID {base_structure_id} not found.")

    # 3. Update the fields
    prompt_obj.base_structure_id = struct.id
    prompt_obj.base_structure_key = struct.structure_key
    prompt_obj.base_structure_name = struct.structure_name

    await db.commit()
    await db.refresh(prompt_obj)

    return {
        "ok": True,
        "message": f"Successfully assigned base structure '{struct.structure_name}' to prompt {prompt_id}.",
        "prompt_id": prompt_obj.prompt_id,
        "base_structure_id": prompt_obj.base_structure_id,
        "base_structure_key": prompt_obj.base_structure_key,
        "base_structure_name": prompt_obj.base_structure_name,
    }


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
        base_structure_id=struct.id,
        base_structure_key=struct.structure_key,
        base_structure_name=struct.structure_name,
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


async def refresh_boston_medical_base_structure(db: AsyncSession) -> dict[str, Any]:
    """
    Manually refreshes the 'boston_medical_audio' structure from prompt 1
    and its active criteria.
    """
    # 1. Fetch active prompt version 1
    current_version = await _get_current_version(db, 1)
    if not current_version or not current_version.prompt:
        raise ValueError("No active prompt version found for prompt_id=1.")

    # 2. Fetch active criteria of prompt 1
    from app.models.criteria import PromptCriterion
    crit_res = await db.execute(
        select(PromptCriterion)
        .where(PromptCriterion.prompt_id == 1, PromptCriterion.is_active == True)
        .order_by(PromptCriterion.order_index.asc())
    )
    crit_list = crit_res.scalars().all()

    # 3. Query base structure 'boston_medical_audio'
    result = await db.execute(
        select(PromptBaseStructure).where(PromptBaseStructure.structure_key == "boston_medical_audio")
    )
    struct = result.scalars().first()
    if not struct:
        raise ValueError("Base structure 'boston_medical_audio' not found in database.")

    # 4. Map criteria
    mapped = []
    for c in crit_list:
        mapped.append({
            "criterion_key": c.criterion_key,
            "criterion_name": c.criterion_name,
            "criterion_description": c.criterion_description,
            "criterion_type": c.criterion_type,
            "output_key": c.output_key,
            "feed_key": c.feed_key,
            "is_active": c.is_active,
            "is_required": c.is_required,
            "order_index": c.order_index
        })

    # 5. Update and commit
    struct.base_prompt = current_version.prompt
    struct.default_criteria = mapped
    await db.commit()
    await db.refresh(struct)

    return {
        "ok": True,
        "message": "Boston Medical base structure successfully refreshed from active prompt 1 and its active criteria.",
        "structure_id": struct.id,
        "criteria_count": len(mapped),
        "default_criteria": mapped
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

