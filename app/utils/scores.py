"""
Unified score calculation utilities.
"""
from decimal import Decimal
from typing import Any, List, Dict

# Standard set of evaluative criteria keys used for global score calculation
EVALUATIVE_CRITERIA_KEYS = {
    "empatia", "simpatia", "claridad", "procedimiento", "saludo_inicio",
    "n3_preguntas", "despedida_con_refuerzo", "gestion_objeciones",
    "uso_nombre_paciente", "uso_preguntas", "explicaciones_medicas",
    "claridad_explicacion_economica", "siguiente_paso"
}


def calculate_score_from_criterion_results(criterion_results: List[Any]) -> float | None:
    """
    Calculate global score from a list of AnalysisCriterionResult database models.
    Averages applicable evaluative criteria and rounds to 2 decimal places.
    """
    scores = []
    for cr in criterion_results:
        key = getattr(cr, "criterion_key", None)
        if key not in EVALUATIVE_CRITERIA_KEYS:
            continue
            
        # Check applicability
        is_applicable = getattr(cr, "is_applicable", True)
        not_applicable = getattr(cr, "not_applicable", False)
        if not is_applicable or not_applicable:
            continue
            
        # Extract numeric value
        val = None
        if getattr(cr, "numeric_value", None) is not None:
            val = float(cr.numeric_value)
        elif getattr(cr, "percentage_value", None) is not None:
            val = float(cr.percentage_value)
        elif getattr(cr, "boolean_value", None) is not None:
            val = 10.0 if cr.boolean_value else 0.0
            
        if val is not None:
            scores.append(val)
            
    if not scores:
        return None
        
    return round(sum(scores) / len(scores), 2)


def calculate_score_from_items(items: List[Dict[str, Any]]) -> float | None:
    """
    Calculate global score from a list of item dictionaries (used in MassEvaluationResult).
    Averages applicable evaluative criteria and rounds to 2 decimal places.
    """
    if not items:
        return None
        
    scores = []
    for item in items:
        key = item.get("criterion_key") or item.get("key") or item.get("output_key")
        if key not in EVALUATIVE_CRITERIA_KEYS:
            continue
            
        # Check applicability
        not_applicable = item.get("not_applicable", False)
        is_applicable = item.get("is_applicable", True)
        if not_applicable or not is_applicable:
            continue
            
        val = None
        if item.get("numeric_value") is not None:
            val = float(item["numeric_value"])
        elif item.get("percentage_value") is not None:
            val = float(item["percentage_value"])
        elif item.get("boolean_value") is not None:
            val = 10.0 if item["boolean_value"] else 0.0
        else:
            # Fallback checks on plain value field
            v = item.get("value")
            if isinstance(v, bool):
                val = 10.0 if v else 0.0
            elif isinstance(v, (int, float, Decimal)):
                val = float(v)
            elif isinstance(v, str):
                cleaned = v.strip().lower()
                if cleaned in ["si", "sí"]:
                    val = 10.0
                elif cleaned == "no":
                    val = 0.0
                    
        if val is not None:
            scores.append(val)
            
    if not scores:
        return None
        
    return round(sum(scores) / len(scores), 2)
