"""
Evaluation Item Score Filters Utility Module.
=============================================
Provides validation, parsing, and filtering logic for item score queries.
Enforces:
- Max 3 item filters per request.
- Strict AND evaluation logic across criteria.
- Valid numeric score ranges (min <= max).
- 422 HTTP exceptions for invalid JSON or range parameters.
"""
import json
import logging
from typing import Any
from fastapi import HTTPException, status
from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dashboard_service import extract_score_from_mass

logger = logging.getLogger(__name__)

# Standard evaluative items catalog fallback
EVALUATION_ITEMS_FALLBACK = [
    {"key": "evaluacion_global", "label": "Evaluación Global", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 1},
    {"key": "empatia", "label": "Empatía", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 2},
    {"key": "claridad", "label": "Claridad", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 3},
    {"key": "procedimiento", "label": "Procedimiento", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 4},
    {"key": "saludo_inicio", "label": "Saludo de Inicio", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 5},
    {"key": "n3_preguntas", "label": "N3 Preguntas", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 6},
    {"key": "despedida_con_refuerzo", "label": "Despedida con Refuerzo", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 7},
    {"key": "gestion_objeciones", "label": "Gestión de Objeciones", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 8},
    {"key": "uso_nombre_paciente", "label": "Uso del Nombre del Paciente", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 9},
    {"key": "uso_preguntas", "label": "Uso de Preguntas", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 10},
    {"key": "explicaciones_medicas", "label": "Explicaciones Médicas", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 11},
    {"key": "claridad_explicacion_economica", "label": "Claridad Explicación Económica", "min_score": 0.0, "max_score": 10.0, "active": True, "sort_order": 12},
]


def parse_item_score_filters(raw_param: str | list | dict | None) -> list[dict[str, Any]]:
    """
    Parses and validates evaluation item score filters.
    
    Accepts:
      - JSON string: '[{"key": "empatia", "min": 1, "max": 5}]'
      - List of dicts or single dict
      
    Returns list of dicts: [{"key": str, "min": float, "max": float}]
    Raises HTTP 422 if format, range or length rules (> 3 items) are violated.
    """
    if not raw_param:
        return []

    data = raw_param
    if isinstance(raw_param, str):
        try:
            data = json.loads(raw_param)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Parámetro item_filters JSON inválido: {str(e)}"
            )

    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="item_filters debe ser una lista de filtros por ítem."
        )

    if len(data) > 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Se permite un máximo de 3 filtros por ítems de evaluación por consulta."
        )

    parsed_filters = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Filtro #{idx+1} debe ser un objeto JSON."
            )

        key = item.get("key") or item.get("criterion_key") or item.get("item_key")
        if not key or not isinstance(key, str):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Filtro #{idx+1} requiere el campo 'key' (string)."
            )

        min_val = item.get("min") if "min" in item else item.get("min_score")
        max_val = item.get("max") if "max" in item else item.get("max_score")

        if min_val is None:
            min_val = 0.0
        if max_val is None:
            max_val = 10.0

        try:
            min_flt = float(min_val)
            max_flt = float(max_val)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Filtro '{key}': 'min' y 'max' deben ser valores numéricos."
            )

        if min_flt > max_flt:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Filtro '{key}': el valor mínimo ({min_flt}) no puede ser mayor que el máximo ({max_flt})."
            )

        parsed_filters.append({
            "key": key.strip().lower(),
            "min": min_flt,
            "max": max_flt
        })

    return parsed_filters


def filter_mass_results_by_items(results: list[Any], item_filters: list[dict[str, Any]]) -> list[Any]:
    """
    Filters a list of MassEvaluationResult rows in memory using strict AND logic.
    Each item in item_filters must be satisfied by the conversation result.
    """
    if not item_filters:
        return results

    filtered = []
    for r in results:
        rj = getattr(r, "result_json", None)
        ij = getattr(r, "items_json", None)

        passes_all = True
        for filt in item_filters:
            k = filt["key"]
            min_val = filt["min"]
            max_val = filt["max"]

            score = extract_score_from_mass(rj, ij, k)
            if score is None:
                passes_all = False
                break

            # Scale check: if score is > 10.0 (e.g. 0-100 scale), scale down to 0-10
            normalized_score = score / 10.0 if score > 10.0 else score

            if not (min_val <= normalized_score <= max_val):
                passes_all = False
                break

        if passes_all:
            filtered.append(r)

    return filtered


async def get_evaluation_item_filter_options(
    db: AsyncSession,
    company_ids: list[int] | None = None,
    service_ids: list[int] | None = None
) -> list[dict[str, Any]]:
    """
    Returns available evaluative criteria item filter options dynamically,
    falling back to standard defaults.
    """
    from app.models.mass_evaluations import MassEvaluationCriterionResult
    
    stmt = select(
        MassEvaluationCriterionResult.criterion_key,
        MassEvaluationCriterionResult.criterion_name,
        MassEvaluationCriterionResult.service_id
    ).where(
        MassEvaluationCriterionResult.criterion_type.in_(["score_1_10", "score", "number", "boolean"])
    )

    if service_ids is not None:
        stmt = stmt.where(MassEvaluationCriterionResult.service_id.in_(service_ids))

    stmt = stmt.group_by(
        MassEvaluationCriterionResult.criterion_key,
        MassEvaluationCriterionResult.criterion_name,
        MassEvaluationCriterionResult.service_id
    )

    options = []
    seen = set()
    try:
        res = await db.execute(stmt)
        rows = res.all()
        for idx, r in enumerate(rows):
            ckey, cname, sid = r
            if not ckey or ckey in seen:
                continue
            seen.add(ckey)
            label = cname or ckey.replace("_", " ").capitalize()
            options.append({
                "key": ckey,
                "label": label,
                "min_score": 0.0,
                "max_score": 10.0,
                "service_id": sid,
                "active": True,
                "sort_order": idx + 1
            })
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
