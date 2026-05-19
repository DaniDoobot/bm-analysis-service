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

async def build_prompt_with_ai(
    db: AsyncSession,
    prompt_id: int,
    instructions: str | None,
    draft_data: Any | None = None,
    base_structure_id: int | None = None,
) -> dict[str, Any]:
    """
    Generate a new prompt version using AI.
    """
    # 1. Fetch prompt object
    result = await db.execute(select(Prompt).where(Prompt.prompt_id == prompt_id))
    prompt_obj = result.scalars().first()
    if not prompt_obj:
        return {"ok": False, "status": "error", "error_message": f"Prompt_id {prompt_id} not found."}

    # 2. Get current version
    current_version = await _get_current_version(db, prompt_id)
    current_prompt_text = current_version.prompt if current_version else None
    base_version_id = current_version.id if current_version else None

    # 3. Get active criteria
    criteria = await get_active_criteria(db, prompt_id)
    if criteria is None:
        criteria = []

    # 3.2. Fetch base structure if provided
    base_structure = None
    if base_structure_id is not None:
        from app.models.prompts import PromptBaseStructure
        res_struct = await db.execute(
            select(PromptBaseStructure).where(PromptBaseStructure.id == base_structure_id)
        )
        base_structure = res_struct.scalars().first()

    # 4. Build the meta-prompt for OpenAI
    meta_prompt = _build_meta_prompt(
        current_prompt_text=current_prompt_text,
        criteria=criteria,
        general_instructions=instructions,
        draft_data=draft_data,
        base_structure=base_structure,
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

    try:
        raw_response = await openai_service.complete_text(
            messages=messages, response_format="json_object"
        )
    except Exception as e:
        logger.error("Error calling OpenAI: %s", e, exc_info=True)
        return {"ok": False, "status": "error", "error_message": f"OpenAI error: {str(e)}"}

    parsed = safe_parse_json(raw_response)
    if not parsed or not isinstance(parsed, dict):
        logger.error("AI returned non-JSON response: %s", raw_response[:500])
        return {"ok": False, "status": "error", "error_message": "AI did not return valid JSON"}

    generated_prompt = parsed.get("generated_prompt", "")
    
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
            # We check if the allowed_values (stringified) or its items appear in the prompt
            if isinstance(c.allowed_values, list):
                for val in c.allowed_values:
                    if str(val) not in generated_prompt:
                        validation_errors.append(f"El valor permitido '{val}' para la categoría '{c.output_key}' no aparece en el prompt generado.")
            elif isinstance(c.allowed_values, str):
                # Just check if some keywords from the string appear
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
            
    # 3. Check for encoding/mojibake issues
    mojibake_patterns = ["Ã", "Â", "â", "³", "±", "Ã³", "Ã±"]
    for mb in mojibake_patterns:
        if mb in generated_prompt:
            validation_errors.append(f"Se detectaron problemas de codificación (carácter '{mb}').")
            
    if validation_errors:
        error_msg = "El prompt generado falló las validaciones estrictas: " + " ".join(validation_errors)
        logger.error(error_msg)
        return {"ok": False, "status": "error", "error_message": error_msg}

    return {
        "ok": True,
        "status": "completed",
        "prompt_id": prompt_id,
        "prompt_name": prompt_obj.prompt_name,
        "prompt_type": prompt_obj.prompt_type,
        "base_version_id": base_version_id,
        "generated_name": parsed.get("generated_name", f"prompt_ai_{_ts()}"),
        "change_summary": parsed.get("change_summary", ""),
        "generated_prompt": generated_prompt,
        "criteria_count": len(criteria),
    }


def _build_meta_prompt(
    current_prompt_text: str | None,
    criteria: list[PromptCriterion],
    general_instructions: str | None,
    draft_data: Any | None,
    base_structure: Any | None = None,
) -> str:
    """Build the meta-prompt sent to OpenAI."""
    criteria_block = _format_criteria(criteria) if criteria else "(No hay criterios activos configurados)"
    output_format_block = _build_output_format(criteria)

    # 1. Resolve task context based on base_structure or default to Boston Medical
    task_context = "Genera un prompt completo para que un LLM analice llamadas entre agentes y clientes."
    if base_structure:
        if base_structure.structure_key != "blank":
            task_context = f"Genera un prompt completo basado en la estructura '{base_structure.structure_name}' ({base_structure.description or ''})."
    else:
        task_context = "Genera un prompt completo para que un LLM analice llamadas entre agentes de Boston Medical (clínica de salud sexual masculina) y pacientes potenciales."

    # 2. Resolve base rules and baseline structure
    rules_and_base_structure = []
    if base_structure:
        if base_structure.structure_key == "blank":
            rules_and_base_structure = [
                "# Diseño de Estructura Libre",
                "Crea la estructura del prompt de análisis completamente desde cero, asegurando una redacción limpia, fluida y profesional en español.",
                "Define una estructura adecuada para las instrucciones facilitadas."
            ]
        else:
            rules_and_base_structure = [
                "# Estructura base obligatoria de análisis",
                "El prompt generado DEBE basarse estrictamente y heredar las reglas de la siguiente estructura base:",
                base_structure.base_prompt
            ]
    else:
        # Default Boston Medical Rules
        rules_and_base_structure = [
            "# Estructura base obligatoria de análisis",
            "El prompt generado DEBE exigirle al analizador que cumpla estas reglas irrompibles:",
            "1. El analizador clasifica cada llamada en un único tipo_llamada. Los tipos permitidos son estrictamente: cita, informacion_sin_cita, confirmacion, cancelacion, reagendo, falta_con_reagendo, falta_sin_reagendo, no_interesado, no_apto, otros. (Preserva siempre esta lista exacta, no la acortes ni resumas).",
            "2. Evalúa los criterios activos de la base de datos (se listan abajo) y usa sus output_key y feed_key.",
            "3. Devuelve exclusivamente JSON válido. No usa markdown en la salida final (ni ```json).",
            "4. Cíñete ESTRICTAMENTE al formato JSON de salida solicitado. No inventes claves, no omitas claves obligatorias y no reutilices claves antiguas del prompt de referencia.",
            "5. Reglas sobre valores null: distingue entre evaluación del agente y cualificación del paciente.",
            "   - En criterios de evaluación del agente, si el criterio aplica y el agente no lo cumple, NO devuelvas null: devuelve una puntuación baja, 'No' o el valor negativo que corresponda según el tipo de criterio. Usa null solo cuando el criterio no aplique realmente al tipo de llamada, cuando la llamada sea insuficiente para evaluarlo o cuando el audio/transcripción no permita valorar el comportamiento.",
            "   - En criterios de cualificación o datos del paciente, devuelve null si el paciente no facilita esa información explícitamente.",
            "6. En campos textuales de objeciones, objeciones debe ser siempre string; si no hay objeciones, devuelve ''.",
            "7. Cada output_key del listado de criterios activos debe aparecer en el JSON final. Si el criterio tiene feed_key, también debe aparecer."
        ]

    # 3. If there are no criteria
    if not criteria:
        criteria_notice = [
            "# Criterios y Formato de Salida",
            "Este prompt NO tiene criterios de evaluación específicos en este momento. El LLM analizador solo debe clasificar la llamada en 'tipo_llamada' y responder en base a las instrucciones generales sin necesidad de generar una rúbrica o diccionario JSON de criterios de evaluación en la salida final."
        ]
    else:
        criteria_notice = [
            "# Criterios activos (fuente de verdad)",
            "Los siguientes criterios deben estar TODOS documentados e incluidos en el prompt generado.",
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
        "- En la sección de 'CRITERIOS DE ANÁLISIS' del prompt generado, si hay criterios activos, DEBEN LISTARSE TODOS Y CADA UNO de ellos con su definición explícita. No resumas ni agrupes. Todo output_key y feed_key que aparezca en el JSON debe tener su definición explícita en el texto.",
        "- El prompt generado debe contener contexto, la tarea, reglas generales, definiciones EXACTAS y completas de todos los tipos de llamada, definiciones de los criterios (si los hay) y el formato JSON estricto.",
        "- Para los criterios de tipo 'category', el formato JSON final debe mostrar explícitamente los valores permitidos (ej. \"valor1\"|\"valor2\"|null) y no un simple string|null.",
        "- Prohíbe expresamente en el prompt generado el uso de claves legacy como campo_1, campo_2, campo_3, campo_4, campo_5.",
        "- No añadas claves _feed en el formato si el criterio activo no tiene configurado un feed_key.",
        "",
        "# Respuesta esperada de tu parte (como asistente experto)",
        "Responde EXCLUSIVAMENTE con un JSON válido usando estas claves:",
        '{"generated_name": "Nombre corto de esta versión", "change_summary": "Resumen claro de lo que cambiaste", "generated_prompt": "El texto COMPLETO del prompt listo para ser inyectado en el analizador de llamadas"}',
    ])

    return "\n".join(sections)


