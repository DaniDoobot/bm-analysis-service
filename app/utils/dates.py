"""Date utilities."""
from datetime import datetime, timezone
from typing import Any


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def safe_parse_datetime(value: Any) -> datetime | None:
    """
    Parse a datetime from various formats (ISO string, ms timestamp, etc.).
    Returns None if parsing fails.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value

    # Try millisecond timestamp (HubSpot style)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    # Try ISO string
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            # Handle "Z" suffix
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        # Try millisecond string
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            pass

    return None


def format_date_label(dt: datetime | None) -> str | None:
    """Return 'YYYY-MM-DD' string or None."""
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d")
