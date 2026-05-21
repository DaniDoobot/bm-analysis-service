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
    is_active: bool | None = None,
    include_archived: bool = False,
) -> list[dict]:
    """Return all prompts joined with their current version."""
    stmt = select(Prompt).where(Prompt.deleted_at == None)
    if not include_archived:
        stmt = stmt.where(Prompt.is_archived == False)
    
    if not prompt_type:
        prompt_type = "audio"
    stmt = stmt.where(Prompt.prompt_type == prompt_type)
    if base_structure_id is not None:
        stmt = stmt.where(Prompt.base_structure_id == base_structure_id)
    if base_structure_key is not None:
        stmt = stmt.where(Prompt.base_structure_key == base_structure_key)
    if is_active is not None:
        stmt = stmt.where(Prompt.is_active == is_active)
    stmt = stmt.order_by(Prompt.prompt_id)

    result = await db.execute(stmt)
    prompts = result.scalars().all()

    out = []
    for p in prompts:
        current = await _get_current_version(db, p.prompt_id)
        
        prompt_text = current.prompt if current else None
        if not prompt_text and p.prompt_id:
            prompt_text = await build_fallback_prompt_from_criteria(db, p.prompt_id)

        # Check for active draft in bm_prompt_drafts
        from app.models.drafts import PromptDraft
        draft_stmt = select(PromptDraft).where(
            PromptDraft.prompt_id == p.prompt_id,
            PromptDraft.status.in_(["draft", "pending", "active"])
        ).order_by(PromptDraft.updated_at.desc()).limit(1)
        draft_res = await db.execute(draft_stmt)
        active_draft = draft_res.scalars().first()

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
            "version_name": current.version_name if current else None,
            "prompt": prompt_text,
            "base_structure_id": p.base_structure_id,
            "base_structure_key": p.base_structure_key,
            "base_structure_name": p.base_structure_name,
            "service_id": p.service_id,
            "service_key": p.service.service_key if p.service else None,
            "service_name": p.service.service_name if p.service else None,

            # Aliases for frontend compatibility
            "name": p.prompt_name,
            "version": current.version_label if current else None,
            "label": current.version_label if current else None,
            "base": p.base_structure_name,

            # Archiving info
            "is_archived": p.is_archived,
            "archived_at": p.archived_at,
            "archived_by_email": p.archived_by_email,
            "deleted_at": p.deleted_at,

            # Draft state distinction
            "has_active_draft": active_draft is not None,
            "draft_status": active_draft.status if active_draft else None,
            "active_draft_id": active_draft.draft_id if active_draft else None,
            "current_version_label": current.version_label if current else None,
        }
        out.append(row)
    return out


async def get_active_prompt(db: AsyncSession, prompt_type: str) -> dict | None:
    """Return the active prompt for a given type with its current version."""
    result = await db.execute(
        select(Prompt).where(
            Prompt.prompt_type == prompt_type,
            Prompt.is_active == True,
            Prompt.is_archived == False,
            Prompt.deleted_at == None,
        ).order_by(Prompt.prompt_id.desc()).limit(1)
    )
    p = result.scalars().first()
    if not p:
        return None

    current = await _get_current_version(db, p.prompt_id)
    
    prompt_text = current.prompt if current else None
    if not prompt_text and p.prompt_id:
        prompt_text = await build_fallback_prompt_from_criteria(db, p.prompt_id)

    return {
        "prompt_id": p.prompt_id,
        "prompt_name": p.prompt_name,
        "prompt_type": p.prompt_type,
        "description": p.description,
        "prompt_version_id": current.id if current else None,
        "current_version_id": current.id if current else None,  # Keep for backwards compatibility just in case
        "version_label": current.version_label if current else None,
        "prompt": prompt_text,
        "base_structure_id": p.base_structure_id,
        "base_structure_key": p.base_structure_key,
        "base_structure_name": p.base_structure_name,
        "service_id": p.service_id,
        "service_key": p.service.service_key if p.service else None,
        "service_name": p.service.service_name if p.service else None,

        # Aliases for frontend compatibility
        "name": p.prompt_name,
        "version": current.version_label if current else None,
        "label": current.version_label if current else None,
        "base": p.base_structure_name,

        # Archiving info
        "is_archived": p.is_archived,
        "archived_at": p.archived_at,
        "archived_by_email": p.archived_by_email,
    }


