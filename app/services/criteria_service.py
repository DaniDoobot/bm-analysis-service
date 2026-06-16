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


class CriterionSyncError(Exception):
    def __init__(self, val_result: dict):
        self.val_result = val_result
        super().__init__("Criterion synchronization validation failed")


def _find_criterion_header_idx(lines: list[str], criterion, search_keys: list[str]) -> int:
    """
    Finds the index of the header line for the given criterion in a list of prompt lines,
    avoiding collisions with similar keys (e.g. matching 'propension' vs 'prueba_propension').
    """
    import re
    target_id = getattr(criterion, "criterion_id", None)
    target_name = getattr(criterion, "criterion_name", None)
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        # A valid header must start with a list marker and brackets or contain output_key metadata
        if not (stripped.startswith("- [") or stripped.startswith("* [") or stripped.startswith("-  [") or stripped.startswith("*  [") or "output_key:" in stripped):
            continue
            
        # Parse the header metadata
        match_ok = re.search(r"output_key:\s*([a-zA-Z0-9_]+)", line, re.IGNORECASE)
        match_fk = re.search(r"feed_key:\s*([a-zA-Z0-9_]+)", line, re.IGNORECASE)
        match_id = re.search(r"\[ID:\s*(\d+)\]", line, re.IGNORECASE)
        
        header_ok = match_ok.group(1) if match_ok else None
        header_fk = match_fk.group(1) if match_fk else None
        header_id = int(match_id.group(1)) if match_id else None
        
        # Extract display name: e.g. "- [type] Name (metadata)"
        type_match = re.match(r"^[-*]\s*\[([^\]]+)\]", stripped)
        header_name = None
        if type_match:
            remaining = stripped[type_match.end():].strip()
            # remove [ID: \d+]
            remaining = re.sub(r"^\[ID:\s*\d+\]", "", remaining).strip()
            if "(" in remaining:
                header_name = remaining.split("(", 1)[0].strip()
            else:
                header_name = remaining.strip()
                
        is_match = False
        
        # 1. Match by ID if both have it
        if header_id is not None and target_id is not None and header_id == target_id:
            is_match = True
            
        # 2. Match by output_key matching search_keys
        if not is_match and header_ok and header_ok in search_keys:
            is_match = True
            
        # 3. Match by feed_key matching search_keys
        if not is_match and header_fk and header_fk in search_keys:
            is_match = True
            
        # 4. Match by display name exactly (case insensitive) as a fallback
        if not is_match and header_name and target_name:
            if header_name.lower().strip() == target_name.lower().strip():
                is_match = True
                
        # Guard against key collisions:
        # If the header has an output_key that is NOT in search_keys, it belongs to a different criterion.
        if is_match and header_ok and header_ok not in search_keys:
            is_match = False
            
        if is_match:
            return i
            
    return -1


