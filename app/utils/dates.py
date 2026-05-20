"""Date utilities."""
from datetime import date, datetime, timezone
from typing import Any


def now_utc() -> datetime:
    """Return current UTC datetime (always timezone-aware)."""
    return datetime.now(timezone.utc)


def safe_parse_datetime(value: Any) -> datetime | None:
    """
    Parse a timezone-aware datetime from multiple input formats.

    Accepted inputs:
      - None or ""                → None
      - datetime (aware)          → returned as-is
      - datetime (naive)          → assigned UTC timezone
      - int/float                 → treated as millisecond Unix timestamp
      - str "YYYY-MM-DD"          → midnight UTC (00:00:00+00:00)
      - str ISO 8601 with Z       → parsed with UTC offset
      - str ISO 8601 with offset  → parsed preserving offset
      - str of ms integer         → treated as millisecond Unix timestamp

    Returns None if parsing fails.
    The returned datetime is ALWAYS timezone-aware (never naive).
    """
    if value is None:
        return None

    # Already a datetime
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # naive → assume UTC
            return value.replace(tzinfo=timezone.utc)
        return value

    # date object (not datetime) → midnight UTC
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)

    # Timestamp (milliseconds or seconds)
    if isinstance(value, (int, float)):
        try:
            if value > 5000000000:
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            else:
                return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        # Handle "Z" suffix → valid ISO offset
        normalised = value.replace("Z", "+00:00")

        # Try full ISO 8601 (with or without offset after normalisation)
        try:
            dt = datetime.fromisoformat(normalised)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass

        # Try plain date "YYYY-MM-DD"
        try:
            d = date.fromisoformat(value[:10])
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            pass

        # Try millisecond or second string
        try:
            val_int = int(value)
            if val_int > 5000000000:
                return datetime.fromtimestamp(val_int / 1000, tz=timezone.utc)
            else:
                return datetime.fromtimestamp(val_int, tz=timezone.utc)
        except (ValueError, OSError):
            pass

        # Try RFC 2822 format (e.g. standard Twilio timestamps)
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass

    return None


def format_date_label(dt: datetime | None) -> str | None:
    """Return 'YYYY-MM-DD' string or None."""
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d")