async def list_versions(
    db: AsyncSession,
    prompt_id: int,
    include_archived: bool = False,
) -> list[dict]:
    """Return versions for a prompt. By default hides archived versions. Pass include_archived=True for full history."""
    # Fetch prompt to include base structure in versions list
    prompt_res = await db.execute(select(Prompt).where(Prompt.prompt_id == prompt_id))
    p = prompt_res.scalars().first()

    stmt = (
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id)
    )
    if not include_archived:
        stmt = stmt.where(PromptVersion.is_archived == False)
    stmt = stmt.order_by(PromptVersion.created_at.desc())

    result = await db.execute(stmt)
    versions = result.scalars().all()

    out = []
    for v in versions:
        prompt_text = v.prompt
        if not prompt_text and v.prompt_id:
            prompt_text = await build_fallback_prompt_from_criteria(db, v.prompt_id)

        out.append({
            "id": v.id,
            "prompt_id": v.prompt_id,
            "prompt": prompt_text,
            "version_label": v.version_label,
            "version_name": v.version_name,
            "updated_by": v.updated_by,
            "updated_by_email": v.updated_by_email,
            "change_note": v.change_note,
            "source": v.source,
            "is_current": v.is_current,
            "is_archived": v.is_archived,
            "archived_at": v.archived_at,
            "archived_by_email": v.archived_by_email,
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


async def update_prompt_current(
    db: AsyncSession,
    prompt_id: int,
    prompt_text: str,
    prompt_name: str | None = None,
    description: str | None = None,
    updated_by: str | None = None,
    updated_by_email: str | None = None,
) -> dict:
    """
    Overwrite the content of the current version of a prompt without creating a new visible version.
    This is the 'save/edit in place' operation — no version history is exposed to the user.
    Internally it creates a snapshot with source='overwrite' which is immediately archived.
    """
    # 1. Fetch prompt
    prompt_res = await db.execute(select(Prompt).where(Prompt.prompt_id == prompt_id))
    prompt_obj = prompt_res.scalars().first()
    if not prompt_obj:
        raise ValueError(f"Prompt {prompt_id} not found.")

    # 2. Update prompt metadata if provided
    if prompt_name:
        prompt_obj.prompt_name = prompt_name
    if description is not None:
        prompt_obj.description = description
    db.add(prompt_obj)

    # 3. Unset current flag on all existing versions
    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id)
        .values(is_current=False)
    )

    # 4. Create the new overwrite version (immediately marked as current, source=overwrite)
    now_label = _generate_label()
    new_version = PromptVersion(
        prompt_id=prompt_id,
        prompt=prompt_text,
        version_label=now_label,
        version_name=f"Guardado {now_label}",
        updated_by=updated_by,
        updated_by_email=updated_by_email,
        change_note="Guardado directo (sobrescritura)",
        source="overwrite",
        is_current=True,
        is_archived=False,
    )
    db.add(new_version)
    await db.commit()
    await db.refresh(new_version)
    await db.refresh(prompt_obj)

    return {
        "ok": True,
        "prompt_id": prompt_id,
        "prompt_name": prompt_obj.prompt_name,
        "current_version_id": new_version.id,
        "version_label": new_version.version_label,
        "prompt": new_version.prompt,
    }


