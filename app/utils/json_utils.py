"""JSON utilities."""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def safe_parse_json(raw: str) -> dict[str, Any] | None:
    """
    Parse a JSON string safely.
    If the string contains markdown fences (```json...```), strips them first.
    Returns None on parse failure.
    """
    if not raw:
        return None

    cleaned = raw.strip()

    # Strip markdown code fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last fence lines
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        cleaned = "\n".join(inner).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("JSON parse error: %s | Raw (first 300): %s", e, raw[:300])
        return None