def _format_criteria(criteria: list[PromptCriterion]) -> str:
    lines = []
    for c in criteria:
        line = (
            f"- [{c.criterion_type}] {c.criterion_name} "
            f"(output_key: {c.output_key}"
        )
        if c.feed_key:
            line += f", feed_key: {c.feed_key}"
        line += ")"
        if c.criterion_description:
            line += f"\n  Descripción: {c.criterion_description}"
        if c.allowed_values:
            line += f"\n  Valores permitidos: {c.allowed_values}"
        lines.append(line)
    return "\n".join(lines)


def _build_output_format(criteria: list[PromptCriterion]) -> str:
    lines = []
    lines.append('  "tipo_llamada": "cita"|"informacion_sin_cita"|"confirmacion"|"cancelacion"|"reagendo"|"falta_con_reagendo"|"falta_sin_reagendo"|"no_interesado"|"no_apto"|"otros"')
    
    for c in criteria:
        # add a comma to the previous line!
        lines[-1] = lines[-1] + ","
        if c.output_key:
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
            lines[-1] = lines[-1] + ","
            lines.append(f'  "{c.feed_key}": "<texto explicativo o justificación>"')
            
    example = "{\n" + "\n".join(lines) + "\n}"
    return f"```json\n{example}\n```"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")