def sync_criterion_block(prompt_text: str, criterion, old_output_key: str | None = None, old_criterion_key: str | None = None) -> tuple[str, bool]:
    """
    Sincroniza el bloque completo de un criterio en el texto del prompt usando claves técnicas.
    Reconstruye dinámicamente la cabecera, descripción e incluye tipologías aplicables.
    """
    if not prompt_text:
        return prompt_text, False

    import re
    output_key = getattr(criterion, "output_key", None)
    criterion_key = getattr(criterion, "criterion_key", None)
    feed_key = getattr(criterion, "feed_key", None)
    
    # Lista de claves técnicas para buscar el bloque
    search_keys = []
    if old_output_key:
        search_keys.append(old_output_key)
    if output_key:
        search_keys.append(output_key)
    if old_criterion_key:
        search_keys.append(old_criterion_key)
    if criterion_key:
        search_keys.append(criterion_key)
    if feed_key:
        search_keys.append(feed_key)
        
    # Deduplicar claves de búsqueda
    seen = set()
    search_keys = [x for x in search_keys if x and not (x in seen or seen.add(x))]
    
    if not search_keys:
        return prompt_text, False

    lines = prompt_text.splitlines()
    header_idx = _find_criterion_header_idx(lines, criterion, search_keys)

    if header_idx == -1:
        # Fallback: Si no se encuentra el bloque del criterio, lo insertamos en la sección "CRITERIOS DE ANÁLISIS"
        section_idx = -1
        for i, line in enumerate(lines):
            if "criterios de análisis" in line.lower() or "criterios de analisis" in line.lower():
                section_idx = i
                break
                
        # Construir bloque nuevo
        new_block = [
            f"- [{criterion.criterion_type or 'text'}] {criterion.criterion_name} (output_key: {criterion.output_key}" + (f", feed_key: {criterion.feed_key}" if criterion.feed_key else "") + ")",
            f"  {criterion.criterion_description or ''}"
        ]
        if getattr(criterion, "applies_to_types", None):
            typos_str = ", ".join(criterion.applies_to_types) if isinstance(criterion.applies_to_types, list) else str(criterion.applies_to_types)
            new_block.append(f"  Tipologías aplicables: {typos_str}")
        if getattr(criterion, "allowed_values", None):
            vals_str = ", ".join(criterion.allowed_values) if isinstance(criterion.allowed_values, list) else str(criterion.allowed_values)
            new_block.append(f"  Valores permitidos: {vals_str}")
            
        if section_idx != -1:
            lines.insert(section_idx + 1, "")
            for offset, new_line in enumerate(new_block):
                lines.insert(section_idx + 2 + offset, new_line)
            logger.info(f"Sincronización: Insertado nuevo bloque para el criterio {search_keys[0]} bajo la sección de criterios.")
            return "\n".join(lines), True
        else:
            lines.append("")
            lines.extend(new_block)
            logger.info(f"Sincronización: Añadido nuevo bloque para el criterio {search_keys[0]} al final del prompt.")
            return "\n".join(lines), True

    # Encontramos la cabecera. Ahora buscamos el final del bloque
    block_end_idx = len(lines)
    for j in range(header_idx + 1, len(lines)):
        line = lines[j]
        # Límites del bloque:
        # 1. Empieza otro encabezado markdown (#)
        if line.strip().startswith("#"):
            block_end_idx = j
            break
        # 2. Empieza una nueva viñeta que indica otro criterio
        if line.strip().startswith("- [") or line.strip().startswith("* ["):
            block_end_idx = j
            break
        # 3. Contiene otra clave técnica distinta a la de este criterio
        if "output_key:" in line:
            other_key_match = re.search(r"output_key:\s*([a-zA-Z0-9_]+)", line, re.IGNORECASE)
            if other_key_match:
                other_key = other_key_match.group(1)
                if other_key != output_key:
                    block_end_idx = j
                    break
        # 4. Palabras clave de fin de sección
        if re.match(r"^\s*###?\s+(?:FORMATO DE SALIDA|REGLAS|DEFINICIÓN|TAREA|CONTEXTO)", line, re.IGNORECASE):
            block_end_idx = j
            break

    block_lines = lines[header_idx:block_end_idx]
    header_line = block_lines[0]
    technical_lines = []
    original_description_lines = []
    
    # Prefijos estrictos para identificar metadatos técnicos del bloque
    tech_prefixes = (
        "tipologías aplicables:",
        "tipologias aplicables:",
        "valores permitidos:",
        "valores:",
        "output_key:",
        "feed_key:",
        "applies_to_types:",
        "allowed_values:"
    )
    
    for line in block_lines[1:]:
        stripped_line = line.strip().lower()
        is_tech = stripped_line.startswith(tech_prefixes)
        if is_tech:
            technical_lines.append(line)
        else:
            if line.strip():
                original_description_lines.append(line)
            else:
                technical_lines.append(line)

    # Indentación original
    indent = "  "
    if original_description_lines:
        first_desc_line = original_description_lines[0]
        match_indent = re.match(r"^(\s+)", first_desc_line)
        if match_indent:
            indent = match_indent.group(1)
    else:
        match_indent_header = re.match(r"^(\s*)", header_line)
        if match_indent_header:
            indent = match_indent_header.group(1) + "  "

    # Reconstruir cabecera dinámicamente
    header_bullet = "- "
    match_bullet = re.match(r"^(\s*[-*])", header_line)
    if match_bullet:
        header_bullet = match_bullet.group(1) + " "
        
    id_prefix = ""
    match_id = re.search(r"\[ID:\s*\d+\]", header_line)
    if match_id:
        id_prefix = match_id.group(0) + " "
        
    new_header_line = f"{header_bullet.rstrip()} {id_prefix}[{criterion.criterion_type or 'text'}] {criterion.criterion_name}"
    keys_meta = []
    if criterion.output_key:
        keys_meta.append(f"output_key: {criterion.output_key}")
    if getattr(criterion, "feed_key", None):
        keys_meta.append(f"feed_key: {criterion.feed_key}")
    if keys_meta:
        new_header_line += " (" + ", ".join(keys_meta) + ")"

    # Construir nuevas líneas de descripción con la indentación correcta
    new_desc = getattr(criterion, "criterion_description", "") or ""
    new_desc_lines = []
    for line in new_desc.splitlines():
        if line.strip():
            new_desc_lines.append(indent + line.strip())
        else:
            new_desc_lines.append("")

    # Filtrar metadatos técnicos antiguos redundantes que vamos a reconstruir
    preserved_tech_lines = []
    for line in technical_lines:
        normalized_line = line.lower().strip()
        if normalized_line.startswith("valores permitidos:") or normalized_line.startswith("tipologías aplicables:") or normalized_line.startswith("tipologias aplicables:"):
            continue
        preserved_tech_lines.append(line)

    # Reconstruir tipologías aplicables y valores permitidos
    new_tech_lines = []
    if getattr(criterion, "allowed_values", None):
        vals = criterion.allowed_values
        vals_str = ", ".join(vals) if isinstance(vals, list) else str(vals)
        new_tech_lines.append(f"{indent}Valores permitidos: {vals_str}")
        
    if getattr(criterion, "applies_to_types", None):
        typos = criterion.applies_to_types
        typos_str = ", ".join(typos) if isinstance(typos, list) else str(typos)
        new_tech_lines.append(f"{indent}Tipologías aplicables: {typos_str}")
    else:
        # Reconstruir "Todas" si el bloque original contenía la línea de tipologías
        has_orig_typos = any("tipología" in line.lower() or "tipologia" in line.lower() for line in block_lines[1:])
        if has_orig_typos:
            new_tech_lines.append(f"{indent}Tipologías aplicables: Todas")

    # Reconstruir bloque y sustituir en el texto original
    new_block_lines = [new_header_line]
    new_block_lines.extend(new_desc_lines)
    new_block_lines.extend(new_tech_lines)
    new_block_lines.extend(preserved_tech_lines)
    
    lines[header_idx:block_end_idx] = new_block_lines
    return "\n".join(lines), True


