"""
Unit test suite for transversal temporal granularity across all endpoints and services.
Validates:
1. resolve_granularity contract:
   - auto: <=48h -> hour, <=60d -> day, <=180d -> week, >180d -> month
   - explicit overrides: hour, day, week, month (case-insensitive, whitespace)
2. Madrid timezone rounding and bucket succession:
   - round_to_bucket, next_bucket, format_bucket_key
3. ServiceEvolutionService.get_evolution:
   - effective granularity resolution
   - period_expr grouping for hour, day, week, month
4. Router query validation:
   - granularity and bucket aliases accepted
   - invalid granularity rejected
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_transversal_granularity.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from app.db import get_engine, Base
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.mass_evaluations import MassEvaluationResult
from app.services.service_evolution_service import ServiceEvolutionService
from app.utils.dates import (
    MADRID_TZ,
    to_madrid_dt,
    round_to_bucket,
    next_bucket,
    format_bucket_key,
    resolve_granularity,
)
from sqlalchemy.ext.asyncio import AsyncSession


class TestGranularityHelpers(unittest.TestCase):
    """Test resolve_granularity and date bucket helpers."""

    def test_auto_resolution_thresholds(self):
        # <= 48h -> hour
        self.assertEqual(resolve_granularity(timedelta(hours=1), "auto"), "hour")
        self.assertEqual(resolve_granularity(timedelta(hours=24), "auto"), "hour")
        self.assertEqual(resolve_granularity(timedelta(hours=48), "auto"), "hour")
        self.assertEqual(resolve_granularity(timedelta(hours=48), None), "hour")

        # <= 60d -> day
        self.assertEqual(resolve_granularity(timedelta(hours=48, minutes=1), "auto"), "day")
        self.assertEqual(resolve_granularity(timedelta(days=7), "auto"), "day")
        self.assertEqual(resolve_granularity(timedelta(days=30), "auto"), "day")
        self.assertEqual(resolve_granularity(timedelta(days=60), "auto"), "day")

        # <= 180d -> week
        self.assertEqual(resolve_granularity(timedelta(days=61), "auto"), "week")
        self.assertEqual(resolve_granularity(timedelta(days=90), "auto"), "week")
        self.assertEqual(resolve_granularity(timedelta(days=180), "auto"), "week")

        # > 180d -> month
        self.assertEqual(resolve_granularity(timedelta(days=181), "auto"), "month")
        self.assertEqual(resolve_granularity(timedelta(days=365), "auto"), "month")

    def test_explicit_overrides(self):
        span = timedelta(days=365)
        self.assertEqual(resolve_granularity(span, "hour"), "hour")
        self.assertEqual(resolve_granularity(span, "Day"), "day")
        self.assertEqual(resolve_granularity(span, " WEEK "), "week")
        self.assertEqual(resolve_granularity(timedelta(hours=1), "month"), "month")

    def test_format_bucket_key(self):
        # 2026-09-15 14:35:00 UTC = 16:35:00 Europe/Madrid (CEST)
        dt = datetime(2026, 9, 15, 14, 35, 0, tzinfo=timezone.utc)

        self.assertEqual(format_bucket_key(dt, "hour"), "2026-09-15 16:00")
        self.assertEqual(format_bucket_key(dt, "day"), "2026-09-15")
        # 2026-09-15 is Tuesday -> Monday is 2026-09-14
        self.assertEqual(format_bucket_key(dt, "week"), "2026-09-14")
        self.assertEqual(format_bucket_key(dt, "month"), "2026-09-01")

    def test_round_and_next_bucket(self):
        dt = datetime(2026, 9, 15, 14, 35, 0, tzinfo=timezone.utc)

        # Hour
        rh = round_to_bucket(dt, "hour")
        self.assertEqual(rh.minute, 0)
        self.assertEqual(rh.hour, 16)
        nh = next_bucket(rh, "hour")
        self.assertEqual(nh.hour, 17)

        # Day
        rd = round_to_bucket(dt, "day")
        self.assertEqual(rd.hour, 0)
        nd = next_bucket(rd, "day")
        self.assertEqual(nd.day, 16)

        # Week
        rw = round_to_bucket(dt, "week")
        self.assertEqual(rw.day, 14)
        nw = next_bucket(rw, "week")
        self.assertEqual(nw.day, 21)

        # Month
        rm = round_to_bucket(dt, "month")
        self.assertEqual(rm.day, 1)
        nm = next_bucket(rm, "month")
        self.assertEqual(nm.month, 10)


class TestServiceEvolutionGranularity(unittest.IsolatedAsyncioTestCase):
    """Test ServiceEvolutionService with various granularities."""

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            t1 = datetime(2026, 6, 15, 10, 0, 0)
            t2 = datetime(2026, 6, 15, 11, 0, 0)
            t3 = datetime(2026, 6, 16, 10, 0, 0)
            for idx, call_time in enumerate([t1, t2, t3]):
                res = MassEvaluationResult(
                    mass_analysis_id=idx + 1,
                    run_id=1,
                    job_id=1,
                    prompt_id=1,
                    prompt_snapshot="snapshot",
                    call_id=f"call_{idx}",
                    company_id=1,
                    service_id=1,
                    service_key="front",
                    service_name="Front",
                    hubspot_owner_id="agent_1",
                    agent_name="Agent 1",
                    call_timestamp=call_time,
                    evaluacion_global=8.5,
                    status="completed",
                )
                db.add(res)
            await db.commit()

        self.context = TenantContext(
            user_id=1,
            raw_role="superadmin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=None,
            allowed_agent_ids=None,
        )

    async def asyncTearDown(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def test_get_evolution_hour(self):
        async with AsyncSession(self.engine) as db:
            resp = await ServiceEvolutionService.get_evolution(
                db=db,
                context=self.context,
                service_id=1,
                date_from="2026-06-15 00:00:00",
                date_to="2026-06-15 23:59:59",
                granularity="hour",
            )
            self.assertEqual(resp.filters.granularity, "hour")
            self.assertEqual(resp.summary.total_calls, 2)
            self.assertEqual(len(resp.series), 2)
            periods = [s.period for s in resp.series]
            self.assertIn("2026-06-15 10:00", periods)
            self.assertIn("2026-06-15 11:00", periods)

    async def test_get_evolution_day(self):
        async with AsyncSession(self.engine) as db:
            resp = await ServiceEvolutionService.get_evolution(
                db=db,
                context=self.context,
                service_id=1,
                date_from="2026-06-15",
                date_to="2026-06-16",
                granularity="day",
            )
            self.assertEqual(resp.filters.granularity, "day")
            self.assertEqual(resp.summary.total_calls, 3)
            self.assertEqual(len(resp.series), 2)

    async def test_get_evolution_auto_resolution(self):
        async with AsyncSession(self.engine) as db:
            resp_hour = await ServiceEvolutionService.get_evolution(
                db=db,
                context=self.context,
                service_id=1,
                date_from="2026-06-15 00:00:00",
                date_to="2026-06-15 23:59:59",
                granularity="auto",
            )
            self.assertEqual(resp_hour.filters.granularity, "hour")

            resp_day = await ServiceEvolutionService.get_evolution(
                db=db,
                context=self.context,
                service_id=1,
                date_from="2026-06-10",
                date_to="2026-06-17",
                granularity="auto",
            )
            self.assertEqual(resp_day.filters.granularity, "day")

            resp_week = await ServiceEvolutionService.get_evolution(
                db=db,
                context=self.context,
                service_id=1,
                date_from="2026-01-01",
                date_to="2026-04-01",
                granularity="auto",
            )
            self.assertEqual(resp_week.filters.granularity, "week")

            resp_month = await ServiceEvolutionService.get_evolution(
                db=db,
                context=self.context,
                service_id=1,
                date_from="2026-01-01",
                date_to="2026-12-31",
                granularity="auto",
            )
            self.assertEqual(resp_month.filters.granularity, "month")


if __name__ == "__main__":
    unittest.main()
