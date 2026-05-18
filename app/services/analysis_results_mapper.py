"""
Analysis results mapper and grouper.

Handles:
- Grouping AnalysisResult rows by criterion_type.
- Building a summary dict from an Analysis object.
- Converting raw values from AI output to typed columns (used during save).
"""
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.models.analyses import Analysis, AnalysisResult

logger = logging.getLogger(__name__)

CRITERION_TYPES = ["score_1_10", "percentage", "boolean", "text", "category", "number"]

# Boolean truthy/falsy strings
_TRUE_VALUES = {"si", "sí", "yes", "true", "1"}
_FALSE_VALUES = {"no", "false", "0"}


def make_json_safe(value: Any) -> Any:
    """
    Coerce a Python value to a JSON-serialisable type suitable for JSONB columns.

    Rules:
      - None               → None
      - Decimal            → float
      - datetime / date    → ISO-format string
      - dict               → recursively processed
      - list               → recursively processed
      - str, int, float, bool → returned as-is
      - anything else      → str(value)
    """
    if value is None:
        return None
    if isinstance(value, bool):          # must come before int (bool is a subclass of int)
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: make_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [make_json_safe(v) for v in value]
    # Fallback for any other type (sets, custom objects, …)
    return str(value)


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

    raw_value is stored as-is (JSON-safe) in the JSONB column — never coerced to str.
    The typed value_* columns are derived from it.
    """
    out: dict[str, Any] = {
        "value_number": None,
        "value_text": None,
        "value_boolean": None,
        "value_category": None,
        # Store the original value as JSON-safe — preserves int, float, bool, None, str, dict, list
        "raw_value": make_json_safe(raw_value),
    }

    if raw_value is None:
        return out

    if criterion_type in ("score_1_10", "percentage", "number"):
        try:
            out["value_number"] = float(raw_value)
        except (ValueError, TypeError):
            logger.warning(
                "Cannot convert %r to number for criterion_type=%s", raw_value, criterion_type
            )

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
        # else: unrecognised boolean value — leave both None, raw_value still stored

    return out
