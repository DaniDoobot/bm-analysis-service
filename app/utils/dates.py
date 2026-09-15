"""Date utilities."""
from datetime import date, datetime, timedelta, timezone
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

        # Try European / Slash formats (e.g., "07/01/2026", "08/07/2026", "07-01-2026")
        for sep in ("/", "-"):
            if sep in value:
                parts = [p.strip() for p in value.split(sep) if p.strip()]
                if len(parts) == 3:
                    try:
                        # Check if last part is 4-digit year (DD/MM/YYYY)
                        if len(parts[2]) == 4:
                            y, m, d = int(parts[2]), int(parts[1]), int(parts[0])
                        # Check if first part is 4-digit year (YYYY/MM/DD)
                        elif len(parts[0]) == 4:
                            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                        else:
                            continue
                        if 1 <= m <= 12 and 1 <= d <= 31:
                            return datetime(y, m, d, tzinfo=timezone.utc)
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


import zoneinfo
MADRID_TZ = zoneinfo.ZoneInfo("Europe/Madrid")


def parse_madrid_date_bounds(
    date_from: Any = None,
    date_to: Any = None,
    period: str | None = None
) -> tuple[datetime | None, datetime | None]:
    """
    Parses start and end date parameters with Europe/Madrid local time bounds.
    Converts naive dates or dates without explicit offset into Europe/Madrid calendar day bounds
    (00:00:00.000000 for start, 23:59:59.999999 for end) and returns timezone-aware UTC datetimes.

    Supports period shortcuts ('24h', '7d', '30d', '90d') when date_from is None.
    """
    now_utc = datetime.now(timezone.utc)
    dt_from_utc: datetime | None = None
    dt_to_utc: datetime | None = None

    if period and not date_from and not date_to:
        p = period.lower().strip()
        if p == "24h":
            dt_from_utc = now_utc - timedelta(hours=24)
            dt_to_utc = now_utc
        elif p == "7d":
            dt_from_utc = now_utc - timedelta(days=7)
            dt_to_utc = now_utc
        elif p == "30d":
            dt_from_utc = now_utc - timedelta(days=30)
            dt_to_utc = now_utc
        elif p == "90d":
            dt_from_utc = now_utc - timedelta(days=90)
            dt_to_utc = now_utc

    if date_from:
        parsed_f = safe_parse_datetime(date_from)
        if parsed_f:
            raw_str = str(date_from).strip() if isinstance(date_from, str) else ""
            if raw_str and ("+" in raw_str or "Z" in raw_str or (len(raw_str) > 10 and "T" in raw_str and ":" in raw_str and "00:00:00" not in raw_str)):
                dt_from_utc = parsed_f.astimezone(timezone.utc)
            else:
                # Bare date or midnight -> interpret as 00:00:00 in Europe/Madrid -> convert to UTC
                dt_madrid = datetime(parsed_f.year, parsed_f.month, parsed_f.day, 0, 0, 0, tzinfo=MADRID_TZ)
                dt_from_utc = dt_madrid.astimezone(timezone.utc)

    if date_to:
        parsed_t = safe_parse_datetime(date_to)
        if parsed_t:
            raw_str = str(date_to).strip() if isinstance(date_to, str) else ""
            if raw_str and ("+" in raw_str or "Z" in raw_str or (len(raw_str) > 10 and "T" in raw_str and ":" in raw_str and "00:00:00" not in raw_str)):
                dt_to_utc = parsed_t.astimezone(timezone.utc)
            else:
                # Bare date or midnight -> interpret as 23:59:59.999999 in Europe/Madrid -> convert to UTC
                dt_madrid = datetime(parsed_t.year, parsed_t.month, parsed_t.day, 23, 59, 59, 999999, tzinfo=MADRID_TZ)
                dt_to_utc = dt_madrid.astimezone(timezone.utc)

    return dt_from_utc, dt_to_utc


def to_madrid_dt(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware and converted to Europe/Madrid."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MADRID_TZ)


def round_to_bucket(dt: datetime, interval: str) -> datetime:
    """Round a datetime to the bucket start in Europe/Madrid timezone."""
    dt_m = to_madrid_dt(dt)
    if interval == "hour":
        return dt_m.replace(minute=0, second=0, microsecond=0)
    elif interval == "week":
        monday = dt_m - timedelta(days=dt_m.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    elif interval == "month":
        return dt_m.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # day
        return dt_m.replace(hour=0, minute=0, second=0, microsecond=0)


def next_bucket(dt_bucket: datetime, interval: str) -> datetime:
    """Advance a bucket start datetime to the next bucket boundary."""
    if interval == "hour":
        return dt_bucket + timedelta(hours=1)
    elif interval == "week":
        return dt_bucket + timedelta(weeks=1)
    elif interval == "month":
        y = dt_bucket.year + (1 if dt_bucket.month == 12 else 0)
        m = 1 if dt_bucket.month == 12 else dt_bucket.month + 1
        return dt_bucket.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # day
        return dt_bucket + timedelta(days=1)


def format_bucket_key(dt: datetime, interval: str) -> str:
    """Format datetime into standard string key according to granularity in Europe/Madrid."""
    dt_m = to_madrid_dt(dt)
    if interval == "hour":
        return dt_m.strftime("%Y-%m-%d %H:00")
    elif interval == "month":
        return dt_m.strftime("%Y-%m-01")
    elif interval == "week":
        monday = dt_m - timedelta(days=dt_m.weekday())
        return monday.strftime("%Y-%m-%d")
    else:  # day
        return dt_m.strftime("%Y-%m-%d")


def resolve_granularity(span: timedelta, requested_granularity: str | None = "auto") -> str:
    """
    Resolves the effective bucket granularity according to the product contract:
    auto:
      <= 48h  -> 'hour'
      <= 60d  -> 'day'
      <= 180d -> 'week'
      > 180d  -> 'month'
    Explicit override: 'hour' | 'day' | 'week' | 'month' (case-insensitive).
    """
    norm = (requested_granularity or "auto").strip().lower()
    if norm in ("hour", "day", "week", "month"):
        return norm

    if span <= timedelta(hours=48):
        return "hour"
    elif span <= timedelta(days=60):
        return "day"
    elif span <= timedelta(days=180):
        return "week"
    else:
        return "month"
