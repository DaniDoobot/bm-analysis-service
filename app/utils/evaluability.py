"""
Utility for determining call evaluability across all services and prompt structures.
Tri-state semantic evaluability:
- True: Analysis completed and sufficient bilateral conversation exists to evaluate agent quality.
- False: Analysis completed but conversation was insufficient (dropped call, voicemail, no bilateral dialogue).
- None: Unknown or undetermined (technical failure / failed processing / legacy unbackfilled rows).
"""
from typing import Any

METADATA_CRITERIA_KEYS = {
    "hablando_agente", "hablando_paciente", "tiempo_hablando",
    "tiempo_hablando_agente", "tiempo_hablando_paciente",
    "palabras_minuto_agente", "palabras_minuto_paciente",
    "tipo_llamada", "patologia", "meses_patologia", "resumen",
    "motivo_no_cita", "motivo_insatisfaccion_encuesta", "doctor_afetado",
    "cuanto_tiempo", "por_que_ahora", "objecion_1", "objecion_2", "objecion_3", "objeciones"
}


def determine_evaluability(
    typology_key: str | None,
    result_json: dict[str, Any] | None,
    items: list[dict[str, Any]] | None = None,
    call_duration_seconds: int | None = None,
    status: str | None = "completed",
) -> tuple[bool | None, str]:
    """
    Dynamically determines if a call has sufficient conversation to be evaluated for quality.
    Works dynamically across any service (Front, Experiencia de Paciente, ESIC, etc.).
    
    Returns:
      (True, 'valid_conversation')           -> Completed with evaluable conversation
      (False, non_evaluable_reason)          -> Completed but non-evaluable (dropped, voicemail, no bilateral speech)
      (None, 'technical_failure_or_unknown') -> Failed processing or undetermined historical record
    """
    # 1. Technical failure or absent data
    if status == "failed":
        return None, "technical_failure"
    if not result_json and not items:
        return None, "no_analysis_data"

    rj = result_json or {}
    t_key = (typology_key or rj.get("tipo_llamada") or "").strip().lower()

    # 2. Explicit strong non-conversational typologies (voicemail / no answer)
    if t_key in ("buzon_de_voz", "buzon", "no_responde"):
        return False, f"typology_{t_key}"

    # 3. Dynamic count of evaluated substantive criteria (numeric + substantive evaluated items)
    scored_items_count = 0
    substantive_eval_count = 0
    if items and isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                k = (item.get("criterion_key") or item.get("output_key") or "").lower()
                if k in METADATA_CRITERIA_KEYS:
                    continue
                # Count criteria that produced a real numeric evaluation
                if item.get("numeric_value") is not None:
                    scored_items_count += 1
                    substantive_eval_count += 1
                elif item.get("boolean_value") is not None:
                    # Both True and False represent an evaluated boolean criterion (only None is non-evaluated)
                    substantive_eval_count += 1
    elif isinstance(rj, dict):
        for k, v in rj.items():
            if k.lower() in METADATA_CRITERIA_KEYS:
                continue
            if v is not None and v != "" and str(v).lower() not in ("null", "none", "no aplica", "n/a"):
                try:
                    float(v)
                    scored_items_count += 1
                    substantive_eval_count += 1
                except (ValueError, TypeError):
                    if str(v).lower() in ("si", "sí", "true", "no", "false", "1", "0"):
                        substantive_eval_count += 1

    # 4. Patient speaking percentage (if present in result)
    hab_paciente_pct = None
    for k in ("hablando_paciente", "tiempo_hablando_paciente", "paciente_habla"):
        if k in rj and rj[k] is not None:
            s_val = str(rj[k]).replace("%", "").strip()
            try:
                hab_paciente_pct = float(s_val)
                break
            except (ValueError, TypeError):
                pass

    # 5. Dropped/cut call detection from structured reason / motive
    motivo = str(rj.get("motivo_no_cita") or rj.get("motivo_insatisfaccion_encuesta") or "").lower()
    is_cut_call = any(w in motivo for w in [
        "llamada caída", "llamada caida", "llamada cortada", "interrumpida al inicio",
        "llamada interrumpida", "interrupción inmediata", "corta de forma abrupta",
        "interlocutor equivocado / llamada cortada"
    ])

    # Dynamic rules:
    # A. Zero substantive criteria evaluated across ANY structure
    if substantive_eval_count == 0:
        return False, "zero_evaluated_criteria"

    # B. Dropped / cut call with minimal criteria evaluated (<= 2)
    if is_cut_call and substantive_eval_count <= 2:
        return False, "dropped_or_cut_call"

    # C. Zero patient speech AND minimal criteria (<= 2)
    if hab_paciente_pct is not None and hab_paciente_pct <= 0 and substantive_eval_count <= 2:
        return False, "no_bilateral_conversation"

    # D. Intentos de contacto / otros / no_quiere_hablar with minimal criteria (<= 2)
    if t_key in ("intento_contacto", "otros", "no_quiere_hablar") and scored_items_count <= 1 and substantive_eval_count <= 2:
        return False, "insufficient_conversation"

    return True, "valid_conversation"
