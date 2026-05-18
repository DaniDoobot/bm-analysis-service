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
