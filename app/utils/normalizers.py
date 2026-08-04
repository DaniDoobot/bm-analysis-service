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