async def duplicate_prompt(
    db: AsyncSession,
    source_prompt_id: int,
    prompt_name: str,
    description: str | None = None,
    created_by: str | None = None,
    created_by_email: str | None = None,
) -> dict:
    """
    Creates a fully independent copy of a prompt with its current content and active criteria.
    The new prompt starts as inactive (not published). Its criteria and typology relations are copied.
    """
    from app.models.criteria import PromptCriterion, PromptCriterionTypology

    # 1. Fetch source prompt
    src_res = await db.execute(select(Prompt).where(Prompt.prompt_id == source_prompt_id))
    src_prompt = src_res.scalars().first()
    if not src_prompt:
        raise ValueError(f"Source prompt {source_prompt_id} not found.")

    # 2. Fetch current version text
    current_version = await _get_current_version(db, source_prompt_id)
    current_text = current_version.prompt if current_version else None

    # 3. Create new prompt record
    new_prompt = Prompt(
        prompt_name=prompt_name,
        prompt_type=src_prompt.prompt_type,
        description=description or src_prompt.description,
        is_active=False,  # New prompt starts inactive by default
        is_archived=False,
        created_by=created_by,
        created_by_email=created_by_email,
        base_structure_id=src_prompt.base_structure_id,
        base_structure_key=src_prompt.base_structure_key,
        base_structure_name=src_prompt.base_structure_name,
        service_id=src_prompt.service_id,
    )
    db.add(new_prompt)
    await db.flush()

    # 4. Create initial version for the new prompt
    version_label = _generate_label()
    new_version = PromptVersion(
        prompt_id=new_prompt.prompt_id,
        prompt=current_text,
        version_label=version_label,
        version_name=f"Copia de {src_prompt.prompt_name}",
        updated_by=created_by,
        updated_by_email=created_by_email,
        change_note=f"Duplicado desde prompt_id={source_prompt_id}",
        source="duplicate",
        is_current=True,
        is_archived=False,
    )
    db.add(new_version)
    await db.flush()

    # 5. Copy active criteria and their typology relations
    criteria_res = await db.execute(
        select(PromptCriterion).where(
            PromptCriterion.prompt_id == source_prompt_id,
            PromptCriterion.is_active == True,
        ).order_by(PromptCriterion.order_index.asc())
    )
    src_criteria = criteria_res.scalars().all()

    copied_criteria_count = 0
    for src_c in src_criteria:
        new_c = PromptCriterion(
            prompt_id=new_prompt.prompt_id,
            criterion_key=src_c.criterion_key,
            criterion_name=src_c.criterion_name,
            criterion_description=src_c.criterion_description,
            criterion_type=src_c.criterion_type,
            output_key=src_c.output_key,
            feed_key=src_c.feed_key,
            allowed_values=src_c.allowed_values,
            applies_to_types=src_c.applies_to_types,
            order_index=src_c.order_index,
            is_required=src_c.is_required,
            is_active=True,
        )
        db.add(new_c)
        await db.flush()
        copied_criteria_count += 1

        # Copy typology associations
        ct_res = await db.execute(
            select(PromptCriterionTypology).where(
                PromptCriterionTypology.criterion_id == src_c.criterion_id
            )
        )
        for src_ct in ct_res.scalars().all():
            new_ct = PromptCriterionTypology(
                criterion_id=new_c.criterion_id,
                typology_id=src_ct.typology_id,
            )
            db.add(new_ct)

    await db.commit()
    await db.refresh(new_prompt)
    await db.refresh(new_version)

    return {
        "ok": True,
        "prompt_id": new_prompt.prompt_id,
        "prompt_name": new_prompt.prompt_name,
        "prompt_type": new_prompt.prompt_type,
        "service_id": new_prompt.service_id,
        "base_structure_id": new_prompt.base_structure_id,
        "current_version_id": new_version.id,
        "is_active": new_prompt.is_active,
        "copied_criteria_count": copied_criteria_count,
        "source_prompt_id": source_prompt_id,
    }


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

    # Get parent prompt and set it active, deactivating other prompts of the same type
    prompt_res = await db.execute(select(Prompt).where(Prompt.prompt_id == version.prompt_id))
    prompt_obj = prompt_res.scalars().first()
    if prompt_obj:
        prompt_obj.is_active = True
        await db.execute(
            update(Prompt)
            .where(Prompt.prompt_type == prompt_obj.prompt_type, Prompt.prompt_id != prompt_obj.prompt_id)
            .values(is_active=False)
        )

    await db.commit()
    await db.refresh(version)
    return version


# ── Prompt Base Structures CRUD ───────────────────────────────────────────────

