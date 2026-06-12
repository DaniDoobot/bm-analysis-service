"""JSON utilities."""
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _try_extract_json_object(text: str) -> dict[str, Any] | None:
    """
    Attempt to extract the first complete JSON object from a string using
    a brace-depth scanner. Useful when the model prefixes or suffixes the JSON
    with prose text.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        return None
    return None


def safe_parse_json(raw: str) -> dict[str, Any] | None:
    """
    Parse a JSON string safely with several fallback strategies:
    1. Direct parse after stripping whitespace.
    2. Strip markdown code fences (```json...```) and retry.
    3. Attempt brace-depth extraction of the first JSON object.
    Returns None only when all strategies fail.
    """
    if not raw:
        return None

    cleaned = raw.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown code fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        cleaned = "\n".join(inner).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    # Strategy 3: brace-depth extraction from original raw text
    result = _try_extract_json_object(raw)
    if result is not None:
        logger.warning("JSON parsed via brace-depth extraction (model returned non-pure JSON).")
        return result

    logger.error("JSON parse error (all strategies failed) | Raw (first 300): %s", raw[:300])
    return None
