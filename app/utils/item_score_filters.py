"""
Evaluation Item Score & Boolean Filters Utility Module.
=======================================================
Provides validation, parsing, SQL building, and in-memory filtering logic
for evaluation items/criteria queries across dashboards, analytics, and results.

Enforces:
- Max 3 active item filters per request.
- Strict AND evaluation logic across criteria.
- Support for numeric criteria (scores 0-10, percentages 0-100, numbers).
- Support for boolean criteria (true/false, si/no, is_true/is_false).
- Neutral filter detection and discarding (e.g. 0-10 numeric range).
- 422 HTTP exceptions for invalid JSON or invalid range parameters (min > max).
"""
import json
import logging
from typing import Any
from fastapi import HTTPException, status
from sqlalchemy import select, func, or_, and_, exists, literal
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Standard evaluative items catalog fallback
EVALUATION_ITEMS_FALLBACK = [
    {"key": "evaluacion_global", "label": "Evaluación Global", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 1},
    {"key": "empatia", "label": "Empatía", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 2},
    {"key": "claridad", "label": "Claridad", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 3},
    {"key": "procedimiento", "label": "Procedimiento", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 4},
    {"key": "cierre_cita", "label": "Cierre de Cita", "type": "boolean", "options": [{"label": "Sí", "value": True}, {"label": "No", "value": False}], "active": True, "sort_order": 5},
    {"key": "saludo_inicio", "label": "Saludo de Inicio", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 6},
    {"key": "n3_preguntas", "label": "N3 Preguntas", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 7},
    {"key": "despedida_con_refuerzo", "label": "Despedida con Refuerzo", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 8},
    {"key": "gestion_objeciones", "label": "Gestión de Objeciones", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 9},
    {"key": "uso_nombre_paciente", "label": "Uso del Nombre del Paciente", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 10},
    {"key": "uso_preguntas", "label": "Uso de Preguntas", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 11},
    {"key": "explicaciones_medicas", "label": "Explicaciones Médicas", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 12},
    {"key": "claridad_explicacion_economica", "label": "Claridad Explicación Económica", "type": "score", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 13},
    {"key": "conocimiento_boston_medical", "label": "Conocimiento previo Boston Medical", "type": "boolean", "options": [{"label": "Sí", "value": True}, {"label": "No", "value": False}], "active": True, "sort_order": 14},
    {"key": "duracion_consulta", "label": "Duración de Consulta", "type": "boolean", "options": [{"label": "Sí", "value": True}, {"label": "No", "value": False}], "active": True, "sort_order": 15},
    {"key": "direccion_y_referencias", "label": "Dirección y Referencias", "type": "boolean", "options": [{"label": "Sí", "value": True}, {"label": "No", "value": False}], "active": True, "sort_order": 16},
]


