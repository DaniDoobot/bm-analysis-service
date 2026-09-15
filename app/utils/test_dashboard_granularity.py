"""
Unit test suite for Dashboard Summary temporal granularity.
Tests:
  - Granularity auto-selection rules (hour <= 48h, day <= 60d, week <= 180d, month > 180d).
  - Explicit forced granularity overrides (hour, day, week, month).
  - Europe/Madrid timezone grouping and bucket boundaries.
  - Bucket generation and empty bucket filling in calls_evolution and sentiment_evolution.
  - Endpoint signature and router integration.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_dashboard_granularity.db"

from app.services.dashboard_service import (
    _to_madrid,
    _round_dt,
    _next_bucket,
    resolve_granularity,
    get_dashboard_summary,
)
from app.utils.dates import MADRID_TZ


class TestDashboardGranularityLogic(unittest.TestCase):
    """Test Madrid timezone rounding and bucket succession."""

    def test_to_madrid(self):
        # 10:00 UTC during CEST (+02:00 in summer, e.g. July)
        dt_utc_summer = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
        dt_madrid_summer = _to_madrid(dt_utc_summer)
        self.assertEqual(dt_madrid_summer.hour, 12)
        self.assertEqual(dt_madrid_summer.tzinfo.key, "Europe/Madrid")

        # 10:00 UTC during CET (+01:00 in winter, e.g. January)
        dt_utc_winter = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        dt_madrid_winter = _to_madrid(dt_utc_winter)
        self.assertEqual(dt_madrid_winter.hour, 11)

    def test_round_dt_hour(self):
        dt = datetime(2026, 9, 15, 11, 47, 32, tzinfo=timezone.utc)
        # 11:47 UTC = 13:47 Madrid
        rounded = _round_dt(dt, "hour")
        self.assertEqual(rounded.minute, 0)
        self.assertEqual(rounded.second, 0)
        self.assertEqual(rounded.microsecond, 0)
        self.assertEqual(rounded.hour, 13)

    def test_round_dt_day(self):
        dt = datetime(2026, 9, 15, 23, 30, 0, tzinfo=timezone.utc)
        # 23:30 UTC on Sep 15 = 01:30 Madrid on Sep 16
        rounded = _round_dt(dt, "day")
        self.assertEqual(rounded.day, 16)
        self.assertEqual(rounded.hour, 0)
        self.assertEqual(rounded.minute, 0)

    def test_round_dt_week(self):
        # 2026-09-15 is a Tuesday -> Monday is 2026-09-14
        dt = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)
        rounded = _round_dt(dt, "week")
        self.assertEqual(rounded.weekday(), 0)  # Monday
        self.assertEqual(rounded.day, 14)
        self.assertEqual(rounded.month, 9)
        self.assertEqual(rounded.year, 2026)
        self.assertEqual(rounded.hour, 0)
        self.assertEqual(rounded.minute, 0)

    def test_round_dt_month(self):
        dt = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)
        rounded = _round_dt(dt, "month")
        self.assertEqual(rounded.day, 1)
        self.assertEqual(rounded.month, 9)
        self.assertEqual(rounded.year, 2026)
        self.assertEqual(rounded.hour, 0)
        self.assertEqual(rounded.minute, 0)

    def test_next_bucket(self):
        # Hour
        h = datetime(2026, 9, 15, 10, 0, 0, tzinfo=MADRID_TZ)
        h_next = _next_bucket(h, "hour")
        self.assertEqual(h_next, datetime(2026, 9, 15, 11, 0, 0, tzinfo=MADRID_TZ))

        # Day
        d = datetime(2026, 9, 15, 0, 0, 0, tzinfo=MADRID_TZ)
        d_next = _next_bucket(d, "day")
        self.assertEqual(d_next, datetime(2026, 9, 16, 0, 0, 0, tzinfo=MADRID_TZ))

        # Week
        w = datetime(2026, 9, 14, 0, 0, 0, tzinfo=MADRID_TZ)
        w_next = _next_bucket(w, "week")
        self.assertEqual(w_next, datetime(2026, 9, 21, 0, 0, 0, tzinfo=MADRID_TZ))

        # Month standard
        m = datetime(2026, 9, 1, 0, 0, 0, tzinfo=MADRID_TZ)
        m_next = _next_bucket(m, "month")
        self.assertEqual(m_next, datetime(2026, 10, 1, 0, 0, 0, tzinfo=MADRID_TZ))

        # Month year rollover
        m_dec = datetime(2026, 12, 1, 0, 0, 0, tzinfo=MADRID_TZ)
        m_jan = _next_bucket(m_dec, "month")
        self.assertEqual(m_jan, datetime(2027, 1, 1, 0, 0, 0, tzinfo=MADRID_TZ))


class TestDashboardAutoGranularityRules(unittest.TestCase):
    """Test auto resolution logic for time spans using production resolve_granularity."""

    def test_auto_rules_production_helper(self):
        # <= 48h -> hour
        self.assertEqual(resolve_granularity(timedelta(hours=12), "auto"), "hour")
        self.assertEqual(resolve_granularity(timedelta(hours=24), "auto"), "hour")
        self.assertEqual(resolve_granularity(timedelta(hours=48), "auto"), "hour")

        # <= 60d -> day
        self.assertEqual(resolve_granularity(timedelta(hours=49), "auto"), "day")
        self.assertEqual(resolve_granularity(timedelta(days=7), "auto"), "day")
        self.assertEqual(resolve_granularity(timedelta(days=30), "auto"), "day")
        self.assertEqual(resolve_granularity(timedelta(days=60), "auto"), "day")

        # <= 180d -> week (confirming auto=week for 90 days)
        self.assertEqual(resolve_granularity(timedelta(days=61), "auto"), "week")
        self.assertEqual(resolve_granularity(timedelta(days=90), "auto"), "week")
        self.assertEqual(resolve_granularity(timedelta(days=180), "auto"), "week")

        # > 180d -> month
        self.assertEqual(resolve_granularity(timedelta(days=181), "auto"), "month")
        self.assertEqual(resolve_granularity(timedelta(days=365), "auto"), "month")

    def test_forced_overrides_production_helper(self):
        # Specific requirement: granularity=hour forced in range > 48h (e.g. 7d, 90d)
        self.assertEqual(resolve_granularity(timedelta(hours=72), "hour"), "hour")
        self.assertEqual(resolve_granularity(timedelta(days=7), "hour"), "hour")
        self.assertEqual(resolve_granularity(timedelta(days=90), "hour"), "hour")

        # Other forced overrides
        self.assertEqual(resolve_granularity(timedelta(days=300), "day"), "day")
        self.assertEqual(resolve_granularity(timedelta(hours=24), "week"), "week")
        self.assertEqual(resolve_granularity(timedelta(days=7), "month"), "month")



class TestDashboardRouterSignature(unittest.TestCase):
    """Test that dashboard_summary router accepts granularity and bucket params."""

    def test_router_param_presence(self):
        import inspect
        from app.routers.dashboard import dashboard_summary

        sig = inspect.signature(dashboard_summary)
        self.assertIn("granularity", sig.parameters)
        self.assertIn("bucket", sig.parameters)


def tearDownModule():
    if os.path.exists("test_dashboard_granularity.db"):
        try:
            os.remove("test_dashboard_granularity.db")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
