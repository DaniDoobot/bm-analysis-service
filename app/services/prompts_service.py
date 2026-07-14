"""
Prompts service — business logic for prompts and versions.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import re

logger = logging.getLogger(__name__)


class PromptValidationError(Exception):
    """Exception raised when prompt validation fails due to size limits or duplicate keys."""
    pass

def clean_whitespaces(text: str) -> str:
    """
    Normalizes line endings, strips trailing spaces, limits consecutive blank lines to max 2,
    and removes internal double/triple spaces without altering leading indentation.
    """
    if not text:
        return text
    # Normalize line endings
    text = text.replace("\r\n", "\n")
    lines = []
    for line in text.splitlines():
        # Strip trailing space
        r_stripped = line.rstrip()
        if not r_stripped:
            lines.append("")
            continue
        # Deduplicate internal spaces (3 or more) without affecting leading indent
        stripped = r_stripped.lstrip()
        indent = r_stripped[:len(r_stripped) - len(stripped)]
        cleaned_content = re.sub(r' {3,}', ' ', stripped)
        lines.append(indent + cleaned_content)
    text = "\n".join(lines)
    # Limit consecutive newlines to maximum 2
    text = re.sub(r'\n\n\n+', '\n\n', text)
    return text


def sanitize_static_prompt_sections(prompt_text: str) -> tuple[str, dict]:
    """
    Parses prompt_text into sections starting with '###'.
    Deduplicates sections starting with known headers:
    - DEFINICIÓN DE TIPOS DE LLAMADA
    - PRIORIDADES EN CASO DE CONFLICTO
    - CRITERIOS DE ANÁLISIS
    - FORMATO DE SALIDA JSON
    Keeps only the first occurrence of each section, preserving order.
    Returns the sanitized prompt text and a dict with statistics on removed duplicates.
    """
    header_pattern = re.compile(r'(?:\r?\n|^)(###\s+[^\r\n]+)')
    
    matches = list(header_pattern.finditer(prompt_text))
    if not matches:
        return prompt_text, {"removed_count": 0, "details": {}}
        
    sections = []
    # Add intro section (before the first header)
    first_match = matches[0]
    intro = prompt_text[:first_match.start()]
    sections.append(("", intro))
    
    # Slice the remaining sections
    for i in range(len(matches)):
        start = matches[i].start()
        header_text = matches[i].group(1).strip()
        
        end = matches[i+1].start() if i + 1 < len(matches) else len(prompt_text)
        content = prompt_text[start:end]
        sections.append((header_text, content))
        
    # Deduplicate known sections
    seen_canonical_headers = set()
    sanitized_sections = []
    removed_count = 0
    details = {}
    
    def canonicalize(h: str) -> str:
        h = h.lower()
        h = re.sub(r'\s+', ' ', h)
        h = h.replace('á', 'a').replace('é', 'e').replace('í', 'i').replace('ó', 'o').replace('ú', 'u')
        h = re.sub(r'[^a-z0-9 ]', '', h)
        return h.strip()
        
    known_keys = {
        "definicion de tipos de llamada": "### DEFINICIÓN DE TIPOS DE LLAMADA",
        "prioridades en caso de conflicto": "### PRIORIDADES EN CASO DE CONFLICTO",
        "criterios de analisis": "### CRITERIOS DE ANÁLISIS",
        "formato de salida json": "### FORMATO DE SALIDA JSON"
    }
    
    for header, content in sections:
        if not header:
            sanitized_sections.append(content)
            continue
            
        canon = canonicalize(header)
        matched_key = None
        for k in known_keys:
            if k in canon:
                matched_key = k
                break
                
        if matched_key:
            if matched_key in seen_canonical_headers:
                removed_count += 1
                details[matched_key] = details.get(matched_key, 0) + 1
            else:
                seen_canonical_headers.add(matched_key)
                sanitized_sections.append(content)
        else:
            # Unknown header, keep it
            sanitized_sections.append(content)
            
    sanitized_text = "".join(sanitized_sections)
    return sanitized_text, {"removed_count": removed_count, "details": details}


def sync_output_format_in_prompt(
    prompt_text: str,
    active_criteria: list,
    typologies: list,
) -> tuple[str, bool]:
    """
    Localiza la sección del formato de salida JSON y la actualiza con los criterios activos
    y tipologías actuales. Retorna (new_prompt_text, changed).
    """
    if not prompt_text:
        return prompt_text, False

    from app.services.prompt_builder import _build_output_format
    import re

    # Match lines that are headers for response/output format
    header_pattern = re.compile(
        r"^(?:###?\s+)?(?:FORMATO\s+DE\s+(?:RESPUESTA|SALIDA(?:\s+JSON)?))\b",
        re.IGNORECASE | re.MULTILINE
    )

    matches = list(header_pattern.finditer(prompt_text))
    
    schema_block = _build_output_format(active_criteria, typologies)
    header_title = "### FORMATO DE SALIDA JSON"
    new_section = (
        f"{header_title}\n\n"
        f"Responde EXCLUSIVAMENTE con un JSON siguiendo exactamente la siguiente estructura y valores permitidos. "
        f"No incluyas información adicional ni comentarios fuera del JSON. "
        f"No utilices claves legacy como campo_1, campo_2, etc.\n\n"
        f"{schema_block}"
    )

    if matches:
        last_match = matches[-1]
        prefix = prompt_text[:last_match.start()].rstrip()
        new_prompt_text = f"{prefix}\n\n{new_section}"
    else:
        new_prompt_text = f"{prompt_text.rstrip()}\n\n{new_section}"

    normalized_old = clean_whitespaces(prompt_text)
    normalized_new = clean_whitespaces(new_prompt_text)
    
    changed = (normalized_old != normalized_new)
    return new_prompt_text, changed


def build_criteria_text_block(active_criteria: list) -> str:
    """
    Builds the criteria text block from active criteria list.
    Formatted identically to sync_criterion_block format.
    """
    import re
    blocks = []
    for c in active_criteria:
        # Build block header: e.g. "- [type] Name (output_key: key)"
        header_bullet = "- "
        id_prefix = f"[ID: {c.criterion_id}] " if getattr(c, "criterion_id", None) is not None else ""
        header = f"{header_bullet}{id_prefix}[{c.criterion_type or 'text'}] {c.criterion_name}"
        keys_meta = []
        if getattr(c, "output_key", None):
            keys_meta.append(f"output_key: {c.output_key}")
        if getattr(c, "feed_key", None):
            keys_meta.append(f"feed_key: {c.feed_key}")
        if keys_meta:
            header += " (" + ", ".join(keys_meta) + ")"
            
        block_lines = [header]
        desc = getattr(c, "criterion_description", "") or ""
        indent = "  "
        for line in desc.splitlines():
            if line.strip():
                block_lines.append(indent + line.strip())
            else:
                block_lines.append("")
                
        # Reconstruct allowed values and typologies
        if getattr(c, "allowed_values", None):
            vals = c.allowed_values
            vals_str = ", ".join(vals) if isinstance(vals, list) else str(vals)
            block_lines.append(f"{indent}Valores permitidos: {vals_str}")
            
        if getattr(c, "applies_to_types", None):
            typos = c.applies_to_types
            typos_str = ", ".join(typos) if isinstance(typos, list) else str(typos)
            block_lines.append(f"{indent}Tipologías aplicables: {typos_str}")
        else:
            block_lines.append(f"{indent}Tipologías aplicables: Todas")
            
        blocks.append("\n".join(block_lines))
        
    return "\n\n".join(blocks)


def replace_criteria_block_with_delimiters(prompt_text: str, new_criteria_block: str) -> tuple[str, bool]:
    """
    Replaces the criteria section in prompt_text with the new_criteria_block enclosed in
    <!-- BM_CRITERIA_BLOCK_START --> and <!-- BM_CRITERIA_BLOCK_END -->.
    Idempotent: removes all legacy duplicate blocks and inserts exactly one.
    """
    import re
    start_tag = "<!-- BM_CRITERIA_BLOCK_START -->"
    end_tag = "<!-- BM_CRITERIA_BLOCK_END -->"
    
    pattern = re.compile(
        rf"{re.escape(start_tag)}.*?{re.escape(end_tag)}",
        re.DOTALL | re.IGNORECASE
    )
    
    delimited_block = f"{start_tag}\n{new_criteria_block}\n{end_tag}"
    
    if pattern.search(prompt_text):
        # Delimiters exist. Clean any duplicates and replace
        matches = list(pattern.finditer(prompt_text))
        if len(matches) > 1:
            # Clean duplicate delimited blocks
            new_prompt_text = ""
            last_idx = 0
            for idx, match in enumerate(matches):
                new_prompt_text += prompt_text[last_idx:match.start()]
                if idx == 0:
                    new_prompt_text += delimited_block
                last_idx = match.end()
            new_prompt_text += prompt_text[last_idx:]
            return new_prompt_text, True
        else:
            old_block = matches[0].group(0)
            old_cleaned = clean_whitespaces(old_block.replace("\r\n", "\n")).strip()
            new_cleaned = clean_whitespaces(delimited_block.replace("\r\n", "\n")).strip()
            if old_cleaned != new_cleaned:
                print(f"INNER_COMPARE_DIFF: old_cleaned len={len(old_cleaned)} | new_cleaned len={len(new_cleaned)}")
                for idx, (c1, c2) in enumerate(zip(old_cleaned, new_cleaned)):
                    if c1 != c2:
                        print(f"INNER_COMPARE_DIFF index {idx}: c1={repr(c1)} | c2={repr(c2)}")
                        print("OLD CONTEXT:", repr(old_cleaned[max(0, idx-20):idx+30]))
                        print("NEW CONTEXT:", repr(new_cleaned[max(0, idx-20):idx+30]))
                        break
                new_prompt_text = prompt_text[:matches[0].start()] + delimited_block + prompt_text[matches[0].end():]
                return new_prompt_text, True
            return prompt_text, False
            
    # Delimiters not found. Search for legacy criteria section headers.
    header_pattern = re.compile(
        r"^(###?\s+)?(?:CRITERIOS?\s+DE\s+(?:ANÁLISIS|ANALISIS|EVALUACIÓN|EVALUACION))\b",
        re.IGNORECASE | re.MULTILINE
    )
    
    matches = list(header_pattern.finditer(prompt_text))
    if matches:
        header_match = matches[-1]
        start_idx = header_match.end()
        
        # Look for the next section header after criteria section
        next_header_pattern = re.compile(r"^\s*#", re.MULTILINE)
        next_header_match = next_header_pattern.search(prompt_text, pos=start_idx)
        
        if next_header_match:
            end_idx = next_header_match.start()
            prefix = prompt_text[:start_idx].rstrip()
            suffix = prompt_text[end_idx:].lstrip()
            new_prompt_text = f"{prefix}\n\n{delimited_block}\n\n{suffix}"
            return new_prompt_text, True
        else:
            prefix = prompt_text[:start_idx].rstrip()
            new_prompt_text = f"{prefix}\n\n{delimited_block}"
            return new_prompt_text, True
            
    # Fallback: append at the end
    new_prompt_text = f"{prompt_text.rstrip()}\n\n{delimited_block}"
    return new_prompt_text, True


def sync_prompt_text_with_criteria_list(
    prompt_text: str,
    active_criteria: list,
    typologies: list,
) -> tuple[str, bool]:
    """
    Rebuilds prompt text by replacing the criteria section and the output format JSON.
    Uses delimiters for the criteria section to ensure idempotence.
    """
    if not prompt_text:
        return prompt_text, False

    # Apply dynamic prompt sanitization for legacy/hardcoded typologies block
    from app.services.prompt_builder import sanitize_legacy_typologies_block
    prompt_text = sanitize_legacy_typologies_block(prompt_text, typologies)

    # Rebuild criteria section
    new_criteria_block = build_criteria_text_block(active_criteria)
    new_criteria_block = sanitize_legacy_typologies_block(new_criteria_block, typologies, prepend_if_missing=False)
    prompt_text, block_changed = replace_criteria_block_with_delimiters(prompt_text, new_criteria_block)

    # Rebuild JSON output format
    prompt_text, format_changed = sync_output_format_in_prompt(prompt_text, active_criteria, typologies)

    # Clean whitespaces
    prompt_text = clean_whitespaces(prompt_text)

    changed = block_changed or format_changed
    return prompt_text, changed


async def sync_prompt_text_with_active_criteria(
    db: AsyncSession,
    prompt_id: int,
    prompt_text: str,
) -> tuple[str, bool]:
    """
    Sincroniza el texto del prompt con los criterios activos de la base de datos de manera idempotente.
    Usa delimitadores para el bloque de criterios y actualiza el formato JSON de salida.
    Valida límites defensivos de longitud.
    """
    if not prompt_text:
        return prompt_text, False

    from app.services.criteria_service import get_active_criteria
    from app.models.typologies import Typology
    from app.models.services import Service
    from sqlalchemy import select

    # 1. Fetch prompt and resolve service_id
    prompt_res = await db.execute(select(Prompt).where(Prompt.prompt_id == prompt_id))
    p = prompt_res.scalars().first()
    service_id = p.service_id if p else None

    if not service_id:
        s_res = await db.execute(select(Service.service_id).where(Service.service_key == "front"))
        service_id = s_res.scalar()

    # 2. Fetch active typologies (base structure priority, fallback to service)
    from app.models.prompts import BaseStructureTypology
    typologies = []
    if p and p.base_structure_id:
        t_res = await db.execute(
            select(Typology)
            .join(BaseStructureTypology, BaseStructureTypology.typology_id == Typology.typology_id)
            .where(
                BaseStructureTypology.base_structure_id == p.base_structure_id,
                Typology.is_active == True
            )
            .order_by(Typology.sort_order.asc())
        )
        typologies = t_res.scalars().all()

    if not typologies and service_id:
        t_res = await db.execute(
            select(Typology)
            .where(Typology.service_id == service_id, Typology.is_active == True)
            .order_by(Typology.sort_order.asc())
        )
        typologies = t_res.scalars().all()

    # 3. Fetch active criteria
    active_criteria = await get_active_criteria(db, prompt_id)

    # LÍMITE DEFENSIVO: Criterios únicos máximos
    MAX_CRITERIA_LIMIT = 100
    if len(active_criteria) > MAX_CRITERIA_LIMIT:
        raise PromptValidationError(
            f"Prompt build failed: Active criteria count ({len(active_criteria)}) exceeds "
            f"the maximum allowed limit of {MAX_CRITERIA_LIMIT}."
        )

    # 3.5 Sanitize static prompt sections first
    sanitized_prompt_text, stats = sanitize_static_prompt_sections(prompt_text)
    sanitized_changed = (stats["removed_count"] > 0) or (sanitized_prompt_text != prompt_text)

    # 4. Run the unifier sync
    new_prompt_text, list_changed = sync_prompt_text_with_criteria_list(
        prompt_text=sanitized_prompt_text,
        active_criteria=active_criteria,
        typologies=typologies
    )
    changed = sanitized_changed or list_changed

    # 5. Check duplicate keys in new_prompt_text
    import re
    header_count = len(re.findall(r"^\s*[-*]\s*\[[^\]]+\]", new_prompt_text, re.MULTILINE))
    
    final_header_occurrences = {}
    for c in active_criteria:
        canonical_key = c.criterion_key or c.output_key or f"id:{c.criterion_id}"
        final_header_occurrences[canonical_key] = 0
        for line in new_prompt_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- [") or stripped.startswith("* ["):
                matched = False
                if c.output_key and re.search(rf"output_key:\s*{re.escape(c.output_key)}\b", line, re.IGNORECASE):
                    matched = True
                elif c.feed_key and re.search(rf"feed_key:\s*{re.escape(c.feed_key)}\b", line, re.IGNORECASE):
                    matched = True
                
                if matched:
                    final_header_occurrences[canonical_key] += 1

    duplicate_keys = {k: v for k, v in final_header_occurrences.items() if v > 1}

    logger.info(
        "Prompt build:\n"
        "- raw criteria rows: %d\n"
        "- unique criteria: %d\n"
        "- final prompt chars: %d\n"
        "- duplicate keys detected:\n%s",
        header_count,
        len(active_criteria),
        len(new_prompt_text),
        "\n".join(f"  {k}: {v}" for k, v in duplicate_keys.items()) if duplicate_keys else "  None"
    )

    if duplicate_keys:
        dup_details = ", ".join(f"{k}: {v}" for k, v in duplicate_keys.items())
        raise PromptValidationError(
            f"Prompt build failed: Duplicate criteria keys detected in finalized prompt: {dup_details}."
        )

    # 6. Length check (120,000)
    MAX_CHARACTERS_LIMIT = 120000
    if len(new_prompt_text) > MAX_CHARACTERS_LIMIT:
        # Determine the largest criteria
        crit_sizes = []
        for c in active_criteria:
            desc_len = len(c.criterion_description or "")
            crit_sizes.append((c.criterion_name or c.output_key, desc_len))
        crit_sizes.sort(key=lambda x: x[1], reverse=True)
        largest_str = ", ".join(f"'{name}' ({size} chars)" for name, size in crit_sizes[:5])
        
        raise PromptValidationError(
            f"Prompt build failed: Prompt length ({len(new_prompt_text)} characters) exceeds "
            f"the maximum allowed defensive limit of {MAX_CHARACTERS_LIMIT} characters. "
            f"Largest criteria: {largest_str}. "
            f"Please compact active criteria descriptions or deactivate redundant criteria."
        )

    return new_prompt_text, changed



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
        if prompt_text:
            prompt_text = await sanitize_prompt_text_for_preview(db, p.prompt_id, prompt_text)
        elif p.prompt_id:
            prompt_text = await build_fallback_prompt_from_criteria(db, p.prompt_id)

        # Check for active draft in bm_prompt_drafts
        from app.models.drafts import PromptDraft
        draft_stmt = select(PromptDraft).where(
            PromptDraft.prompt_id == p.prompt_id,
            PromptDraft.status.in_(["draft", "pending", "active"])
        ).order_by(PromptDraft.updated_at.desc()).limit(1)
        draft_res = await db.execute(draft_stmt)
        active_draft = draft_res.scalars().first()

        # Clean up stale/obsolete active draft if any
        if active_draft and current:
            active_draft = await _check_and_cleanup_draft(db, active_draft, current)

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
            "owner_user_id": p.owner_user_id,

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
    
    # Saneamiento automático / Sanity check antes de servir
    if current and prompt_text:
        try:
            prompt_text, changed = await sync_prompt_text_with_active_criteria(db, p.prompt_id, prompt_text)
            
            # Si hubo cambios, persistir en base de datos
            if changed or current.prompt != prompt_text:
                from sqlalchemy import func
                from datetime import timezone, datetime
                current.prompt = prompt_text
                current.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                db.add(current)
                await db.commit()
                await db.refresh(current)
                logger.info(f"Saneamiento automático: Se saneó y persistió la versión activa ID {current.id} para prompt_id {p.prompt_id}.")
        except Exception as ex:
            logger.error(f"Error durante el saneamiento automático de versión activa: {ex}", exc_info=True)

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
        "owner_user_id": p.owner_user_id,

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
        if prompt_text and v.prompt_id:
            prompt_text = await sanitize_prompt_text_for_preview(db, v.prompt_id, prompt_text)
        elif not prompt_text and v.prompt_id:
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
        prompt=clean_whitespaces(body.prompt),
        version_label=version_label,
        version_name=version_name,
        updated_by=body.updated_by,
        updated_by_email=body.updated_by_email,
        change_note=body.change_note,
        source=body.source or "manual",
        is_current=True,
    )
    # Discard (delete) all active drafts for this prompt
    from sqlalchemy import delete
    from app.models.drafts import PromptDraft
    await db.execute(
        delete(PromptDraft)
        .where(PromptDraft.prompt_id == body.prompt_id, PromptDraft.status == "draft")
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
        prompt=clean_whitespaces(prompt_text),
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

    # Discard (delete) all active drafts for this prompt
    from sqlalchemy import delete
    from app.models.drafts import PromptDraft
    await db.execute(
        delete(PromptDraft)
        .where(PromptDraft.prompt_id == prompt_id, PromptDraft.status == "draft")
    )

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
    owner_user_id: int | None = None,
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
        owner_user_id=owner_user_id,
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
        owner_user_id=body.owner_user_id,
    )
    db.add(new_struct)
    await db.commit()
    await db.refresh(new_struct)
    return new_struct


async def update_base_structure(
    db: AsyncSession, structure_id: int, body: PromptBaseStructureUpdate
) -> PromptBaseStructure | None:
    """Update an existing prompt base structure."""
    try:
        result = await db.execute(
            select(PromptBaseStructure).where(PromptBaseStructure.id == structure_id)
        )
        struct = result.scalars().first()
        if not struct:
            return None

        # Support both 'name' and 'structure_name' fields from payload
        name_to_use = body.name if body.name is not None else body.structure_name
        if name_to_use is not None:
            struct.structure_name = name_to_use
        if body.description is not None:
            struct.description = body.description
        if body.prompt_type is not None:
            struct.prompt_type = body.prompt_type
        if body.base_prompt is not None:
            struct.base_prompt = body.base_prompt
        
        # Correctly persist default criteria list instead of wiping it
        if body.default_criteria is not None:
            struct.default_criteria = body.default_criteria
        
        if body.is_active is not None:
            struct.is_active = body.is_active
        if body.service_id is not None:
            struct.service_id = body.service_id

        await db.commit()
        await db.refresh(struct)
        logger.info(
            "Successfully updated base structure ID %d: name=%s, description=%s, type=%s, criteria_count=%d",
            struct.id, struct.structure_name, struct.description, struct.prompt_type,
            len(struct.default_criteria) if struct.default_criteria else 0
        )
        return struct
    except Exception as e:
        await db.rollback()
        logger.error("Error updating base structure ID %d: %s", structure_id, e, exc_info=True)
        raise e


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
        owner_user_id=body.owner_user_id,
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
    
    # 4. Copy default criteria if requested and present
    criteria_count = 0
    if body.copy_default_criteria and struct.default_criteria:
        from app.models.criteria import PromptCriterion
        for idx, item in enumerate(struct.default_criteria):
            new_crit = PromptCriterion(
                prompt_id=new_prompt.prompt_id,
                criterion_key=item.get("criterion_key"),
                criterion_name=item.get("criterion_name"),
                criterion_description=item.get("criterion_description"),
                criterion_type=item.get("criterion_type", "score_1_10"),
                output_key=item.get("output_key"),
                feed_key=item.get("feed_key"),
                allowed_values=item.get("allowed_values"),
                applies_to_types=item.get("applies_to_types"),
                order_index=item.get("order_index", idx + 1),
                is_required=item.get("is_required", False),
                is_active=item.get("is_active", True),
            )
            db.add(new_crit)
            criteria_count += 1
        await db.flush()

    # Capture attribute values before commit to avoid lazy load issues on expired attributes
    prompt_id = new_prompt.prompt_id
    version_id = new_version.id
    prompt_name = new_prompt.prompt_name
    prompt_type = new_prompt.prompt_type
    prompt_text = new_version.prompt
    service_id = new_prompt.service_id

    await db.commit()

    return {
        "ok": True,
        "prompt_id": prompt_id,
        "prompt_version_id": version_id,
        "prompt_name": prompt_name,
        "prompt_type": prompt_type,
        "prompt": prompt_text,
        "criteria_count": criteria_count,
        "service_id": service_id,
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


async def _check_and_cleanup_draft(db: AsyncSession, active_draft: Any, current: Any) -> Any:
    """
    Checks if an active draft is obsolete (older than the current published version
    or has no real differences compared to the current version).
    If it is obsolete, deletes it from the DB and returns None.
    Otherwise, returns the active_draft.
    """
    if not active_draft:
        return None
    if not current:
        return active_draft
        
    from app.models.drafts import PromptDraft
    # 1. Check if draft is older than the current version
    if active_draft.updated_at <= current.created_at:
        # Draft is obsolete because a newer version was saved/published
        from sqlalchemy import delete
        await db.execute(delete(PromptDraft).where(PromptDraft.draft_id == active_draft.draft_id))
        await db.commit()
        return None
        
    # 2. Check if the draft has no real changes compared to the current version
    draft_data = active_draft.draft_data or {}
    draft_prompt = draft_data.get("prompt")
    current_prompt = current.prompt
    
    # Compare prompt text
    prompt_changed = (draft_prompt or "").strip() != (current_prompt or "").strip()
    
    # Compare criteria
    from app.models.criteria import PromptCriterion
    crit_stmt = select(PromptCriterion).where(
        PromptCriterion.prompt_id == active_draft.prompt_id,
        PromptCriterion.is_active == True
    )
    res_crit = await db.execute(crit_stmt)
    db_criteria = res_crit.scalars().all()
    draft_criteria = draft_data.get("criteria", []) or []
    
    criteria_changed = not _criteria_are_equal(db_criteria, draft_criteria)
    
    if not prompt_changed and not criteria_changed:
        # Draft has no real differences compared to current published version
        from sqlalchemy import delete
        await db.execute(delete(PromptDraft).where(PromptDraft.draft_id == active_draft.draft_id))
        await db.commit()
        return None
        
    return active_draft


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


async def validate_prompt_sync(db: AsyncSession, prompt_id: int) -> dict:
    """
    Valida que todas las claves esperadas de los criterios activos estén presentes
    en el texto del prompt y en la sección del formato de salida JSON.
    """
    from app.services.criteria_service import get_active_criteria
    import re

    # 1. Fetch active criteria
    active_criteria = await get_active_criteria(db, prompt_id)
    
    # 2. Get current version text
    current_version = await _get_current_version(db, prompt_id)
    prompt_text = current_version.prompt if current_version else ""
    
    # 3. Find format section
    header_pattern = re.compile(
        r"^(?:###?\s+)?(?:FORMATO\s+DE\s+(?:RESPUESTA|SALIDA(?:\s+JSON)?))\b",
        re.IGNORECASE | re.MULTILINE
    )
    matches = list(header_pattern.finditer(prompt_text))
    format_section = prompt_text[matches[-1].start():] if matches else ""

    missing_in_prompt = []
    missing_in_output_format = []

    for c in active_criteria:
        if c.output_key:
            if c.output_key not in prompt_text:
                missing_in_prompt.append(c.output_key)
            if c.output_key not in format_section:
                missing_in_output_format.append(c.output_key)
        if c.feed_key:
            if c.feed_key not in prompt_text:
                missing_in_prompt.append(c.feed_key)
            if c.feed_key not in format_section:
                missing_in_output_format.append(c.feed_key)
                
    ok = (len(missing_in_prompt) == 0 and len(missing_in_output_format) == 0)
    
    return {
        "ok": ok,
        "missing_in_prompt": missing_in_prompt,
        "missing_in_output_format": missing_in_output_format,
        "orphan_keys_removed": []
    }


async def sanitize_prompt_text_for_preview(db: AsyncSession, prompt_id: int, prompt_text: str) -> str:
    """Dynamically resolves and sanitizes a prompt's typology block on read/preview without altering DB state."""
    if not prompt_text:
        return ""
    from app.models.prompts import Prompt, BaseStructureTypology
    from app.models.typologies import Typology
    from sqlalchemy import select
    from app.services.prompt_builder import sanitize_legacy_typologies_block
    
    p_stmt = select(Prompt).where(Prompt.prompt_id == prompt_id)
    p_res = await db.execute(p_stmt)
    p = p_res.scalars().first()
    
    base_structure_id = p.base_structure_id if p else None
    service_id = p.service_id if p else None
    
    typologies = []
    if base_structure_id:
        t_stmt = (
            select(Typology)
            .join(BaseStructureTypology, BaseStructureTypology.typology_id == Typology.typology_id)
            .where(
                BaseStructureTypology.base_structure_id == base_structure_id,
                Typology.is_active == True
            )
            .order_by(Typology.sort_order.asc(), Typology.typology_id.asc())
        )
        t_res = await db.execute(t_stmt)
        typologies = t_res.scalars().all()
        
    if not typologies and service_id:
        t_stmt = select(Typology).where(
            Typology.service_id == service_id,
            Typology.is_active == True
        ).order_by(Typology.sort_order.asc(), Typology.typology_id.asc())
        t_res = await db.execute(t_stmt)
        typologies = t_res.scalars().all()
        
    return sanitize_legacy_typologies_block(prompt_text, typologies)