async def list_base_structures(
    db: AsyncSession,
    prompt_type: str | None = None,
    include_archived: bool = False,
) -> list[PromptBaseStructure]:
    """Return base structures. By default only active ones; pass include_archived=True to see all."""
    stmt = select(PromptBaseStructure)
    if not include_archived:
        stmt = stmt.where(PromptBaseStructure.is_active == True)
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
        prompt_type="text", # Normalizamos a 'text' para que todas las estructuras base sean de texto
        base_prompt=body.base_prompt,
        default_criteria=None, # Discarded for simplified structures (no items)
        is_active=True,
        created_by=body.created_by,
        created_by_email=body.created_by_email,
        service_id=body.service_id,
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
        struct.prompt_type = "text"
    if body.base_prompt is not None:
        struct.base_prompt = body.base_prompt
    
    struct.default_criteria = None # Always force None to clear out legacy items
    
    if body.is_active is not None:
        struct.is_active = body.is_active
    if body.service_id is not None:
        struct.service_id = body.service_id

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
        is_active=body.activate,
        created_by=body.created_by,
        created_by_email=body.created_by_email,
        base_structure_id=struct.id,
        base_structure_key=struct.structure_key,
        base_structure_name=struct.structure_name,
        service_id=struct.service_id,
    )
    db.add(new_prompt)
    await db.flush()

    # If explicitly requested to activate, deactivate all other prompts of the same type
    if body.activate:
        await db.execute(
            update(Prompt)
            .where(Prompt.prompt_type == body.prompt_type, Prompt.prompt_id != new_prompt.prompt_id)
            .values(is_active=False)
        )


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
    
    # 4. Copy default criteria: DEPRECATED & IGNORED.
    # Base structures no longer contain criteria. Specific structures always start with 0 criteria items.
    criteria_count = 0

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
        "service_id": new_prompt.service_id,
    }


async def refresh_boston_medical_base_structure(db: AsyncSession) -> dict[str, Any]:
    """
    Manually refreshes the 'boston_medical_audio' structure from prompt 1
    (text only).
    """
    # 1. Fetch active prompt version 1
    current_version = await _get_current_version(db, 1)
    if not current_version or not current_version.prompt:
        raise ValueError("No active prompt version found for prompt_id=1.")

    # 2. Query base structure 'boston_medical_audio'
    result = await db.execute(
        select(PromptBaseStructure).where(PromptBaseStructure.structure_key == "boston_medical_audio")
    )
    struct = result.scalars().first()
    if not struct:
        raise ValueError("Base structure 'boston_medical_audio' not found in database.")

    # 3. Update only the text and set default_criteria to None
    struct.base_prompt = current_version.prompt
    struct.default_criteria = None
    await db.commit()
    await db.refresh(struct)

    return {
        "ok": True,
        "message": "Boston Medical base structure successfully refreshed (text only) from active prompt 1.",
        "structure_id": struct.id,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_current_version(db: AsyncSession, prompt_id: int) -> PromptVersion | None:
    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id, PromptVersion.is_current == True)
        .order_by(PromptVersion.id.desc())
        .limit(1)
    )
    return result.scalars().first()


def _generate_label() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("v%Y%m%d-%H%M")


async def build_fallback_prompt_from_criteria(db: AsyncSession, prompt_id: int) -> str:
    """Build a legible preview text representation of the criteria for a prompt when no prompt text is generated."""
    from app.models.criteria import PromptCriterion
    stmt = (
        select(PromptCriterion)
        .where(PromptCriterion.prompt_id == prompt_id, PromptCriterion.is_active == True)
        .order_by(PromptCriterion.order_index.asc(), PromptCriterion.criterion_id.asc())
    )
    res = await db.execute(stmt)
    items = res.scalars().all()
    
    if not items:
        return "Estructura sin texto de prompt generado y sin criterios activos."
        
    lines = [
        "### ESTRUCTURA DE EVALUACIÓN PERSONALIZADA (VISTA PREVIA DE CRITERIOS)",
        "Esta estructura no tiene un prompt de texto consolidado, pero está compuesta por los siguientes criterios activos de evaluación:",
        ""
    ]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item.criterion_name} (output_key: {item.output_key})")
        lines.append(f"   - Tipo: {item.criterion_type}")
        if item.criterion_description:
            lines.append(f"   - Descripción: {item.criterion_description}")
        if item.feed_key:
            lines.append(f"   - Clave de feedback: {item.feed_key}")
        if item.allowed_values:
            lines.append(f"   - Valores permitidos: {item.allowed_values}")
        lines.append("")
        
    return "\n".join(lines)

