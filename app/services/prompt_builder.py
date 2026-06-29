"""
Prompt builder service — generates a new prompt using OpenAI based on active criteria.

Critical rules enforced:
- Source of truth for criteria is bm_prompt_criteria (not draft_data).
- Output JSON must only use output_key and feed_key from active criteria.
- No hallucinated keys (campo_1, campo_2, etc.).
- Returns: generated_name, change_summary, generated_prompt.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompts import Prompt
from app.models.criteria import PromptCriterion
from app.services import openai_service
from app.services.criteria_service import get_active_criteria
from app.services.prompts_service import _get_current_version
from app.utils.json_utils import safe_parse_json

logger = logging.getLogger(__name__)

def sanitize_legacy_typologies_block(prompt_text: str, active_typologies: list[Any], legacy_typos_list: list[str] | None = None) -> str:
    """
    Sanitizes or neutralizes legacy typology references in prompt templates.
    Replaces old typologies sections with the current service typologies list
    and clears specific occurrences of legacy typology keys.
    """
    if not prompt_text:
        return ""

    if not active_typologies:
        # Fallback to Front desk typologies as custom Mock classes
        class MockTypology:
            def __init__(self, key, name, desc):
                self.typology_key = key
                self.typology_name = name
                self.description = desc
        active_typologies = [
            MockTypology("cita", "Cita", "El paciente solicita agendar una nueva cita."),
            MockTypology("confirmacion", "Confirmación", "El paciente confirma la asistencia a una cita agendada."),
            MockTypology("cancelacion", "Cancelación", "El paciente cancela la cita agendada."),
            MockTypology("reagendo", "Reagendo", "El paciente reagenda la cita para otra fecha."),
            MockTypology("falta", "Falta", "El paciente no asistió a su cita agendada."),
            MockTypology("otros", "Otros", "Cualquier otra consulta, reclamo o duda.")
        ]

    # Build the new dynamic typologies section
    bullet_lines = []
    for t in active_typologies:
        desc = getattr(t, "description", None) or f"Llamada clasificada como {getattr(t, 'typology_name', t.typology_key)}."
        bullet_lines.append(f"- {t.typology_key}: {desc}")

    dynamic_section = (
        "### DEFINICIÓN DE TIPOS DE LLAMADA\n"
        "El analizador clasifica cada llamada en un único tipo_llamada. Los tipos permitidos son estrictamente:\n" +
        "\n".join(bullet_lines) + "\n\n"
    )

    # 1. Regex to find markdown headers related to call types/typologies
    import re
    header_pattern = re.compile(
        r"(?im)^(#+\s*(?:tipos?\s+de\s+llamada|definición\s+de\s+tipos?|clasificación\s+de\s+llamadas|tipologías\s+del\s+servicio).*?)$"
    )

    match = header_pattern.search(prompt_text)
    if match:
        start_idx = match.start()
        # Find the next header starting with # after this section
        next_header_pattern = re.compile(r"(?m)^#+\s+")
        next_match = next_header_pattern.search(prompt_text, pos=match.end())
        if next_match:
            end_idx = next_match.start()
            # Replace the entire block from the old header to the next header
            prompt_text = prompt_text[:start_idx] + dynamic_section + prompt_text[end_idx:]
        else:
            # If there is no next header, replace until the end of the text
            prompt_text = prompt_text[:start_idx] + dynamic_section
    else:
        # If no explicit header is found, but legacy keywords exist, let's do a safe string replacement
        active_keys = {t.typology_key for t in active_typologies} if active_typologies else set()
        if legacy_typos_list is not None:
            legacy_typos = [lt for lt in legacy_typos_list if lt not in active_keys]
        else:
            legacy_typos = [lt for lt in ["informacion", "informacion_sin_cita", "falta_con_reagendo", "falta_sin_reagendo", "no_interesado", "no_apto"] if lt not in active_keys]
        has_legacy = any(lt in prompt_text for lt in legacy_typos)
        if has_legacy:
            # We prepend the dynamic section to the beginning of the prompt,
            # and explicitly remove legacy bullet lines
            lines = prompt_text.splitlines()
            cleaned_lines = []
            for line in lines:
                if any(lt in line for lt in legacy_typos):
                    continue  # skip lines mentioning legacy typologies
                cleaned_lines.append(line)
            prompt_text = dynamic_section + "\n" + "\n".join(cleaned_lines)

    # Also, double check any remaining direct keyword references and remove/neutralize them
    active_keys = {t.typology_key for t in active_typologies} if active_typologies else set()
    if legacy_typos_list is not None:
        legacy_typos = [lt for lt in legacy_typos_list if lt not in active_keys]
    else:
        legacy_typos = [lt for lt in ["informacion", "informacion_sin_cita", "falta_con_reagendo", "falta_sin_reagendo", "no_interesado", "no_apto"] if lt not in active_keys]
    for lt in legacy_typos:
        prompt_text = prompt_text.replace(f'"{lt}"', '"otros"')
        prompt_text = prompt_text.replace(f"'{lt}'", "'otros'")
        prompt_text = re.sub(rf"\b{lt}\b", "otros", prompt_text)

    return prompt_text


def sanitize_inputs_completely(
    draft_data: Any | None,
    criteria: list[PromptCriterion],
    active_typologies: list[Any],
    criterion_typologies_map: dict[int, list[str]],
    legacy_typos_list: list[str] | None = None
) -> tuple[Any | None, list[PromptCriterion], dict[int, list[str]]]:
    """
    Sanitizes draft_data, database criteria, and relationship maps completely
    to ensure zero legacy typologies leak or cause 500 errors.
    """
    active_keys = [t.typology_key for t in active_typologies] if active_typologies else ["cita", "confirmacion", "cancelacion", "reagendo", "falta", "otros"]
    active_keys_set = set(active_keys)
    
    base_mapping = {
        "informacion": "otros",
        "informacion_sin_cita": "cita",
        "falta_con_reagendo": "falta",
        "falta_sin_reagendo": "falta",
        "no_interesado": "otros",
        "no_apto": "otros"
    }
    
    if legacy_typos_list:
        for lt in legacy_typos_list:
            if lt not in base_mapping:
                base_mapping[lt] = "otros"
                
    legacy_mapping = {
        k: v for k, v in base_mapping.items() if k not in active_keys_set
    }

    # Helper to clean an allowed_values list or string
    def clean_allowed_values(key: str, val: Any) -> Any:
        if key in ("tipo_llamada", "tipo_de_llamada"):
            return list(active_keys)
        if not val:
            return val
        if isinstance(val, list):
            new_list = []
            for item in val:
                if item in legacy_mapping:
                    new_list.append(legacy_mapping[item])
                else:
                    new_list.append(item)
            return sorted(list(set(new_list)))
        if isinstance(val, str):
            items = [i.strip() for i in val.split(",") if i.strip()]
            new_items = []
            for item in items:
                if item in legacy_mapping:
                    new_items.append(legacy_mapping[item])
                else:
                    new_items.append(item)
            return ", ".join(sorted(list(set(new_items))))
        return val

    # Helper to clean applies_to_types list
    def clean_applies_to(applies: Any) -> list[str]:
        if not applies or not isinstance(applies, list):
            return list(active_keys)
        new_applies = []
        for item in applies:
            if item in legacy_mapping:
                new_applies.append(legacy_mapping[item])
            elif item in active_keys:
                new_applies.append(item)
        cleaned = sorted(list(set(new_applies)))
        if not cleaned:
            return list(active_keys)
        return cleaned

    # 1. Sanitize draft_data
    if draft_data and isinstance(draft_data, dict):
        # 1.1 Sanitize draft prompt text
        if "prompt" in draft_data and isinstance(draft_data["prompt"], str):
            draft_data["prompt"] = sanitize_legacy_typologies_block(draft_data["prompt"], active_typologies, legacy_typos_list)
            
        # 1.2 Sanitize draft criteria
        draft_criteria = draft_data.get("criteria")
        if draft_criteria and isinstance(draft_criteria, list):
            for c_dict in draft_criteria:
                if not isinstance(c_dict, dict):
                    continue
                c_key = c_dict.get("criterion_key") or c_dict.get("output_key") or ""
                
                # Clean allowed_values
                if "allowed_values" in c_dict:
                    c_dict["allowed_values"] = clean_allowed_values(c_key, c_dict["allowed_values"])
                    
                # Clean applies_to_types
                if "applies_to_types" in c_dict:
                    c_dict["applies_to_types"] = clean_applies_to(c_dict["applies_to_types"])

    # 2. Sanitize database criteria
    for c in criteria:
        c_key = c.criterion_key or c.output_key or ""
        if c.allowed_values:
            c.allowed_values = clean_allowed_values(c_key, c.allowed_values)
            
    # 3. Sanitize criterion typologies map
    if criterion_typologies_map:
        for c_id, keys in list(criterion_typologies_map.items()):
            criterion_typologies_map[c_id] = clean_applies_to(keys)

    return draft_data, criteria, criterion_typologies_map


async def build_prompt_with_ai(
    db: AsyncSession,
    prompt_id: int,
    instructions: str | None,
    draft_data: Any | None = None,
    base_structure_id: int | None = None,
    version_name: str | None = None,
    change_note: str | None = None,
) -> dict[str, Any]:
    """
    Generate a new prompt version using AI.
    """
    import time
    t_start = time.perf_counter()

    # 1. Fetch prompt object
    result = await db.execute(select(Prompt).where(Prompt.prompt_id == prompt_id))
    prompt_obj = result.scalars().first()
    if not prompt_obj:
        return {"ok": False, "status": "error", "error_message": f"Prompt_id {prompt_id} not found."}

    t_load_prompt = time.perf_counter()

    # 2. Get current version
    current_version = await _get_current_version(db, prompt_id)
    current_prompt_text = current_version.prompt if current_version else None
    base_version_id = current_version.id if current_version else None

    t_load_version = time.perf_counter()

    # 3. Get active criteria
    criteria = await get_active_criteria(db, prompt_id)
    if criteria is None:
        criteria = []

    t_load_criteria = time.perf_counter()

    # 3.2. Fetch base structure based on priority
    resolved_base_structure_id = base_structure_id
    if resolved_base_structure_id is None and prompt_obj.base_structure_id is not None:
        resolved_base_structure_id = prompt_obj.base_structure_id

    base_structure = None
    if resolved_base_structure_id is not None:
        from app.models.prompts import PromptBaseStructure
        res_struct = await db.execute(
            select(PromptBaseStructure).where(PromptBaseStructure.id == resolved_base_structure_id)
        )
        base_structure = res_struct.scalars().first()

    t_load_base = time.perf_counter()

    # 3.5. Fetch active typologies of the service
    service_id = prompt_obj.service_id
    if not service_id and base_structure:
        service_id = base_structure.service_id

    if not service_id:
        from app.models.services import Service
        s_res = await db.execute(select(Service.service_id).where(Service.service_key == "front"))
        service_id = s_res.scalar()

    from app.models.typologies import Typology
    from app.models.criteria import PromptCriterionTypology
    from app.models.prompts import BaseStructureTypology

    typologies = []
    if resolved_base_structure_id:
        # PRIMARY: load typologies associated to this specific base structure
        t_res = await db.execute(
            select(Typology)
            .join(BaseStructureTypology, BaseStructureTypology.typology_id == Typology.typology_id)
            .where(
                BaseStructureTypology.base_structure_id == resolved_base_structure_id,
                Typology.is_active == True,
            )
            .order_by(Typology.sort_order.asc())
        )
        typologies = t_res.scalars().all()
        logger.debug(
            "Typologies loaded from base_structure_id=%s: %s",
            resolved_base_structure_id,
            [t.typology_key for t in typologies],
        )

    if not typologies and service_id:
        # FALLBACK: no base structure or no associations → load all active typologies of the service
        logger.debug(
            "No typologies found for base_structure_id=%s; falling back to service_id=%s",
            resolved_base_structure_id,
            service_id,
        )
        t_res = await db.execute(
            select(Typology)
            .where(Typology.service_id == service_id, Typology.is_active == True)
            .order_by(Typology.sort_order.asc())
        )
        typologies = t_res.scalars().all()

    t_load_typos = time.perf_counter()

    # Fetch criterion-typology mappings (Optimized batch query instead of N+1)
    criterion_typologies_map = {c.criterion_id: [] for c in criteria}
    if criteria:
        criteria_ids = [c.criterion_id for c in criteria]
        assoc_res = await db.execute(
            select(PromptCriterionTypology.criterion_id, Typology.typology_key)
            .join(Typology, PromptCriterionTypology.typology_id == Typology.typology_id)
            .where(PromptCriterionTypology.criterion_id.in_(criteria_ids), Typology.is_active == True)
        )
        for c_id, t_key in assoc_res.all():
            if c_id in criterion_typologies_map:
                criterion_typologies_map[c_id].append(t_key)

    t_load_mappings = time.perf_counter()

    # 3.6. Check for legacy typologies before sanitization for logging purposes
    active_keys = {t.typology_key for t in typologies} if typologies else set()
    
    # Fetch all typology keys from the DB to dynamically find any inactive/legacy ones
    try:
        all_typos_res = await db.execute(select(Typology.typology_key))
        all_typos_keys = set(all_typos_res.scalars().all())
    except Exception as e:
        logger.warning("Failed to fetch all typologies from DB: %s", e)
        all_typos_keys = set()
        
    legacy_keys_from_db = all_typos_keys - active_keys
    legacy_keys_set = {"informacion", "informacion_sin_cita", "falta_con_reagendo", "falta_sin_reagendo", "no_interesado", "no_apto"}
    legacy_keys_set.update(legacy_keys_from_db)
    legacy_typos = [lt for lt in legacy_keys_set if lt not in active_keys]
    
    def detect_legacy_in_obj(obj: Any) -> bool:
        if not obj:
            return False
        if isinstance(obj, str):
            return any(lt in obj for lt in legacy_typos)
        if isinstance(obj, (list, tuple, set)):
            return any(detect_legacy_in_obj(x) for x in obj)
        if isinstance(obj, dict):
            return any(detect_legacy_in_obj(k) or detect_legacy_in_obj(v) for k, v in obj.items())
        if hasattr(obj, "__dict__"):
            return any(detect_legacy_in_obj(v) for k, v in obj.__dict__.items() if not k.startswith("_"))
        try:
            return any(lt in str(obj) for lt in legacy_typos)
        except Exception:
            return False

    legacy_detected_before = (
        detect_legacy_in_obj(current_prompt_text) or
        detect_legacy_in_obj(base_structure.base_prompt if base_structure else None) or
        detect_legacy_in_obj(draft_data) or
        detect_legacy_in_obj(criteria) or
        detect_legacy_in_obj(criterion_typologies_map)
    )

    # 3.7. Perform complete input, draft, and criteria sanitization of legacy typologies
    draft_data, criteria, criterion_typologies_map = sanitize_inputs_completely(
        draft_data=draft_data,
        criteria=criteria,
        active_typologies=typologies,
        criterion_typologies_map=criterion_typologies_map,
        legacy_typos_list=legacy_typos
    )

    # 3.8. Sanitize templates of legacy typologies before sending them to OpenAI
    sanitized_current_prompt = sanitize_legacy_typologies_block(current_prompt_text, typologies, legacy_typos) if current_prompt_text else None
    
    sanitized_base_prompt = None
    if base_structure and base_structure.base_prompt:
        sanitized_base_prompt = sanitize_legacy_typologies_block(base_structure.base_prompt, typologies, legacy_typos)

    # 3.9. Check legacy presence after sanitization and write execution logs
    legacy_detected_after = (
        detect_legacy_in_obj(sanitized_current_prompt) or
        detect_legacy_in_obj(sanitized_base_prompt) or
        detect_legacy_in_obj(draft_data) or
        detect_legacy_in_obj(criteria) or
        detect_legacy_in_obj(criterion_typologies_map)
    )

    logger.info(
        "Build-with-AI execution info:\n"
        " - prompt_id: %s\n"
        " - base_structure_id: %s\n"
        " - service_id: %s\n"
        " - typologies activas: %s\n"
        " - legacy_detected_before_sanitize: %s\n"
        " - legacy_detected_after_sanitize: %s",
        prompt_id,
        resolved_base_structure_id,
        service_id,
        [t.typology_key for t in typologies],
        legacy_detected_before,
        legacy_detected_after
    )

    # 4. Build the meta-prompt for OpenAI
    meta_prompt = _build_meta_prompt(
        current_prompt_text=sanitized_current_prompt,
        criteria=criteria,
        general_instructions=instructions,
        draft_data=draft_data,
        base_structure=base_structure,
        typologies=typologies,
        criterion_typologies_map=criterion_typologies_map,
        sanitized_base_prompt=sanitized_base_prompt,
    )

    messages = [
        {
            "role": "system",
            "content": (
                "Eres un experto en diseño de prompts para análisis de llamadas de salud. "
                "Tu tarea es generar un prompt completo, estructurado y listo para usar en producción. "
                "Debes responder EXCLUSIVAMENTE con un JSON válido."
            ),
        },
        {"role": "user", "content": meta_prompt},
    ]

    t_meta_prompt = time.perf_counter()

    # Try generating up to 1 time (no sequential corrective LLM retries)
    max_attempts = 1
    attempt = 1
    current_messages = list(messages)
    parsed = None
    generated_prompt = ""

    t_llm_start = time.perf_counter()

    while attempt <= max_attempts:
        try:
            raw_response = await openai_service.complete_text(
                messages=current_messages, response_format="json_object"
            )
        except Exception as e:
            logger.error("Error calling OpenAI: %s", e, exc_info=True)
            return {"ok": False, "status": "error", "error_message": f"OpenAI error: {str(e)}"}

        parsed = safe_parse_json(raw_response)
        if not parsed or not isinstance(parsed, dict):
            logger.error("AI returned non-JSON response: %s", raw_response[:500])
            return {"ok": False, "status": "error", "error_message": "AI did not return valid JSON"}

        generated_prompt = parsed.get("generated_prompt", "")
        from app.services.criteria_service import deduplicate_criteria_blocks
        from app.services.prompts_service import clean_whitespaces
        generated_prompt = clean_whitespaces(deduplicate_criteria_blocks(generated_prompt))
        
        # --- POST-GENERATION VALIDATION ---
        validation_errors = []
        
        # 1. Check for output_key and feed_key presence (at least twice: once in JSON format, once in definitions)
        for c in criteria:
            if c.output_key:
                count = generated_prompt.count(c.output_key)
                if count == 0:
                    validation_errors.append(f"Falta la clave obligatoria '{c.output_key}'.")
                elif count == 1:
                    validation_errors.append(f"La clave '{c.output_key}' aparece en el JSON pero no está definida en la sección de criterios del prompt (o viceversa). Debe aparecer al menos 2 veces.")
            if c.feed_key:
                count = generated_prompt.count(c.feed_key)
                if count == 0:
                    validation_errors.append(f"Falta la clave de justificación '{c.feed_key}'.")
                elif count == 1:
                    validation_errors.append(f"La clave '{c.feed_key}' aparece solo una vez. Debe estar en el formato JSON y tener su definición en el texto.")
                    
            # Validar allowed_values para categories
            if c.criterion_type == "category" and c.allowed_values:
                if isinstance(c.allowed_values, list):
                    for val in c.allowed_values:
                        if str(val) not in generated_prompt:
                            validation_errors.append(f"El valor permitido '{val}' para la categoría '{c.output_key}' no aparece en el prompt generado.")
                elif isinstance(c.allowed_values, str):
                    vals = [v.strip() for v in c.allowed_values.split(",") if v.strip()]
                    for val in vals:
                        if val not in generated_prompt:
                            validation_errors.append(f"El valor permitido '{val}' para '{c.output_key}' no aparece en el prompt generado.")
                
        # 2. Check for legacy keys contextually
        legacy_keys = ["campo_1", "campo_2", "campo_3", "campo_4", "campo_5"]
        for lk in legacy_keys:
            patterns = [
                rf"['\"]{lk}['\"]\s*:",
                rf"['\"]{lk}_feed['\"]\s*:",
                rf"output_key\s*:\s*{lk}\b",
                rf"feed_key\s*:\s*{lk}_feed\b",
                rf"criterion_key\s*:\s*{lk}\b"
            ]
            for pat in patterns:
                match = re.search(pat, generated_prompt)
                if match:
                    start_idx = max(0, match.start() - 30)
                    end_idx = min(len(generated_prompt), match.end() + 30)
                    context = generated_prompt[start_idx:end_idx].replace('\n', '\\n')
                    validation_errors.append(f"Uso estructural de clave prohibida '{lk}'. Contexto: '...{context}...'")
                    break

        # 2.2. Prevent legacy typologies leakage
        active_keys = {t.typology_key for t in typologies} if typologies else set()
        legacy_typos = [lt for lt in ["informacion_sin_cita", "falta_con_reagendo", "falta_sin_reagendo", "no_interesado", "no_apto"] if lt not in active_keys]
        has_legacy_leak = False
        for lt in legacy_typos:
            if lt in generated_prompt:
                has_legacy_leak = True
                validation_errors.append(f"El prompt generado contiene la tipología antigua prohibida '{lt}'.")

        # If there is a legacy typologies leak and it's the first attempt, try correcting it!
        if has_legacy_leak and attempt < max_attempts:
            logger.warning("Attempt %d: Legacy typologies leak detected in generated prompt. Retrying with a stronger corrective instruction...", attempt)
            # Append raw response and a corrective user instruction
            current_messages.append({"role": "assistant", "content": raw_response})
            current_messages.append({
                "role": "user",
                "content": (
                    "¡ERROR CRÍTICO! Has incluido tipologías antiguas prohibidas en el prompt generado. "
                    "Por favor, vuelve a generar el prompt y asegúrate de eliminar por completo y no mencionar ninguna de las siguientes palabras: "
                    f"{', '.join(legacy_typos)}. "
                    "Usa estrictamente las nuevas tipologías dinámicas permitidas: " + (", ".join([t.typology_key for t in typologies]) if typologies else "cita, confirmacion, cancelacion, reagendo, falta, otros")
                )
            })
            attempt += 1
            continue

        # 3. Check for encoding/mojibake issues
        mojibake_patterns = ["Ã", "Â", "â", "³", "±", "Ã³", "Ã±"]
        for mb in mojibake_patterns:
            if mb in generated_prompt:
                validation_errors.append(f"Se detectaron problemas de codificación (carácter '{mb}').")
                
        if validation_errors:
            legacy_details = []
            if has_legacy_leak:
                try:
                    leaked_keys = [lt for lt in legacy_typos if lt in generated_prompt]
                    for lt in leaked_keys:
                        # Find all matching typologies (both active and inactive) in DB
                        from app.models.typologies import Typology
                        t_db_res = await db.execute(
                            select(Typology).where(Typology.typology_key == lt)
                        )
                        t_db_objs = t_db_res.scalars().all()
                        
                        normalized_mapped = {"informacion_sin_cita": "cita", "falta_con_reagendo": "falta", "falta_sin_reagendo": "falta", "no_interesado": "otros", "no_apto": "otros"}.get(lt, "otros")
                        
                        if not t_db_objs:
                            legacy_details.append({
                                "typology_id": None,
                                "typology_key": lt,
                                "typology_name": None,
                                "service_id": service_id,
                                "estado": "not_in_db",
                                "deleted_at": None,
                                "tabla_origen": "bm_typologies",
                                "normalized_key": normalized_mapped
                            })
                        else:
                            for t_obj in t_db_objs:
                                legacy_details.append({
                                    "typology_id": t_obj.typology_id,
                                    "typology_key": t_obj.typology_key,
                                    "typology_name": t_obj.typology_name,
                                    "service_id": t_obj.service_id,
                                    "estado": "active" if t_obj.is_active else "inactive/soft-deleted",
                                    "deleted_at": None if t_obj.is_active else "is_active=False",
                                    "tabla_origen": "bm_typologies",
                                    "normalized_key": normalized_mapped
                                })
                    
                    logger.error(
                        "LEGACY TYPOLOGY LEAK DETAIL REPORT:\n"
                        " - Service ID: %s\n"
                        " - Structure ID: %s\n"
                        " - Detailed records: %s",
                        service_id,
                        resolved_base_structure_id,
                        legacy_details
                    )
                except Exception as log_ex:
                    logger.error("Error creating legacy leak details report: %s", log_ex)

                error_msg = (
                    "No se pudo generar una estructura válida porque el texto base contiene tipologías legacy. "
                    "Actualiza la estructura base o limpia el bloque de tipologías."
                )
            else:
                error_msg = "El prompt generado falló las validaciones estrictas: " + " ".join(validation_errors)
                
            logger.error(error_msg)
            return {
                "ok": False,
                "status": "error",
                "error_message": error_msg,
                "legacy_details": legacy_details
            }

        # If we successfully parsed and validated, exit loop
        break

    t_llm_end = time.perf_counter()
    t_validation_end = time.perf_counter()

    t_total = time.perf_counter() - t_start
    duration_db_prompt = (t_load_prompt - t_start) * 1000
    duration_db_version = (t_load_version - t_load_prompt) * 1000
    duration_db_criteria = (t_load_criteria - t_load_version) * 1000
    duration_db_base = (t_load_base - t_load_criteria) * 1000
    duration_db_typos = (t_load_typos - t_load_base) * 1000
    duration_db_mappings = (t_load_mappings - t_load_typos) * 1000
    duration_meta_prompt = (t_meta_prompt - t_load_mappings) * 1000
    duration_llm = (t_llm_end - t_llm_start) * 1000
    duration_validation = (t_validation_end - t_llm_end) * 1000
    
    logger.info(
        "\n==================================================\n"
        "PERFORMANCE DIAGNOSIS REPORT - PROMPT BUILD WITH AI\n"
        "==================================================\n"
        f"- Carga de prompt (DB): {duration_db_prompt:.2f} ms\n"
        f"- Carga de versión (DB): {duration_db_version:.2f} ms\n"
        f"- Carga de criterios (DB): {duration_db_criteria:.2f} ms\n"
        f"- Carga de estructura base (DB): {duration_db_base:.2f} ms\n"
        f"- Carga de tipologías (DB): {duration_db_typos:.2f} ms\n"
        f"- Carga de mapeos de tipologías (DB - Optimizado): {duration_db_mappings:.2f} ms\n"
        f"- Construcción de prompt / sanitización: {duration_meta_prompt:.2f} ms\n"
        f"- Llamada OpenAI/LLM: {duration_llm:.2f} ms ({duration_llm/1000:.2f} s)\n"
        f"- Validación post-generación: {duration_validation:.2f} ms\n"
        "--------------------------------------------------\n"
        f"DURACIÓN TOTAL RESPUESTA: {t_total:.4f} s\n"
        "=================================================="
    )

    # Extract, validate and map improved descriptions suggestions
    improved_descriptions = parsed.get("improved_criteria_descriptions", [])
    active_criterion_ids = {c.criterion_id for c in criteria}
    active_criterion_keys = {c.criterion_key for c in criteria if c.criterion_key}
    
    validated_improved_descriptions = []
    if isinstance(improved_descriptions, list):
        for desc_entry in improved_descriptions:
            if not isinstance(desc_entry, dict):
                continue
            c_id = desc_entry.get("criterion_id")
            c_key = desc_entry.get("criterion_key")
            
            is_valid = False
            if c_id is not None:
                try:
                    c_id_int = int(c_id)
                    if c_id_int in active_criterion_ids:
                        is_valid = True
                except (ValueError, TypeError):
                    pass
            
            if not is_valid and c_key in active_criterion_keys:
                is_valid = True
                matching_c = next((c for c in criteria if c.criterion_key == c_key), None)
                if matching_c:
                    desc_entry["criterion_id"] = matching_c.criterion_id
            
            if is_valid:
                validated_improved_descriptions.append({
                    "criterion_id": desc_entry.get("criterion_id"),
                    "criterion_key": desc_entry.get("criterion_key") or c_key,
                    "original_description": desc_entry.get("original_description") or "",
                    "improved_description": desc_entry.get("improved_description") or "",
                    "reason": desc_entry.get("reason") or "Mejora de claridad y redactabilidad."
                })

    improved_count = len(validated_improved_descriptions)
    unchanged_count = len(criteria) - improved_count
    
    logger.info(
        "\n==================================================\n"
        "REFORMULATION DIAGNOSIS REPORT:\n"
        "==================================================\n"
        f"- Criterios revisados: {len(criteria)}\n"
        f"- Descripciones mejoradas: {improved_count}\n"
        f"- Descripciones sin cambios: {unchanged_count}\n"
        "=================================================="
    )

    return {
        "ok": True,
        "status": "completed",
        "prompt_id": prompt_id,
        "prompt_name": prompt_obj.prompt_name,
        "prompt_type": prompt_obj.prompt_type,
        "base_version_id": base_version_id,
        "generated_name": version_name or parsed.get("generated_name", f"prompt_ai_{_ts()}"),
        "change_summary": change_note or parsed.get("change_summary", ""),
        "generated_prompt": generated_prompt,
        "criteria_count": len(criteria),
        "improved_criteria_descriptions": validated_improved_descriptions,
    }


def _build_meta_prompt(
    current_prompt_text: str | None,
    criteria: list[PromptCriterion],
    general_instructions: str | None,
    draft_data: Any | None,
    base_structure: Any | None = None,
    typologies: list[Any] = None,
    criterion_typologies_map: dict[int, list[str]] = None,
    sanitized_base_prompt: str | None = None,
) -> str:
    """Build the meta-prompt sent to OpenAI."""
    criteria_block = _format_criteria(criteria, criterion_typologies_map) if criteria else "(No hay criterios activos configurados)"
    output_format_block = _build_output_format(criteria, typologies)

    # 1. Resolve task context based on base_structure or default to Boston Medical
    task_context = "Genera un prompt completo para que un LLM analice llamadas entre agentes y clientes."
    if base_structure:
        if base_structure.structure_key != "blank":
            task_context = f"Genera un prompt completo basado en la estructura '{base_structure.structure_name}' ({base_structure.description or ''})."
    else:
        task_context = "Genera un prompt completo para que un LLM analice llamadas entre agentes de Boston Medical (clínica de salud sexual masculina) y pacientes potenciales."

    # 2. Resolve base rules and baseline structure
    typology_keys_str = ", ".join([t.typology_key for t in typologies]) if typologies else "cita, confirmacion, cancelacion, reagendo, falta, otros"
    
    rules_and_base_structure = [
        "# Reglas irrompibles de análisis",
        "El prompt generado DEBE exigirle al analizador que cumpla estas reglas irrompibles:",
        f"1. El analizador clasifica cada llamada en un único tipo_llamada. Los tipos permitidos son estrictamente: {typology_keys_str}. (El prompt generado debe listar y exigir únicamente esta lista exacta, prohibiendo expresamente cualquier otra tipología como informacion_sin_cita, falta_con_reagendo, falta_sin_reagendo, no_interesado, no_apto, etc.).",
        "2. Evalúa los criterios activos de la base de datos (se listan abajo) y usa sus output_key y feed_key.",
        "3. Devuelve exclusivamente JSON válido. No usa markdown en la salida final (ni ```json).",
        "4. Cíñete ESTRICTAMENTE al formato JSON de salida solicitado. No inventes claves, no omitas claves obligatorias y no reutilices claves antiguas o desactualizadas del prompt de referencia.",
        "5. Reglas sobre valores null y aplicabilidad por tipología:",
        "   - IMPORTANTE: Evalúa cada criterio ÚNICAMENTE si la tipología de llamada clasificada está dentro de sus 'Tipologías aplicables'. Si la tipología de la llamada NO es aplicable para un criterio determinado, debes devolver estrictamente null en su output_key (y en su feed_key si lo tiene).",
        "   - Para los criterios aplicables, si el agente no cumple con la conducta esperada, NO devuelvas null: devuelve una puntuación baja, 'No' o el valor negativo que corresponda según el tipo de criterio. Usa null para criterios aplicables solo cuando sea imposible evaluar por falta de datos en el audio o transcripción.",
        "   - En criterios de cualificación o datos del paciente, devuelve null si el paciente no facilita esa información explícitamente.",
        "6. En campos textuales de objeciones, objeciones debe ser siempre string; si no hay objeciones, devuelve ''.",
        "7. Cada output_key del listado de criterios activos debe aparecer en el JSON final. Si el criterio tiene feed_key, también debe aparecer."
    ]

    if base_structure:
        if base_structure.structure_key == "blank":
            rules_and_base_structure.extend([
                "",
                "# Diseño de Estructura Libre",
                "Crea la estructura del prompt de análisis completamente desde cero, asegurando una redacción limpia, fluida y profesional en español.",
                "Define una estructura adecuada para las instrucciones facilitadas."
            ])
        else:
            rules_and_base_structure.extend([
                "",
                "# Estructura base obligatoria de referencia",
                "El prompt generado DEBE basarse, complementar y heredar las directrices estilísticas y conceptuales de la siguiente estructura base, pero adaptándolas SIEMPRE a las reglas irrompibles de arriba (ej. sustituyendo cualquier lista antigua de tipologías por la nueva lista exacta de tipo_llamada permitida):",
                sanitized_base_prompt or base_structure.base_prompt
            ])

    # 3. Description Review and Reformulation Rules
    reformulation_instructions = [
        "",
        "# REVISIÓN Y REFORMULACIÓN INTELIGENTE DE CRITERIOS",
        "Antes de redactar la estructura final, debes revisar críticamente las descripciones de todos los criterios activos proporcionados.",
        "Si una descripción es ambigua, incompleta, poco evaluable, demasiado corta o está mal redactada, DEBES REFORMULARLA para que sea clara, observable y útil para evaluar una llamada. Mantén siempre la intención original del criterio.",
        "",
        "Criterios de calidad que debe cumplir la descripción reformulada:",
        "1. Explicar claramente qué conductas, palabras o hechos específicos deben observarse en la llamada.",
        "2. Ser objetiva y fácilmente evaluable por un modelo de lenguaje sin ambigüedad.",
        "3. Evitar frases vagas como 'evaluar bien', 'ver si lo hace correcto', 'analizar comportamiento'.",
        "4. Indicar qué se considera un buen desempeño y qué se considera un incumplimiento.",
        "5. No mezclar múltiples objetivos en un solo criterio (mantener foco funcional).",
        "6. Aplicación según tipo de criterio (criterion_type):",
        "   - score_1_10: Definir de forma gradual qué amerita puntuaciones bajas, medias y altas.",
        "   - boolean: Dejar muy claro qué constituye un 'true' (cumplido) y un 'false' (incumplido).",
        "   - category: Respetar únicamente los valores permitidos de allowed_values y definir cuándo aplica cada uno.",
        "   - text / free_text: Indicar qué dato textual preciso extraer y recalcar que solo debe extraerse si aparece explícitamente (si no, devolver '').",
        "   - number: Indicar qué número extraer o calcular y en qué unidad.",
        "   - percentage: Indicar qué porcentaje calcular y sobre qué base.",
        "",
        "Reglas irrompibles para la reformulación:",
        "1. Mantén siempre la intención original y la aplicabilidad por tipología de cada criterio.",
        "2. NUNCA inventes criterios nuevos ni elimines criterios activos existentes.",
        "3. Conserva intactos todos los identificadores y claves técnicas: criterion_id, criterion_key, output_key, feed_key y criterion_type.",
        "4. Usa la descripción reformulada y mejorada en la redacción de la sección 'CRITERIOS DE ANÁLISIS' del prompt generado.",
        "5. Devuelve la lista estructurada de descripciones originales vs reformuladas en la clave 'improved_criteria_descriptions' de tu JSON de respuesta."
    ]

    # 4. If there are no criteria
    if not criteria:
        criteria_notice = [
            "# Criterios y Formato de Salida",
            "Este prompt NO tiene criterios de evaluación específicos en este momento. El LLM analizador solo debe clasificar la llamada en 'tipo_llamada' y responder en base a las instrucciones generales sin necesidad de generar una rúbrica o diccionario JSON de criterios de evaluación en la salida final."
        ]
    else:
        criteria_notice = [
            "# Criterios activos (fuente de verdad)",
            "Los siguientes criterios deben estar TODOS documentados e incluidos en el prompt generado utilizando sus descripciones mejoradas/reformuladas.",
            "NO inventes criterios. NO omitas ninguno. NO uses campos genéricos heredados como campo_1, campo_2, etc.",
            "",
            criteria_block,
            "",
            "# Formato de salida JSON que el prompt debe producir",
            "El prompt generado DEBE instruir al LLM analizador a devolver EXACTAMENTE este JSON, sin omitir ninguna de estas claves:",
            "",
            output_format_block
        ]

    sections = [
        "# Tarea",
        task_context,
        "Genera texto limpio y nativo en español, sin problemas de codificación (NO emitas caracteres extraños como Ã, Â, etc).",
        "",
    ]
    sections.extend(rules_and_base_structure)
    sections.extend(reformulation_instructions)
    sections.extend([
        "",
        "# CRITERIOS DE EVALUACIÓN VS DATOS DEL PACIENTE",
        "El prompt generado debe explicar claramente al analizador la diferencia de comportamiento:",
        "- Los criterios de desempeño del agente se evalúan aunque el agente no cumpla: en ese caso se penaliza.",
        "- Los datos del paciente se extraen solo si aparecen explícitamente.",
        "- 'null' no significa 'mal desempeño'; 'null' significa 'no aplicable o imposible de evaluar'.",
        "- El mal desempeño debe reflejarse con puntuación baja o 'No'.",
        "",
        "# Instrucciones del usuario (prioridad máxima para los cambios)",
        general_instructions or "(No se proporcionaron instrucciones adicionales)",
        "",
        "# PROMPT ACTUAL SOLO COMO REFERENCIA CONCEPTUAL PARCIAL",
        "Puede contener reglas antiguas, formatos obsoletos, criterios inactivos o esquemas JSON anteriores. NO copies su formato de salida. NO uses sus claves como fuente de verdad. La fuente de verdad del formato de salida son exclusivamente los criterios activos y la lista exacta de output_key/feed_key.",
        current_prompt_text or "(No existe prompt previo, comienza desde cero)",
        "",
    ])
    sections.extend(criteria_notice)
    sections.extend([
        "",
        "# Reglas críticas para el prompt generado",
        "- Usa estructura Markdown sencilla (e.g., ### REGLAS GENERALES) y NO uses separadores decorativos ASCII ni caracteres especiales (e.g., ─────).",
        "- El prompt generado debe escribirse fluido y no parecer texto pegado de fragmentos aislados.",
        "- En la sección de 'CRITERIOS DE ANÁLISIS' del prompt generado, si hay criterios activos, DEBEN LISTARSE TODOS Y CADA UNO de ellos con su definición explícita usando la versión reformulada y mejorada. No resumas ni agrupes. Todo output_key y feed_key que aparezca en el JSON debe tener su definición explícita en el texto.",
        "- El prompt generado debe contener contexto, la tarea, reglas generales, definiciones EXACTAS y completas de todos los tipos de llamada, definiciones de los criterios (si los hay) y el formato JSON estricto.",
        "- Para los criterios de tipo 'category', el formato JSON final debe mostrar explícitamente los valores permitidos (ej. \"valor1\"|\"valor2\"|null) y no un simple string|null.",
        "- Prohíbe expresamente en el prompt generado el uso de claves legacy como campo_1, campo_2, campo_3, campo_4, campo_5.",
        "- No añadas claves _feed en el formato si el criterio activo no tiene configurado un feed_key.",
        "",
        "# Respuesta esperada de tu parte (como asistente experto)",
        "Responde EXCLUSIVAMENTE con un JSON válido usando estas claves:",
        '{"generated_name": "Nombre corto de esta versión", "change_summary": "Resumen claro de lo que cambiaste", "generated_prompt": "El texto COMPLETO del prompt listo para ser inyectado en el analizador de llamadas (usando las descripciones reformuladas)", "improved_criteria_descriptions": [{"criterion_id": 123, "criterion_key": "clave_criterio", "original_description": "descripción original", "improved_description": "descripción reformulada y mejorada", "reason": "explicación clara de por qué se mejoró la descripción"}]}',
    ])

    return "\n".join(sections)


def _format_criteria(criteria: list[PromptCriterion], criterion_typologies_map: dict[int, list[str]] = None) -> str:
    lines = []
    for c in criteria:
        line = (
            f"- [ID: {c.criterion_id}] [{c.criterion_type}] {c.criterion_name} "
            f"(output_key: {c.output_key}"
        )
        if c.feed_key:
            line += f", feed_key: {c.feed_key}"
        line += ")"
        if c.criterion_description:
            line += f"\n  Descripción: {c.criterion_description}"
        if c.allowed_values:
            line += f"\n  Valores permitidos: {c.allowed_values}"
        
        # Add applicable typologies info
        applies_to = criterion_typologies_map.get(c.criterion_id) if criterion_typologies_map else None
        if applies_to:
            line += f"\n  Tipologías aplicables: {', '.join(applies_to)}"
        else:
            line += f"\n  Tipologías aplicables: Todas"
            
        lines.append(line)
    return "\n".join(lines)


def _build_output_format(criteria: list[PromptCriterion], typologies: list[Any] = None) -> str:
    lines = []
    seen_keys = {"tipo_llamada"}
    
    # 1. Add tipo_llamada first
    if typologies:
        options = "|".join([f'"{t.typology_key}"' for t in typologies]) + "|null"
    else:
        options = '"cita"|"confirmacion"|"cancelacion"|"reagendo"|"falta"|"otros"|null'
        
    lines.append(f'  "tipo_llamada": {options}')
    
    for c in criteria:
        if c.output_key:
            if c.output_key in seen_keys:
                continue
            
            seen_keys.add(c.output_key)
            lines[-1] = lines[-1] + ","
            
            if c.criterion_type == "category" and c.allowed_values:
                if isinstance(c.allowed_values, list):
                    vals_str = "|".join([f'"{v}"' for v in c.allowed_values]) + "|null"
                elif isinstance(c.allowed_values, str):
                    vals = [v.strip() for v in c.allowed_values.split(",") if v.strip()]
                    vals_str = "|".join([f'"{v}"' for v in vals]) + "|null"
                else:
                    vals_str = f'<{c.criterion_type}>'
                lines.append(f'  "{c.output_key}": {vals_str}')
            else:
                lines.append(f'  "{c.output_key}": "<{c.criterion_type}>"')
                
        if c.feed_key:
            if c.feed_key in seen_keys:
                continue
            
            seen_keys.add(c.feed_key)
            lines[-1] = lines[-1] + ","
            lines.append(f'  "{c.feed_key}": "<texto explicativo o justificación>"')
            
    example = "{\n" + "\n".join(lines) + "\n}"
    return f"```json\n{example}\n```"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")


