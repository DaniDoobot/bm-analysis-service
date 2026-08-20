"""Normalizer utilities for analysis values."""


def normalize_boolean_value(value) -> tuple[bool | None, str | None]:
    """
    Normalize a boolean value from AI output.
    Returns (value_boolean, value_text).
    """
    if value is None:
        return None, None

    normalized = str(value).strip().lower()

    TRUE_VALS = {"si", "sí", "yes", "true", "1"}
    FALSE_VALS = {"no", "false", "0"}

    if normalized in TRUE_VALS:
        return True, "Si"
    if normalized in FALSE_VALS:
        return False, "No"
    return None, None


def normalize_number(value) -> float | None:
    """Convert a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def normalize_text(value) -> str | None:
    """Convert value to string, None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


import re
import unicodedata
from fastapi import HTTPException, status


def normalize_typology(typology_raw: str | None) -> str | None:
    """
    Normalize typology raw string parameter.
    Returns normalized key string (e.g. 'falta', 'intento_contacto', 'transferencia', 'cita') or None.
    Handles 'todos', 'all', '', None -> None.
    """
    if not typology_raw:
        return None
    s = str(typology_raw).strip()
    if not s or s.lower() in ("todos", "todas", "all", "none", "null"):
        return None

    # Lowercase & strip accents
    s_lower = s.lower()
    nfkd_form = unicodedata.normalize('NFKD', s_lower)
    s_no_accents = "".join([c for c in nfkd_form if not unicodedata.combining(c)])

    # Replace non-alphanumeric chars with underscore
    normalized = re.sub(r'[^a-z0-9]+', '_', s_no_accents).strip('_')
    return normalized if normalized else None


def normalize_direction(direction_raw: str | None) -> str | None:
    """
    Normalize direction raw parameter ('inbound', 'outbound', 'all').
    Raises HTTPException(422) if invalid value is provided.
    Returns 'inbound', 'outbound', or None if 'all'/empty.
    """
    if not direction_raw:
        return None
    s = str(direction_raw).strip().lower()
    if s in ("all", "todas", "todos", "none", "null", ""):
        return None

    INBOUND_VALS = {"inbound", "entrante", "in", "inbound_call"}
    OUTBOUND_VALS = {"outbound", "saliente", "out", "outbound_call"}

    if s in INBOUND_VALS:
        return "inbound"
    if s in OUTBOUND_VALS:
        return "outbound"

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Valor de dirección no válido '{direction_raw}'. Valores aceptados: all, inbound, outbound, entrante, saliente."
    )


def normalize_status(status_raw: str | None) -> str | None:
    """
    Normalize status raw parameter ('completed', 'failed', 'all').
    Raises HTTPException(422) if invalid value is provided.
    Returns 'completed', 'failed', 'all', or None if omitted/empty.
    """
    if status_raw is None:
        return None
    s = str(status_raw).strip().lower()
    if not s:
        return None
    if s in ("all", "todas", "todos", "none", "null", "*"):
        return "all"
    if s in ("completed", "completado", "ok", "success"):
        return "completed"
    if s in ("failed", "fallido", "error"):
        return "failed"

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Valor de estado no válido '{status_raw}'. Valores aceptados: completed, failed, all."
    )


VALID_SORT_FIELDS = {
    "date": "date",
    "call_timestamp": "date",
    "agent": "agent",
    "agent_name": "agent",
    "call_id": "call_id",
    "duration": "duration",
    "call_duration_seconds": "duration",
    "score": "score",
    "evaluacion_global": "score",
    "global_score": "score",
    "typology": "typology",
    "typology_name": "typology",
    "direction": "direction",
    "status": "status",
    "service": "service",
    "service_name": "service",
    "execution_source": "execution_source",
    "source": "execution_source",
}


def normalize_sort(sort_by_raw: str | None, sort_order_raw: str | None = None) -> tuple[str | None, str]:
    """
    Validate and normalize sort_by and sort_order parameters.
    Returns (canonical_sort_by, canonical_sort_order).
    Raises HTTPException(422) if invalid field or order is provided.
    """
    canonical_field = None
    if sort_by_raw is not None:
        s_by = str(sort_by_raw).strip().lower()
        if s_by:
            if s_by not in VALID_SORT_FIELDS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Campo de ordenación no válido '{sort_by_raw}'. Valores aceptados: date, agent, call_id, duration, score, typology, direction, status, service, execution_source."
                )
            canonical_field = VALID_SORT_FIELDS[s_by]

    canonical_order = "desc"
    if sort_order_raw is not None:
        s_ord = str(sort_order_raw).strip().lower()
        if s_ord:
            if s_ord not in ("asc", "desc"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Dirección de ordenación no válida '{sort_order_raw}'. Valores aceptados: asc, desc."
                )
            canonical_order = s_ord

    return canonical_field, canonical_order