def remove_criterion_block(prompt_text: str, criterion) -> tuple[str, bool]:
    """
    Elimina el bloque de un criterio completo y sus metadatos del texto del prompt,
    e invalida sus llaves técnicas del formato JSON de salida final.
    """
    if not prompt_text:
        return prompt_text, False

    import re
    output_key = getattr(criterion, "output_key", None)
    criterion_key = getattr(criterion, "criterion_key", None)
    feed_key = getattr(criterion, "feed_key", None)
    
    # Lista de claves técnicas para buscar el bloque
    search_keys = [output_key, criterion_key, feed_key]
    search_keys = [x for x in search_keys if x]
    
    if not search_keys:
        return prompt_text, False

    lines = prompt_text.splitlines()
    header_idx = _find_criterion_header_idx(lines, criterion, search_keys)

    if header_idx == -1:
        return prompt_text, False

    # Encontrar final del bloque
    block_end_idx = len(lines)
    for j in range(header_idx + 1, len(lines)):
        line = lines[j]
        if line.strip().startswith("#"):
            block_end_idx = j
            break
        if line.strip().startswith("- [") or line.strip().startswith("* ["):
            block_end_idx = j
            break
        if "output_key:" in line:
            other_key_match = re.search(r"output_key:\s*([a-zA-Z0-9_]+)", line, re.IGNORECASE)
            if other_key_match:
                other_key = other_key_match.group(1)
                if other_key != output_key:
                    block_end_idx = j
                    break
        if re.match(r"^\s*###?\s+(?:FORMATO DE SALIDA|REGLAS|DEFINICIÓN|TAREA|CONTEXTO)", line, re.IGNORECASE):
            block_end_idx = j
            break

    # Eliminar bloque de líneas
    # Remover también una línea en blanco anterior si está libre para evitar huecos en el prompt
    if header_idx > 0 and lines[header_idx - 1].strip() == "":
        header_idx -= 1
        
    del lines[header_idx:block_end_idx]
    new_prompt_text = "\n".join(lines)
    
    # Limpiar las llaves del JSON final para evitar que el LLM las retorne
    for key in [output_key, feed_key]:
        if not key:
            continue
        pattern_json = re.compile(rf'^\s*[\'"]?{re.escape(key)}[\'"]?\s*:\s*[^,\n]+,?\s*$', re.MULTILINE | re.IGNORECASE)
        new_prompt_text = pattern_json.sub("", new_prompt_text)

    return new_prompt_text, True


