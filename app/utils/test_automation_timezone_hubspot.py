"""
Tests for Automation and HubSpot Timezone Conversion Exactness.
===============================================================
Validates that UTC and Europe/Madrid datetimes convert to exact epoch milliseconds,
and that safe_parse_datetime handles ISO strings with timezones seamlessly.
"""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.utils.dates import safe_parse_datetime


class TestAutomationTimezoneHubSpot(unittest.TestCase):
    def test_utc_and_madrid_timestamp_parity(self):
        """Test that 10:29 CEST and 08:29 UTC produce the exact same epoch millisecond timestamp."""
        madrid_tz = ZoneInfo("Europe/Madrid")
        dt_madrid = datetime(2026, 8, 15, 10, 29, 33, 560000, tzinfo=madrid_tz)
        dt_utc = datetime(2026, 8, 15, 8, 29, 33, 560000, tzinfo=timezone.utc)

        ms_madrid = int(dt_madrid.timestamp() * 1000)
        ms_utc = int(dt_utc.timestamp() * 1000)

        self.assertEqual(ms_madrid, ms_utc)
        self.assertEqual(dt_madrid.astimezone(timezone.utc), dt_utc)

    def test_iso_string_safe_parsing_and_epoch_ms(self):
        """Test safe_parse_datetime with ISO format strings containing CEST offset (+02:00)."""
        iso_str = "2026-08-15T10:29:33.560142+02:00"
        parsed = safe_parse_datetime(iso_str)

        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo)

        expected_utc = datetime(2026, 8, 15, 8, 29, 33, 560142, tzinfo=timezone.utc)
        self.assertEqual(parsed.astimezone(timezone.utc), expected_utc)

        from_ms = int(parsed.timestamp() * 1000)
        expected_ms = int(expected_utc.timestamp() * 1000)
        self.assertEqual(from_ms, expected_ms)

    def test_naive_datetime_assigned_utc(self):
        """Test that naive datetime is safely treated as UTC by safe_parse_datetime."""
        naive_dt = datetime(2026, 8, 15, 8, 29, 33)
        parsed = safe_parse_datetime(naive_dt)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.hour, 8)
        self.assertEqual(parsed.minute, 29)


if __name__ == "__main__":
    unittest.main()