def _parse_bool_value(val: Any) -> bool | None:
    """Safely converts various boolean representations to True/False/None."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if val == 1:
            return True
        if val == 0:
            return False
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("true", "si", "sí", "1", "yes", "t", "s"):
            return True
        if v in ("false", "no", "0", "f", "n"):
            return False
    return None


def parse_item_score_filters_detailed(raw_param: str | list | dict | None) -> dict[str, Any]:
    """
    Parses and validates evaluation item score/boolean filters.
    Detects neutral numeric filters (0-10 range) and returns active filters.

    Returns:
      {
        "raw_count": int,
        "active_filters": list[dict[str, Any]],
        "discarded_neutral_count": int
      }
    Raises HTTP 422 if format, range or length rules (> 3 items) are violated.
    """
    if not raw_param:
        return {"raw_count": 0, "active_filters": [], "discarded_neutral_count": 0}

    data = raw_param
    if isinstance(raw_param, str):
        try:
            data = json.loads(raw_param)
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"Parámetro item_filters JSON inválido: {str(e)}"
            )

    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        raise HTTPException(
            status_code=422,
            detail="item_filters debe ser una lista de filtros por ítem."
        )

    raw_count = len(data)
    if raw_count > 3:
        raise HTTPException(
            status_code=422,
            detail="Se permite un máximo de 3 filtros por ítems de evaluación por consulta."
        )

    active_filters = []
    discarded_neutral_count = 0

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Filtro #{idx+1} debe ser un objeto JSON."
            )

        key = item.get("key") or item.get("criterion_key") or item.get("item_key") or item.get("criterion")
        if not key or not isinstance(key, str):
            raise HTTPException(
                status_code=422,
                detail=f"Filtro #{idx+1} requiere el campo 'key' (string)."
            )

        clean_key = key.strip().lower()
        operator = str(item.get("operator", "")).strip().lower() if item.get("operator") else None
        item_type = str(item.get("type", "")).strip().lower() if item.get("type") else None

        # -------------------------------------------------------------
        # 1. Check if this is a Boolean filter
        # -------------------------------------------------------------
        is_bool_filter = (
            item_type == "boolean"
            or operator in ("is_true", "is_false")
            or "boolean_value" in item
            or (isinstance(item.get("value"), bool) and "min" not in item and "max" not in item)
            or (isinstance(item.get("value"), str) and item.get("value", "").lower() in ("si", "no", "sí", "true", "false") and "min" not in item and "max" not in item)
        )

        if is_bool_filter:
            expected_bool = None
            if operator == "is_true":
                expected_bool = True
            elif operator == "is_false":
                expected_bool = False
            elif "boolean_value" in item:
                expected_bool = _parse_bool_value(item["boolean_value"])
            elif "value" in item:
                expected_bool = _parse_bool_value(item["value"])

            if expected_bool is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"Filtro booleano '{clean_key}': valor o condición booleana no válida."
                )

            active_filters.append({
                "key": clean_key,
                "type": "boolean",
                "operator": "eq",
                "expected_bool": expected_bool,
            })
            continue

        # -------------------------------------------------------------
        # 2. Otherwise treat as Numeric filter (score, number, percentage)
        # -------------------------------------------------------------
        min_val = item.get("min") if "min" in item else (item.get("min_score") if "min_score" in item else item.get("min_value"))
        max_val = item.get("max") if "max" in item else (item.get("max_score") if "max_score" in item else item.get("max_value"))

        if operator == "gte" and "value" in item:
            min_val = item["value"]
            max_val = max_val if max_val is not None else 10.0
        elif operator == "lte" and "value" in item:
            min_val = min_val if min_val is not None else 0.0
            max_val = item["value"]
        elif operator == "eq" and "value" in item and min_val is None and max_val is None:
            min_val = item["value"]
            max_val = item["value"]

        if min_val is None:
            min_val = 0.0
        if max_val is None:
            max_val = 10.0

        try:
            min_flt = float(min_val)
            max_flt = float(max_val)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail=f"Filtro '{clean_key}': 'min' y 'max' deben ser valores numéricos."
            )

        if min_flt > max_flt:
            raise HTTPException(
                status_code=422,
                detail=f"Filtro '{clean_key}': el valor mínimo ({min_flt}) no puede ser mayor que el máximo ({max_flt})."
            )

        # Detect neutral filter: range spans 0.0 to 10.0 (or wider) which matches all valid scores
        if min_flt <= 0.0 and max_flt >= 10.0 and item_type not in ("percentage", "number_100"):
            discarded_neutral_count += 1
            logger.info(
                "[item_score_filters] Discarded neutral filter for key='%s' min=%.1f max=%.1f",
                clean_key, min_flt, max_flt
            )
            continue

        active_filters.append({
            "key": clean_key,
            "type": item_type or "numeric",
            "operator": operator or "between",
            "min": min_flt,
            "max": max_flt
        })

    return {
        "raw_count": raw_count,
        "active_filters": active_filters,
        "discarded_neutral_count": discarded_neutral_count
    }


def parse_item_score_filters(raw_param: str | list | dict | None) -> list[dict[str, Any]]:
    """
    Parses and validates evaluation item score/boolean filters.
    Discards neutral filters and returns active filtering criteria.
    """
    parsed_info = parse_item_score_filters_detailed(raw_param)
    return parsed_info["active_filters"]


def build_item_filters_sql(item_filters: list[dict[str, Any]]) -> list[Any]:
    """
    Builds SQLAlchemy SQL WHERE expressions for MassEvaluationResult queries
    using EXISTS subqueries on MassEvaluationCriterionResult.
    """
    if not item_filters:
        return []

    from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult

    sql_conditions = []
    for filt in item_filters:
        key = filt["key"]
        filt_type = filt.get("type", "numeric")

        if filt_type == "boolean":
            expected = filt["expected_bool"]
            subq = select(literal(1)).select_from(MassEvaluationCriterionResult).where(
                MassEvaluationCriterionResult.mass_analysis_id == MassEvaluationResult.mass_analysis_id,
                MassEvaluationCriterionResult.criterion_key == key,
            )
            if expected is True:
                subq = subq.where(
                    or_(
                        MassEvaluationCriterionResult.boolean_value.is_(True),
                        func.lower(func.coalesce(MassEvaluationCriterionResult.text_value, "")).in_(["si", "sí", "true", "1"])
                    )
                )
            else:
                subq = subq.where(
                    or_(
                        MassEvaluationCriterionResult.boolean_value.is_(False),
                        func.lower(func.coalesce(MassEvaluationCriterionResult.text_value, "")).in_(["no", "false", "0"])
                    )
                )
            sql_conditions.append(exists(subq))
        else:
            min_val = filt["min"]
            max_val = filt["max"]
            subq = select(literal(1)).select_from(MassEvaluationCriterionResult).where(
                MassEvaluationCriterionResult.mass_analysis_id == MassEvaluationResult.mass_analysis_id,
                MassEvaluationCriterionResult.criterion_key == key,
                or_(
                    and_(
                        MassEvaluationCriterionResult.numeric_value.is_not(None),
                        MassEvaluationCriterionResult.numeric_value >= min_val,
                        MassEvaluationCriterionResult.numeric_value <= max_val
                    ),
                    and_(
                        MassEvaluationCriterionResult.percentage_value.is_not(None),
                        MassEvaluationCriterionResult.percentage_value >= min_val,
                        MassEvaluationCriterionResult.percentage_value <= max_val
                    )
                )
            )
            sql_conditions.append(exists(subq))

    return sql_conditions


def extract_boolean_from_mass(result_json: Any, items_json: Any, key: str) -> bool | None:
    """Extracts boolean evaluation value from items_json or result_json."""
    # 1. Check items_json list
    if items_json:
        items = items_json if isinstance(items_json, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_key = item.get("key") or item.get("criterion_key") or item.get("output_key")
            if item_key == key:
                if item.get("boolean_value") is not None:
                    return bool(item["boolean_value"])
                v = item.get("value") if "value" in item else (item.get("text_value") or item.get("raw_value"))
                parsed = _parse_bool_value(v)
                if parsed is not None:
                    return parsed

    # 2. Check result_json dict
    if result_json and isinstance(result_json, dict):
        v = result_json.get(key)
        if isinstance(v, dict):
            for sub_k in ["value", "valor", "boolean_value", "score"]:
                if sub_k in v:
                    parsed = _parse_bool_value(v[sub_k])
                    if parsed is not None:
                        return parsed
        parsed = _parse_bool_value(v)
        if parsed is not None:
            return parsed

    return None


def filter_mass_results_by_items(results: list[Any], item_filters: list[dict[str, Any]]) -> list[Any]:
    """
    Filters a list of MassEvaluationResult rows in memory using strict AND logic.
    Handles numeric score ranges and boolean criteria.
    """
    if not item_filters:
        return results

    from app.services.dashboard_service import extract_score_from_mass

    filtered = []
    for r in results:
        rj = getattr(r, "result_json", None)
        ij = getattr(r, "items_json", None)

        passes_all = True
        for filt in item_filters:
            k = filt["key"]
            filt_type = filt.get("type", "numeric")

            if filt_type == "boolean":
                actual_bool = extract_boolean_from_mass(rj, ij, k)
                if actual_bool is None or actual_bool != filt["expected_bool"]:
                    passes_all = False
                    break
            else:
                min_val = filt["min"]
                max_val = filt["max"]

                score = extract_score_from_mass(rj, ij, k)
                if score is None:
                    passes_all = False
                    break

                # Scale check: if score is > 10.0 on a 0-10 criterion, scale down
                normalized_score = score / 10.0 if (score > 10.0 and max_val <= 10.0) else score

                if not (min_val <= normalized_score <= max_val):
                    passes_all = False
                    break

        if passes_all:
            filtered.append(r)

    return filtered


def apply_item_score_filters_sql_or_python(
    results: list[Any],
    item_filters: list[dict[str, Any]]
) -> list[Any]:
    """
    Unified helper for in-memory item score and boolean filtering.
    """
    if not item_filters:
        return results
    return filter_mass_results_by_items(results, item_filters)


async def get_evaluation_item_filter_options(
    db: AsyncSession,
    company_ids: list[int] | None = None,
    service_ids: list[int] | None = None
) -> list[dict[str, Any]]:
    """
    Returns available evaluative criteria item filter options dynamically with types,
    min/max boundaries, and boolean option values for Lovable frontend.
    """
    from app.models.mass_evaluations import MassEvaluationCriterionResult

    stmt = select(
        MassEvaluationCriterionResult.criterion_key,
        MassEvaluationCriterionResult.criterion_name,
        MassEvaluationCriterionResult.criterion_type,
        MassEvaluationCriterionResult.service_id
    ).where(
        MassEvaluationCriterionResult.criterion_type.in_(["score_1_10", "score", "number", "boolean", "percentage"])
    )

    if service_ids is not None:
        stmt = stmt.where(MassEvaluationCriterionResult.service_id.in_(service_ids))

    stmt = stmt.group_by(
        MassEvaluationCriterionResult.criterion_key,
        MassEvaluationCriterionResult.criterion_name,
        MassEvaluationCriterionResult.criterion_type,
        MassEvaluationCriterionResult.service_id
    )

    options = []
    seen = set()
    try:
        res = await db.execute(stmt)
        rows = res.all()
        for idx, r in enumerate(rows):
            ckey, cname, ctype, sid = r
            if not ckey or ckey in seen:
                continue
            seen.add(ckey)
            label = cname or ckey.replace("_", " ").capitalize()

            norm_type = "score"
            if ctype == "boolean":
                norm_type = "boolean"
            elif ctype == "percentage":
                norm_type = "percentage"
            elif ctype == "number":
                norm_type = "number"

            item_dict: dict[str, Any] = {
                "key": ckey,
                "label": label,
                "type": norm_type,
                "service_id": sid,
                "active": True,
                "sort_order": idx + 1
            }

            if norm_type == "boolean":
                item_dict["options"] = [
                    {"label": "Sí", "value": True},
                    {"label": "No", "value": False}
                ]
            elif norm_type == "percentage":
                item_dict["min_score"] = 0.0
                item_dict["max_score"] = 100.0
            else:
                item_dict["min_score"] = 0.0
                item_dict["max_score"] = 10.0

            options.append(item_dict)
    except Exception as e:
        logger.warning("Error fetching dynamic criterion filter options: %s", e)

    # Combine with fallbacks if missing
    for fb in EVALUATION_ITEMS_FALLBACK:
        if fb["key"] not in seen:
            seen.add(fb["key"])
            fb_copy = dict(fb)
            fb_copy["sort_order"] = len(options) + 1
            options.append(fb_copy)

    return options