def clean_orphaned_blocks(prompt_text: str, active_criteria: list) -> str:
    """
    Inspecciona el prompt completo buscando bloques huérfanos (que ya no pertenecen a criterios activos)
    y los elimina automáticamente.
    """
    if not prompt_text:
        return prompt_text

    import re
    # Obtener todas las output_keys y criterion_keys activas
    active_keys = set()
    for c in active_criteria:
        if c.output_key:
            active_keys.add(c.output_key)
        if c.criterion_key:
            active_keys.add(c.criterion_key)
            
    # Conservar siempre "tipo_llamada"
    active_keys.add("tipo_llamada")

    lines = prompt_text.splitlines()
    orphaned_keys = []
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- [") or stripped.startswith("* ["):
            match = re.search(r"output_key:\s*([a-zA-Z0-9_]+)", line, re.IGNORECASE)
            if match:
                key = match.group(1)
                if key not in active_keys:
                    orphaned_keys.append(key)

    # Eliminar todos los bloques huérfanos encontrados
    for o_key in orphaned_keys:
        class MockCriterion:
            def __init__(self, key):
                self.output_key = key
                self.criterion_key = key
                self.feed_key = f"{key}_feed"
                
        mock_c = MockCriterion(o_key)
        prompt_text, removed = remove_criterion_block(prompt_text, mock_c)
        if removed:
            logger.info(f"Limpieza automática: Bloque huérfano '{o_key}' eliminado del prompt completo.")
            
    return prompt_text


async def _sync_prompt_on_removal(db: AsyncSession, prompt_id: int, criterion) -> None:
    """
    Sincroniza la eliminación o deactivación de un criterio en la versión actual del prompt
    y en los borradores activos.
    """
    await db.flush()
    try:
        from app.services.prompts_service import _get_current_version, sync_output_format_in_prompt, clean_whitespaces
        from app.models.prompts import Prompt
        from app.models.drafts import PromptDraft
        from app.models.typologies import Typology
        from app.models.services import Service
        from sqlalchemy import func
        from datetime import timezone, datetime
        
        # Resolve active typologies of the service
        prompt_obj = await db.get(Prompt, prompt_id)
        service_id = prompt_obj.service_id if prompt_obj else None
        if not service_id:
            s_res = await db.execute(select(Service.service_id).where(Service.service_key == "front"))
            service_id = s_res.scalar()
            
        typologies = []
        if service_id:
            t_res = await db.execute(
                select(Typology)
                .where(Typology.service_id == service_id, Typology.is_active == True)
                .order_by(Typology.sort_order.asc())
            )
            typologies = t_res.scalars().all()

        # Fetch remaining active criteria (excluding this deleted/deactivated one)
        active_criteria_stmt = select(PromptCriterion).where(
            PromptCriterion.prompt_id == prompt_id,
            PromptCriterion.is_active == True,
            PromptCriterion.criterion_id != criterion.criterion_id,
            PromptCriterion.deleted_at.is_(None)
        )
        active_res = await db.execute(active_criteria_stmt)
        remaining_criteria = list(active_res.scalars().all())
        remaining_criteria = _deduplicate_criteria_list(remaining_criteria)

        # 1. Sincronizar en PromptVersion
        current_version = await _get_current_version(db, prompt_id)
        if current_version and current_version.prompt:
            prompt_text = current_version.prompt
            new_text, removed = remove_criterion_block(prompt_text, criterion)
            
            # Rebuild output format section without this criterion
            new_text, format_changed = sync_output_format_in_prompt(new_text, remaining_criteria, typologies)
            
            new_text = clean_whitespaces(new_text)
            
            if (removed or format_changed) and new_text != current_version.prompt:
                current_version.prompt = new_text
                current_version.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                db.add(current_version)
                
                # Actualizar también parent Prompt
                if prompt_obj:
                    prompt_obj.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                    db.add(prompt_obj)
                logger.info(f"Sincronización de eliminación: Se eliminó el bloque del criterio '{criterion.output_key}' y se reconstruyó el formato JSON en la versión activa del prompt.")

        # 2. Sincronizar en borradores activos (PromptDraft)
        drafts_stmt = select(PromptDraft).where(
            PromptDraft.prompt_id == prompt_id,
            PromptDraft.status == "draft"
        )
        drafts_res = await db.execute(drafts_stmt)
        active_drafts = drafts_res.scalars().all()
        for draft in active_drafts:
            draft_data = draft.draft_data or {}
            draft_changed = False
            
            # A. Eliminar bloque del prompt de borrador
            if "prompt" in draft_data and isinstance(draft_data["prompt"], str):
                draft_prompt = draft_data["prompt"]
                new_draft_prompt, removed = remove_criterion_block(draft_prompt, criterion)
                
                # Rebuild output format section
                new_draft_prompt, format_changed = sync_output_format_in_prompt(new_draft_prompt, remaining_criteria, typologies)
                
                new_draft_prompt = clean_whitespaces(new_draft_prompt)
                if removed or format_changed:
                    draft_data["prompt"] = new_draft_prompt
                    draft_changed = True
                    
            # B. Eliminar en la lista de criterios estructurados del borrador
            if "criteria" in draft_data and isinstance(draft_data["criteria"], list):
                original_len = len(draft_data["criteria"])
                draft_data["criteria"] = [
                    crit for crit in draft_data["criteria"] 
                    if not (
                        (crit.get("criterion_id") is not None and crit.get("criterion_id") == criterion.criterion_id) or 
                        (crit.get("criterion_key") is not None and crit.get("criterion_key") == criterion.criterion_key) or
                        (crit.get("output_key") is not None and crit.get("output_key") == criterion.output_key)
                    )
                ]
                if len(draft_data["criteria"]) != original_len:
                    draft_changed = True
                    
            if draft_changed:
                draft.draft_data = dict(draft_data)
                draft.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                db.add(draft)
                logger.info(f"Sincronización de eliminación en borrador: Se actualizó el borrador ID {draft.draft_id} tras eliminar/deactivar el criterio.")
                
    except Exception as ex:
        logger.error(f"Error al sincronizar eliminación del criterio en prompt/borradores: {ex}", exc_info=True)




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


def _deduplicate_criteria_list(criteria: list[PromptCriterion]) -> list[PromptCriterion]:
    """
    Deduplicates a list of criteria keeping order and first occurrence based on:
    1. criterion_key
    2. output_key
    3. f"id:{criterion_id}"
    """
    seen = set()
    unique_criteria = []
    for c in criteria:
        canonical_key = (
            c.criterion_key
            or c.output_key
            or (f"id:{c.criterion_id}" if hasattr(c, "criterion_id") else None)
        )
        if not canonical_key:
            unique_criteria.append(c)
            continue
        if canonical_key in seen:
            logger.info(f"Deduplicating criteria list: Removing duplicate for canonical key '{canonical_key}'")
            continue
        seen.add(canonical_key)
        unique_criteria.append(c)
    return unique_criteria


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
    all_criteria = list(criteria_result.scalars().all())
    all_criteria = _deduplicate_criteria_list(all_criteria)

    # Group by type
    grouped: dict[str, list] = {t: [] for t in CRITERION_TYPES}
    for c in all_criteria:
        key = c.criterion_type or "text"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(c)

    return CriteriaGroupedOut(
        prompt=prompt_text,
        criteria=all_criteria,
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
    criteria = list(result.scalars().all())
    return _deduplicate_criteria_list(criteria)


async def _sync_criterion_on_save(
    db: AsyncSession,
    criterion: PromptCriterion,
    old_name: str | None = None,
    old_output_key: str | None = None
) -> None:
    """
    Sincroniza un criterio guardado, creado o restaurado en la versión activa del prompt
    y borradores activos. Sincroniza descripciones, deduplica y actualiza el formato JSON de salida.
    """
    await db.flush()
    try:
        from app.services.prompts_service import sync_prompt_text_with_active_criteria, sync_output_format_in_prompt, clean_whitespaces
        from app.models.prompts import Prompt
        from app.models.drafts import PromptDraft
        from app.models.typologies import Typology
        from app.models.services import Service
        from sqlalchemy import func
        from datetime import timezone, datetime
        
        prompt_id = criterion.prompt_id
        if not prompt_id:
            return

        if not criterion.is_active:
            # Si se guarda como inactivo, remover de la versión activa y borradores
            await _sync_prompt_on_removal(db, prompt_id, criterion)
            return

        # Cargar criterios activos una sola vez para la limpieza de huérfanos
        active_criteria_stmt = select(PromptCriterion).where(
            PromptCriterion.prompt_id == prompt_id,
            PromptCriterion.is_active == True,
            PromptCriterion.deleted_at.is_(None)
        )
        active_criteria_res = await db.execute(active_criteria_stmt)
        active_criteria = list(active_criteria_res.scalars().all())
        active_criteria = _deduplicate_criteria_list(active_criteria)
        
        # Asegurar que el criterio actual esté en active_criteria para que no se considere huérfano
        if not any(c.criterion_id == criterion.criterion_id or c.criterion_key == criterion.criterion_key for c in active_criteria):
            active_criteria.append(criterion)

        prompt_obj = await db.get(Prompt, prompt_id)
        service_id = prompt_obj.service_id if prompt_obj else None
        if not service_id:
            s_res = await db.execute(select(Service.service_id).where(Service.service_key == "front"))
            service_id = s_res.scalar()

        # Fetch active typologies
        typologies = []
        if service_id:
            t_res = await db.execute(
                select(Typology)
                .where(Typology.service_id == service_id, Typology.is_active == True)
                .order_by(Typology.sort_order.asc())
            )
            typologies = t_res.scalars().all()

        # --- 1. Sincronizar de la versión activa (PromptVersion) ---
        from app.services.prompts_service import _get_current_version
        current_version = await _get_current_version(db, prompt_id)
        if current_version and current_version.prompt:
            prompt_text = current_version.prompt
            changed = False
            
            # Primero, aplicar reemplazos globales de nombre y claves
            if old_name and criterion.criterion_name and old_name != criterion.criterion_name:
                if old_name in prompt_text:
                    prompt_text = prompt_text.replace(old_name, criterion.criterion_name)
                    changed = True
            
            if old_output_key and criterion.output_key and old_output_key != criterion.output_key:
                if old_output_key in prompt_text:
                    prompt_text = prompt_text.replace(old_output_key, criterion.output_key)
                    changed = True
            
            # Luego, sincronizar usando la lógica centralizada
            new_text, sync_changed = await sync_prompt_text_with_active_criteria(db, prompt_id, prompt_text)
            if sync_changed:
                changed = True
                prompt_text = new_text
            
            if changed or prompt_text != current_version.prompt:
                current_version.prompt = prompt_text
                current_version.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                if prompt_obj:
                    prompt_obj.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                    db.add(prompt_obj)
                db.add(current_version)
                logger.info(f"Sincronización automática: Se actualizó el prompt activo para el criterio ID {criterion.criterion_id}.")

        # --- 2. Sincronizar de borradores activos (PromptDraft) ---
        drafts_stmt = select(PromptDraft).where(
            PromptDraft.prompt_id == prompt_id,
            PromptDraft.status == "draft"
        )
        drafts_res = await db.execute(drafts_stmt)
        active_drafts = drafts_res.scalars().all()
        for draft in active_drafts:
            draft_data = draft.draft_data or {}
            draft_changed = False
            
            # A. Actualizar texto de prompt en el borrador
            if "prompt" in draft_data and isinstance(draft_data["prompt"], str):
                draft_prompt = draft_data["prompt"]
                
                if old_name and criterion.criterion_name and old_name != criterion.criterion_name:
                    if old_name in draft_prompt:
                        draft_prompt = draft_prompt.replace(old_name, criterion.criterion_name)
                        draft_changed = True
                        
                if old_output_key and criterion.output_key and old_output_key != criterion.output_key:
                    if old_output_key in draft_prompt:
                        draft_prompt = draft_prompt.replace(old_output_key, criterion.output_key)
                        draft_changed = True
                        
                # Sincronizar el bloque descriptivo del criterio
                for c in active_criteria:
                    draft_prompt, desc_changed = sync_criterion_block(draft_prompt, c)
                    if desc_changed:
                        draft_changed = True
                
                draft_prompt = clean_orphaned_blocks(draft_prompt, active_criteria)
                
                # Sincronizar el JSON de formato de salida del borrador
                draft_prompt, format_changed = sync_output_format_in_prompt(draft_prompt, active_criteria, typologies)
                if format_changed:
                    draft_changed = True
                
                draft_prompt = clean_whitespaces(draft_prompt)
                draft_data["prompt"] = draft_prompt
                draft_changed = True
            
            # B. Actualizar elemento correspondiente en la lista de criterios estructurados del borrador
            if "criteria" in draft_data and isinstance(draft_data["criteria"], list):
                exists = False
                for crit_dict in draft_data["criteria"]:
                    if not isinstance(crit_dict, dict):
                        continue
                    c_id = crit_dict.get("criterion_id")
                    c_key = crit_dict.get("criterion_key")
                    
                    match_by_id = (c_id is not None and c_id == criterion.criterion_id)
                    match_by_key = (c_key is not None and c_key == criterion.criterion_key)
                    
                    if match_by_id or match_by_key:
                        crit_dict["criterion_description"] = criterion.criterion_description
                        crit_dict["criterion_name"] = criterion.criterion_name
                        crit_dict["criterion_key"] = criterion.criterion_key
                        crit_dict["output_key"] = criterion.output_key
                        crit_dict["feed_key"] = criterion.feed_key
                        crit_dict["criterion_type"] = criterion.criterion_type
                        crit_dict["allowed_values"] = criterion.allowed_values
                        crit_dict["applies_to_types"] = criterion.applies_to_types
                        crit_dict["order_index"] = criterion.order_index
                        crit_dict["is_required"] = criterion.is_required
                        crit_dict["is_active"] = criterion.is_active
                        exists = True
                        draft_changed = True
                if not exists:
                    # Agregar nuevo
                    new_crit_dict = {
                        "criterion_id": criterion.criterion_id,
                        "criterion_name": criterion.criterion_name,
                        "criterion_description": criterion.criterion_description,
                        "criterion_key": criterion.criterion_key,
                        "output_key": criterion.output_key,
                        "feed_key": criterion.feed_key,
                        "criterion_type": criterion.criterion_type,
                        "allowed_values": criterion.allowed_values,
                        "applies_to_types": criterion.applies_to_types,
                        "order_index": criterion.order_index,
                        "is_required": criterion.is_required,
                        "is_active": criterion.is_active
                    }
                    draft_data["criteria"].append(new_crit_dict)
                    draft_changed = True
            
            if draft_changed:
                draft.draft_data = dict(draft_data)
                draft.updated_at = func.now() if hasattr(func, 'now') else datetime.now(timezone.utc)
                db.add(draft)
                logger.info(f"Sincronización automática de borrador: Se actualizó el borrador ID {draft.draft_id} con el criterio modificado/creado.")
                
    except Exception as sync_ex:
        logger.error(f"Error durante la sincronización automática de criterio en prompt/borradores: {sync_ex}", exc_info=True)


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
        old_desc = criterion.criterion_description
        old_name = criterion.criterion_name
        old_output_key = criterion.output_key

        for field, value in body.model_dump(exclude={"criterion_id"}).items():
            setattr(criterion, field, value)
        criterion.deleted_at = None
        criterion.deleted_by_email = None

        await _sync_criterion_on_save(db, criterion, old_name=old_name, old_output_key=old_output_key)

        await db.flush()
        from app.services.prompts_service import validate_prompt_sync
        val_res = await validate_prompt_sync(db, criterion.prompt_id)
        if not val_res["ok"]:
            await db.rollback()
            raise CriterionSyncError(val_res)

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
            
            await _sync_criterion_on_save(db, criterion)
            
            await db.flush()
            from app.services.prompts_service import validate_prompt_sync
            val_res = await validate_prompt_sync(db, criterion.prompt_id)
            if not val_res["ok"]:
                await db.rollback()
                raise CriterionSyncError(val_res)

            await db.commit()
            await db.refresh(criterion)
            return criterion
            
    # Create new
    logger.info(f"Creating new criterion (key: '{body.criterion_key}').")
    criterion = PromptCriterion(**body.model_dump(exclude={"criterion_id"}))
    db.add(criterion)
    await db.flush() # Flush to generate criterion_id
    
    await _ensure_typology_associations(db, criterion)
    
    await _sync_criterion_on_save(db, criterion)
    
    await db.flush()
    from app.services.prompts_service import validate_prompt_sync
    val_res = await validate_prompt_sync(db, criterion.prompt_id)
    if not val_res["ok"]:
        await db.rollback()
        raise CriterionSyncError(val_res)

    await db.commit()
    await db.refresh(criterion)
    return criterion


async def toggle_criterion(db: AsyncSession, criterion_id: int, is_active: bool) -> None:
    stmt = select(PromptCriterion).where(PromptCriterion.criterion_id == criterion_id)
    res = await db.execute(stmt)
    criterion = res.scalars().first()
    if not criterion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Criterio con ID {criterion_id} no encontrado."
        )
    criterion.is_active = is_active
    db.add(criterion)
    
    # Sincronización
    await _sync_criterion_on_save(db, criterion)
    
    await db.flush()
    from app.services.prompts_service import validate_prompt_sync
    val_res = await validate_prompt_sync(db, criterion.prompt_id)
    if not val_res["ok"]:
        await db.rollback()
        raise CriterionSyncError(val_res)

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

    # Sync prompt output format when typologies are updated
    if criterion.prompt_id:
        await _sync_criterion_on_save(db, criterion)
        
        await db.flush()
        from app.services.prompts_service import validate_prompt_sync
        val_res = await validate_prompt_sync(db, criterion.prompt_id)
        if not val_res["ok"]:
            await db.rollback()
            raise CriterionSyncError(val_res)

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

    # Sincronizar la eliminación en el texto del prompt y borradores antes de borrar en la BD
    if criterion.prompt_id:
        await _sync_prompt_on_removal(db, criterion.prompt_id, criterion)

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

    # Validate sync after deletion
    if criterion.prompt_id:
        await db.flush()
        from app.services.prompts_service import validate_prompt_sync
        val_res = await validate_prompt_sync(db, criterion.prompt_id)
        if not val_res["ok"]:
            await db.rollback()
            raise CriterionSyncError(val_res)

    await db.commit()

    return {
        "ok": True,
        "criterion_id": criterion_id,
        "action": action,
        "message": "Item eliminado correctamente"
    }


def deduplicate_criteria_blocks(prompt_text: str) -> str:
    """
    Scans the prompt text and removes any duplicate criterion blocks.
    A block is recognized by standard headers: '- [type]' or '* [type]'.
    Keeps only the first occurrence of each canonical block key parsed from header.
    """
    if not prompt_text:
        return prompt_text
    
    import re
    lines = prompt_text.splitlines()
    new_lines = []
    seen_keys = set()
    
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        
        is_header = False
        canonical_key = None
        
        if stripped.startswith("- [") or stripped.startswith("* [") or stripped.startswith("-  [") or stripped.startswith("*  ["):
            # Try to match output_key
            match = re.search(r"output_key:\s*([a-zA-Z0-9_]+)", line, re.IGNORECASE)
            # Try to match feed_key
            feed_match = re.search(r"feed_key:\s*([a-zA-Z0-9_]+)", line, re.IGNORECASE)
            # Try to match ID
            id_match = re.search(r"\[ID:\s*(\d+)\]", line, re.IGNORECASE)
            
            if match:
                canonical_key = match.group(1)
            elif feed_match:
                canonical_key = feed_match.group(1)
            elif id_match:
                canonical_key = f"id:{id_match.group(1)}"
            else:
                # Fallback to display name slug
                name_match = re.search(r"\]\s*([^(#\n]+)", stripped)
                if name_match:
                    name_str = name_match.group(1).strip().lower()
                    canonical_key = re.sub(r"[^a-z0-9_]+", "_", name_str).strip("_")
            
            if canonical_key:
                is_header = True
                
        if is_header and canonical_key:
            if canonical_key in seen_keys:
                logger.info(f"Deduplication: Removing duplicate block for '{canonical_key}' starting at line {i}")
                # Scan forward until the next block boundary
                i += 1
                while i < len(lines):
                    next_line = lines[i]
                    next_stripped = next_line.strip()
                    if next_stripped.startswith("#"):
                        break
                    if next_stripped.startswith("- [") or next_stripped.startswith("* ["):
                        break
                    if "output_key:" in next_line:
                        other_key_match = re.search(r"output_key:\s*([a-zA-Z0-9_]+)", next_line, re.IGNORECASE)
                        if other_key_match:
                            other_key = other_key_match.group(1)
                            if other_key != canonical_key:
                                break
                    if re.match(r"^\s*###?\s+(?:FORMATO DE SALIDA|REGLAS|DEFINICIÓN|TAREA|CONTEXTO)", next_line, re.IGNORECASE):
                        break
                    i += 1
                continue
            else:
                seen_keys.add(canonical_key)
                
        new_lines.append(line)
        i += 1
        
    return "\n".join(new_lines)


async def generate_criterion_description_ai(db: AsyncSession, criterion_id: int | None, body: Any) -> dict:
    """
    Generate a structured, operationally-focused mini-prompt for a single evaluation criterion using AI.
    Produces sections (dimensions, rubric, penalties, null rules, feedback) so the analysis bot
    can interpret and evaluate the criterion correctly.
    Applies strict validations to prevent formatting leaks, global instructions, JSON or duplications.
    """
    import re
    from app.services.openai_service import complete_text

    criterion_name = body.criterion_name
    criterion_type = body.criterion_type
    output_key = body.output_key
    feed_key = body.feed_key
    current_description = body.current_description or ""
    instruction = (body.instruction or "").strip()
    typology_keys = body.typology_keys or []

    # Default instruction when the user leaves it blank
    if not instruction:
        instruction = (
            "Convierte el criterio actual en una instrucción de evaluación clara, estructurada y operativa "
            "para un bot de análisis de llamadas. No lo resumas. Reescríbelo como un mini-prompt de auditoría "
            "con apartados, dimensiones de evaluación, reglas de puntuación, penalizaciones, cuándo devolver null "
            "y cómo justificar el feedback. Mantén el sentido original, pero hazlo más preciso, exigente y fácil "
            "de interpretar por el modelo."
        )

    # ── 1. System prompt ────────────────────────────────────────────────────────
    system_content = (
        "Eres un experto en diseño de criterios de evaluación para auditoría de llamadas de centros médicos.\n"
        "Tu tarea es generar una INSTRUCCIÓN DE EVALUACIÓN OPERATIVA y ESTRUCTURADA para un único criterio.\n"
        "Esta instrucción será usada directamente por un modelo de IA para evaluar llamadas reales.\n\n"
        "REGLAS ABSOLUTAS E IRROMPIBLES:\n"
        "1. NO generes código JSON, bloques de formato JSON, ni instrucciones globales del prompt.\n"
        "2. NO inventes ni modifiques el nombre del criterio, tipo ni claves técnicas.\n"
        "3. NO incluyas las etiquetas técnicas 'output_key:' o 'feed_key:' ni su valor literal en el texto.\n"
        "4. NO menciones otros criterios del prompt ni el prompt en sí.\n"
        "5. NO uses párrafos únicos gigantes. USA SIEMPRE secciones, apartados, listas y saltos de línea.\n"
        "6. Redacta en español profesional con lenguaje de auditoría de calidad.\n"
        "7. Devuelve EXCLUSIVAMENTE el texto del criterio, sin introducciones ni preámbulos "
        "('Aquí tienes...', 'Claro...', 'Por supuesto...').\n"
        "8. Longitud mínima orientativa: 800 caracteres para criterios complejos. "
        "Puedes llegar hasta 3500 caracteres si el contenido es estructurado y útil.\n"
        "9. Integra las instrucciones adicionales del usuario dentro de la estructura, "
        "no las añadas como comentario suelto al final.\n"
    )

    # ── Type-specific structural templates ──────────────────────────────────────
    if criterion_type == "score_1_10":
        system_content += (
            "\nESTRUCTURA OBLIGATORIA para score_1_10 — usa EXACTAMENTE estas secciones:\n\n"
            "  1. Frase de apertura: empieza con 'Actúa como un auditor de calidad [adjetivo]. Evalúa [objetivo].'\n"
            "  2. 'Dimensiones de evaluación:' — lista numerada de 2-4 dimensiones.\n"
            "     Cada dimensión: qué observar, qué penaliza, qué evidencias buscar.\n"
            "  3. 'Reglas de puntuación:' — escala completa con criterio para cada tramo:\n"
            "     - 1-2: Muy deficiente. [descripción]\n"
            "     - 3-4: Deficiente. [descripción]\n"
            "     - 5-6: Aceptable pero mejorable. [descripción]\n"
            "     - 7-8: Bueno. [descripción]\n"
            "     - 9-10: Excelente. [descripción]\n"
            "  4. 'Criterios de penalización:' — lista de condiciones que rebajan la nota.\n"
            "  5. 'Cuándo devolver null:' — una o dos condiciones claras y estrictas.\n"
        )
        if feed_key:
            system_content += (
                "  6. 'Formato de feedback:' — instrucción clara para justificar la puntuación.\n"
                "     Pide ejemplos concretos, citas literales de la llamada, y explicación "
                "de qué faltó si la nota es baja.\n"
            )

    elif criterion_type == "boolean":
        system_content += (
            "\nESTRUCTURA OBLIGATORIA para boolean:\n"
            "  1. Frase de apertura: 'Actúa como un auditor estricto. Verifica si el agente cumple: [objetivo].'\n"
            "  2. 'Regla de evaluación:' — define exactamente cuándo devolver 'Si', 'No' y null.\n"
            "  3. 'Evidencias:' — qué frases, comportamientos o señales buscar.\n"
            "     No se presupone cumplimiento si no aparece explícitamente.\n"
            "  4. 'Cuándo devolver null:' — condición exacta.\n"
        )
        if feed_key:
            system_content += (
                "  5. 'Formato de feedback:' — justificar con frase literal o resumen de evidencia.\n"
            )

    elif criterion_type in ("text", "free_text"):
        system_content += (
            "\nESTRUCTURA OBLIGATORIA para text:\n"
            "  1. Frase de apertura: 'Extrae de la llamada [dato solicitado].'\n"
            "  2. 'Reglas:' — cómo extraer el dato, formato esperado, qué priorizar si hay varias opciones.\n"
            "  3. 'Cuándo devolver null:' — si el dato no aparece en la llamada.\n"
            "  4. 'Formato de respuesta:' — respuesta breve y sin elaboración adicional.\n"
        )
        if feed_key:
            system_content += "  5. 'Formato de feedback:' — qué contexto adicional incluir si aplica.\n"

    elif criterion_type in ("number", "percentage"):
        system_content += (
            "\nESTRUCTURA OBLIGATORIA para number/percentage:\n"
            "  1. Frase de apertura: 'Calcula o estima [métrica].'\n"
            "  2. 'Reglas de cálculo:' — cómo obtener el valor, qué fuentes de la llamada usar.\n"
            "  3. 'Formato del resultado:' — solo el número, sin símbolos adicionales.\n"
            "  4. 'Cuándo devolver null:' — si no puede calcularse de forma fiable.\n"
        )
        if feed_key:
            system_content += "  5. 'Formato de feedback:' — explica la base del cálculo y fuentes usadas.\n"

    else:
        system_content += (
            "\nUsa apartados claros: objetivo, qué evaluar, reglas, cuándo devolver null.\n"
        )
        if feed_key:
            system_content += "Incluye también cómo justificar el feedback.\n"

    # ── 2. User prompt ──────────────────────────────────────────────────────────
    typologies_str = ", ".join(typology_keys) if typology_keys else "Todas las tipologías"
    user_content = (
        f"Datos del Criterio:\n"
        f"- Nombre: {criterion_name}\n"
        f"- Tipo: {criterion_type}\n"
        f"- Tipologías de llamada asociadas: {typologies_str}\n"
        f"- ¿Tiene feedback?: {'Sí' if feed_key else 'No'}\n"
    )
    if current_description:
        user_content += f"- Descripción/instrucción actual:\n{current_description}\n\n"
    else:
        user_content += "- Descripción actual: (ninguna — criterio nuevo)\n\n"
    user_content += (
        f"Instrucción del usuario:\n\"{instruction}\"\n\n"
        "Genera ahora la instrucción de evaluación estructurada. "
        "No añadas introducciones. Empieza directamente con el contenido."
    )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user",   "content": user_content},
    ]

    # ── 3. Call OpenAI ──────────────────────────────────────────────────────────
    try:
        raw_description = await complete_text(
            messages=messages,
            temperature=0.4,
            response_format=None
        )
    except Exception as ex:
        logger.error(f"Error calling OpenAI for criterion AI description: {ex}", exc_info=True)
        raise RuntimeError(f"Error en la llamada de IA: {str(ex)}")

    # ── 4. Normalise whitespace (preserve structure) ────────────────────────────
    cleaned_desc = raw_description.strip()
    cleaned_desc = re.sub(r'\n{3,}', '\n\n', cleaned_desc)   # max 2 blank lines
    cleaned_desc = re.sub(r'[ \t]+\n', '\n', cleaned_desc)   # trailing spaces per line
    cleaned_desc = re.sub(r'\n[ \t]+', '\n', cleaned_desc)   # leading spaces per line
    cleaned_desc = cleaned_desc.strip()

    # ── 5. Defensive sanitisations ──────────────────────────────────────────────
    warnings = []

    # A. JSON block cleanup
    if "{" in cleaned_desc or "}" in cleaned_desc:
        cleaned_desc = re.sub(r'\{[^{}]*\}', '', cleaned_desc).strip()
        warnings.append("Se detectó y removió un posible bloque en formato JSON en el texto generado.")

    # B. output_key / feed_key literal label cleanup
    if "output_key" in cleaned_desc.lower() or "feed_key" in cleaned_desc.lower():
        cleaned_desc = re.sub(r'output_key\s*:\s*[a-zA-Z0-9_]+', '', cleaned_desc, flags=re.IGNORECASE)
        cleaned_desc = re.sub(r'feed_key\s*:\s*[a-zA-Z0-9_]+', '', cleaned_desc, flags=re.IGNORECASE)
        cleaned_desc = re.sub(r'\boutput_key\b', '', cleaned_desc, flags=re.IGNORECASE)
        cleaned_desc = re.sub(r'\bfeed_key\b', '', cleaned_desc, flags=re.IGNORECASE)
        warnings.append("Se eliminaron menciones a los campos técnicos 'output_key' o 'feed_key' de la descripción.")

    # C. Strip model preamble lines
    cleaned_desc = re.sub(
        r'^(aquí tienes|claro[,!]?|por supuesto[,!]?|a continuación|como solicitaste|entendido[,!]?)[^\n]*\n',
        '', cleaned_desc, flags=re.IGNORECASE
    ).lstrip()

    # D. Length control — allow up to 5000 chars
    MAX_CHARS = 5000
    if len(cleaned_desc) > MAX_CHARS:
        cut = cleaned_desc[:MAX_CHARS]
        last_break = max(cut.rfind('\n\n'), cut.rfind('. '))
        if last_break > int(MAX_CHARS * 0.75):
            cleaned_desc = cleaned_desc[:last_break].rstrip() + "\n\n[Descripción truncada por exceder la longitud máxima.]"
        else:
            cleaned_desc = cut.rstrip() + "..."
        warnings.append("La propuesta se ha recortado para mantener una longitud manejable.")

    # E. Structural quality checks for score_1_10
    if criterion_type == "score_1_10":
        desc_lower = cleaned_desc.lower()
        if not any(x in desc_lower for x in ("9-10", "excelente", "9 -", "10:")):
            warnings.append("La descripción no incluye criterio explícito para puntuación alta (9-10).")
        if not any(x in desc_lower for x in ("5-6", "7-8", "aceptable", "bueno")):
            warnings.append("La descripción no incluye criterio explícito para puntuación media (5-8).")
        if not any(x in desc_lower for x in ("1-2", "3-4", "deficiente", "muy deficiente")):
            warnings.append("La descripción no incluye criterio explícito para puntuación baja (1-4).")
        if "dimensi" not in desc_lower and "criterio" not in desc_lower:
            warnings.append("La descripción no incluye dimensiones de evaluación diferenciadas.")
        if "penaliz" not in desc_lower:
            warnings.append("La descripción no incluye criterios de penalización explícitos.")
        if "null" not in desc_lower:
            warnings.append("La descripción no especifica cuándo devolver null.")

    # F. Feedback mention check
    if feed_key:
        desc_lower = cleaned_desc.lower()
        if not any(x in desc_lower for x in ("justifica", "ejemplo", "cita", "evidencia", "feedback", "explica")):
            warnings.append(f"La descripción no indica cómo justificar el feedback (campo: {feed_key}).")

    # G. Internal duplication check
    lines_for_dup = [ln.strip() for ln in re.split(r'[\n.]', cleaned_desc) if len(ln.strip()) > 40]
    phrase_counts: dict[str, int] = {}
    for p in lines_for_dup:
        phrase_counts[p] = phrase_counts.get(p, 0) + 1
    if any(v > 1 for v in phrase_counts.values()):
        warnings.append("Se detectaron frases duplicadas en la descripción generada.")

    return {
        "ok": True,
        "description": cleaned_desc,
        "warnings": warnings
    }

