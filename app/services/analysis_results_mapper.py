"""
Analysis results mapper and grouper.

Handles:
- Grouping AnalysisResult rows by criterion_type.
- Building a summary dict from an Analysis object.
- Converting raw values from AI output to typed columns (used during save).
"""
import logging
from typing import Any

from app.models.analyses import Analysis, AnalysisResult

logger = logging.getLogger(__name__)

CRITERION_TYPES = ["score_1_10", "percentage", "boolean", "text", "category", "number"]

# Boolean truthy/falsy strings
_TRUE_VALUES = {"si", "sí", "yes", "true", "1"}
_FALSE_VALUES = {"no", "false", "0"}


def group_results(results: list[AnalysisResult]) -> dict[str, list[AnalysisResult]]:
    """Group result rows by criterion_type."""
    grouped: dict[str, list] = {t: [] for t in CRITERION_TYPES}
    for r in results:
        key = r.criterion_type or "text"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(r)
    return grouped


def build_summary(analysis: Analysis, results: list[AnalysisResult]) -> dict[str, Any]:
    """Build a high-level summary dict for the detail response."""
    return {
        "analysis_id": analysis.analysis_id,
        "call_id": analysis.call_id,
        "analysis_type": analysis.analysis_type,
        "status": analysis.status,
        "agente_telefonico": analysis.agente_telefonico,
        "tipo_llamada": analysis.tipo_llamada,
        "evaluacion_global": analysis.evaluacion_global,
        "fecha_eval": analysis.fecha_eval,
        "model_provider": analysis.model_provider,
        "model_name": analysis.model_name,
        "prompt_id": analysis.prompt_id,
        "prompt_version_id": analysis.prompt_version_id,
        "total_results": len(results),
    }


def map_criterion_value(
    raw_value: Any,
    criterion_type: str,
) -> dict[str, Any]:
    """
    Convert a raw value from the AI JSON output into typed columns.

    Returns a dict with: value_number, value_text, value_boolean, value_category, raw_value.
    """
    raw_str = str(raw_value) if raw_value is not None else None

    out: dict[str, Any] = {
        "value_number": None,
        "value_text": None,
        "value_boolean": None,
        "value_category": None,
        "raw_value": raw_str,
    }

    if raw_value is None:
        return out

    if criterion_type in ("score_1_10", "percentage", "number"):
        try:
            out["value_number"] = float(raw_value)
        except (ValueError, TypeError):
            logger.warning("Cannot convert '%s' to number for type %s", raw_value, criterion_type)

    elif criterion_type == "text":
        out["value_text"] = str(raw_value)

    elif criterion_type == "category":
        out["value_category"] = str(raw_value)
        out["value_text"] = str(raw_value)

    elif criterion_type == "boolean":
        normalized = str(raw_value).strip().lower()
        if normalized in _TRUE_VALUES:
            out["value_boolean"] = True
            out["value_text"] = "Si"
        elif normalized in _FALSE_VALUES:
            out["value_boolean"] = False
            out["value_text"] = "No"
        # else: both remain None

    return out
