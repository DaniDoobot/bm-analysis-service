import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///automation_gap_dry_run_test.db"

import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base, _get_engine
get_settings.cache_clear()
_get_engine.cache_clear()

from app.models.mass_evaluations import (
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
    MassEvaluationJob,
)
from app.utils.backfill_automation_gaps import plan_gap_backfill
from app.utils.diagnose_automation_gaps import scan_automation_gaps

MADRID_TZ = ZoneInfo("Europe/Madrid")


class TestAutomationGapBackfillDryRun(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_detects_historical_gaps_correctly(self):
        """Test that gaps between non-continuous runs are accurately detected and quantified."""
        async with self.async_session() as db:
            job = MassEvaluationJob(
                job_id=48,
                prompt_id=58,
                job_name="[Auto] Front Test",
                selection_mode="filter",
                timezone="Europe/Madrid",
            )
            aut = MassAnalysisAutomation(
                automation_id=8,
                name="Front Auto",
                job_id=48,
                prompt_id=58,
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
            )
            now = datetime.now(timezone.utc)

            # Run 1: now - 60m to now - 50m
            r1 = MassAnalysisAutomationRun(
                automation_id=8,
                window_from=now - timedelta(minutes=60),
                window_to=now - timedelta(minutes=50),
                status="completed",
            )
            # Run 2: now - 35m to now - 25m (GAP of 15m between r1.to and r2.from)
            r2 = MassAnalysisAutomationRun(
                automation_id=8,
                window_from=now - timedelta(minutes=35),
                window_to=now - timedelta(minutes=25),
                status="completed",
            )
            # Run 3: now - 25m to now - 15m (CONTINUOUS with r2, 0m gap)
            r3 = MassAnalysisAutomationRun(
                automation_id=8,
                window_from=now - timedelta(minutes=25),
                window_to=now - timedelta(minutes=15),
                status="completed",
            )
            # Run 4: now - 5m to now + 5m (GAP of 10m between r3.to and r4.from)
            r4 = MassAnalysisAutomationRun(
                automation_id=8,
                window_from=now - timedelta(minutes=5),
                window_to=now + timedelta(minutes=5),
                status="completed",
            )

            db.add_all([job, aut, r1, r2, r3, r4])
            await db.commit()

            # Scan gaps
            scan_res = await scan_automation_gaps(db, automation_id=8, min_gap_seconds=60.0, days_back=1)
            self.assertEqual(scan_res["total_runs_analyzed"], 4)
            self.assertEqual(scan_res["total_gaps_count"], 2)
            self.assertAlmostEqual(scan_res["total_gap_minutes"], 25.0, places=1)

            # Plan dry run backfill
            plan_res = await plan_gap_backfill(db, automation_id=8, days_back=1, min_gap_minutes=1.0)
            self.assertEqual(plan_res["total_gaps_found"], 2)
            self.assertEqual(len(plan_res["planned_batches"]), 2)
            self.assertAlmostEqual(plan_res["planned_batches"][0]["gap_minutes"], 15.0, places=1)
            self.assertAlmostEqual(plan_res["planned_batches"][1]["gap_minutes"], 10.0, places=1)


if __name__ == "__main__":
    unittest.main()
