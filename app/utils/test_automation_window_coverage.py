"""
Test suite for continuous catch-up automation windows without coverage gaps.
===========================================================================
Verifies:
1. Window Chaining: Subsequent run starts exactly at previous completed run's window_to.
2. Slow Run Resilience: When a run takes 15 minutes, next window still starts at window_to without gaps.
3. Downtime / Redeploy Catch-up: Missed ticks create continuous sequential windows without losing time.
4. Already Running Lock: Blocked ticks do not advance or drop the pending window.
5. Initial Run Window: Starts cleanly from created_at or safe lookback.
6. Window Not Ready Guard: When window_to <= window_from, cleanly returns skipped reason='not_due_window_not_ready'.
"""
import os
import unittest
from datetime import datetime, timezone, timedelta

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///automation_window_coverage_test.db"

from sqlalchemy import BigInteger, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
    MassEvaluationRun,
)
from app.services.mass_evaluation_service import MassEvaluationService


class TestAutomationWindowCoverage(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            # Seed company, service, prompt, version, job
            db.add(Company(company_id=900, company_key="cont_co", company_name="Continuous Co"))
            await db.flush()
            db.add(Service(service_id=901, company_id=900, service_key="cont_svc", service_name="Continuous Svc"))
            await db.flush()
            db.add(Prompt(prompt_id=901, company_id=900, service_id=901, prompt_name="Continuous Prompt", prompt_type="mass"))
            await db.flush()
            db.add(PromptVersion(
                id=901,
                prompt_id=901,
                version_name="v1",
                version_label="v1.0",
                is_current=True,
                prompt="System prompt test"
            ))
            await db.flush()
            db.add(MassEvaluationJob(
                job_id=901,
                company_id=900,
                service_id=901,
                prompt_id=901,
                job_name="Continuous Base Job",
                is_active=True
            ))
            await db.flush()
            await db.commit()

    async def test_initial_window_derivation(self):
        """First execution uses created_at safely."""
        now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
        created_at = datetime(2026, 8, 15, 11, 30, 0, tzinfo=timezone.utc)

        async with AsyncSession(self.engine) as db:
            aut = MassAnalysisAutomation(
                automation_id=9001,
                name="Initial Auto",
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
                service_id=901,
                prompt_id=901,
                job_id=901,
                created_at=created_at,
            )
            db.add(aut)
            await db.commit()

            w_from, w_to, is_ready, source = await MassEvaluationService.get_automation_next_window(db, aut, now=now)
            self.assertTrue(is_ready)
            self.assertEqual(source, "initial_lookback")
            expected_start = created_at - timedelta(minutes=15)
            self.assertEqual(w_from, expected_start)
            # w_to = min(11:15 + 10m = 11:25, now - 5m = 11:55) -> 11:25
            self.assertEqual(w_to, expected_start + timedelta(minutes=10))

    async def test_window_chaining_no_gap(self):
        """Second run starts exactly where first completed run left off."""
        t0 = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)

        async with AsyncSession(self.engine) as db:
            aut = MassAnalysisAutomation(
                automation_id=9002,
                name="Chained Auto",
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
                service_id=901,
                prompt_id=901,
                job_id=901,
                created_at=t0,
            )
            db.add(aut)
            await db.flush()

            # Run 1 completed
            w1_from = t0
            w1_to = t0 + timedelta(minutes=10)
            r1 = MassAnalysisAutomationRun(
                automation_run_id=90021,
                automation_id=9002,
                status="completed",
                started_at=t0 + timedelta(minutes=15),
                finished_at=t0 + timedelta(minutes=16),
                window_from=w1_from,
                window_to=w1_to,
                calls_found=5,
                calls_selected=5,
            )
            db.add(r1)
            await db.commit()

            # At t0 + 20 min, calculate next window: now - delay = 10:20 - 5m = 10:15
            now_20 = t0 + timedelta(minutes=20)
            w2_from, w2_to, is_ready, source = await MassEvaluationService.get_automation_next_window(db, aut, now=now_20)
            self.assertTrue(is_ready)
            self.assertEqual(source, "continuous")
            # Gap closed: w2_from MUST equal w1_to (10:10)
            self.assertEqual(w2_from, w1_to)
            # w2_to is capped by now - delay = 10:15
            self.assertEqual(w2_to, t0 + timedelta(minutes=15))

            # At t0 + 25 min: now - delay = 10:20 >= 10:10 + 10m
            now_25 = t0 + timedelta(minutes=25)
            w2_from_25, w2_to_25, is_ready_25, source_25 = await MassEvaluationService.get_automation_next_window(db, aut, now=now_25)
            self.assertTrue(is_ready_25)
            self.assertEqual(source_25, "continuous")
            self.assertEqual(w2_from_25, w1_to)
            self.assertEqual(w2_to_25, w1_to + timedelta(minutes=10))

    async def test_slow_run_does_not_create_gap(self):
        """When a run takes 25 minutes to complete, next window does not skip time."""
        t0 = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)

        async with AsyncSession(self.engine) as db:
            aut = MassAnalysisAutomation(
                automation_id=9003,
                name="Slow Run Auto",
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
                service_id=901,
                prompt_id=901,
                job_id=901,
                created_at=t0,
            )
            db.add(aut)
            await db.flush()

            # Run 1 started at 10:15, finished at 10:40 (25 min long!)
            w1_from = t0
            w1_to = t0 + timedelta(minutes=10) # 10:00 to 10:10
            r1 = MassAnalysisAutomationRun(
                automation_run_id=90031,
                automation_id=9003,
                status="completed",
                started_at=t0 + timedelta(minutes=15),
                finished_at=t0 + timedelta(minutes=40),
                window_from=w1_from,
                window_to=w1_to,
            )
            db.add(r1)
            await db.commit()

            # Now is 10:41 (10:00 + 41m)
            now = t0 + timedelta(minutes=41)
            w2_from, w2_to, is_ready, source = await MassEvaluationService.get_automation_next_window(db, aut, now=now)
            self.assertTrue(is_ready)
            self.assertEqual(source, "continuous")
            # Under old code, w2_from would be now - 15m = 10:26 (gap of 16 minutes lost!)
            # Under new continuous code, w2_from MUST be 10:10 (0 minutes lost!)
            self.assertEqual(w2_from, datetime(2026, 8, 15, 10, 10, 0, tzinfo=timezone.utc))
            self.assertEqual(w2_to, datetime(2026, 8, 15, 10, 20, 0, tzinfo=timezone.utc))

    async def test_redeploy_downtime_catchup(self):
        """After 2 hours of downtime, runs sequential continuous windows."""
        t0 = datetime(2026, 8, 15, 8, 0, 0, tzinfo=timezone.utc)

        async with AsyncSession(self.engine) as db:
            aut = MassAnalysisAutomation(
                automation_id=9004,
                name="Catchup Auto",
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
                service_id=901,
                prompt_id=901,
                job_id=901,
                created_at=t0,
                last_run_at=t0 + timedelta(minutes=15)
            )
            db.add(aut)
            await db.flush()

            # Last completed run was 08:00 -> 08:10
            r1 = MassAnalysisAutomationRun(
                automation_run_id=90041,
                automation_id=9004,
                status="completed",
                started_at=t0 + timedelta(minutes=15),
                finished_at=t0 + timedelta(minutes=16),
                window_from=t0,
                window_to=t0 + timedelta(minutes=10),
            )
            db.add(r1)
            await db.commit()

            # 2 hours later at 10:15
            now = t0 + timedelta(hours=2, minutes=15)
            w2_from, w2_to, is_ready, _ = await MassEvaluationService.get_automation_next_window(db, aut, now=now)
            self.assertTrue(is_ready)
            self.assertEqual(w2_from, datetime(2026, 8, 15, 8, 10, 0, tzinfo=timezone.utc))
            self.assertEqual(w2_to, datetime(2026, 8, 15, 8, 20, 0, tzinfo=timezone.utc))

    async def test_already_running_does_not_lose_window(self):
        """A lock skip (already_running) does not lose the window start point."""
        t0 = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)

        async with AsyncSession(self.engine) as db:
            aut = MassAnalysisAutomation(
                automation_id=9005,
                name="Lock Auto",
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
                service_id=901,
                prompt_id=901,
                job_id=901,
                created_at=t0,
            )
            db.add(aut)
            await db.flush()

            r1 = MassAnalysisAutomationRun(
                automation_run_id=90051,
                automation_id=9005,
                status="completed",
                window_from=t0,
                window_to=t0 + timedelta(minutes=10),
            )
            # Run 2 is running
            r2 = MassAnalysisAutomationRun(
                automation_run_id=90052,
                automation_id=9005,
                status="running",
                started_at=t0 + timedelta(minutes=15),
                window_from=t0 + timedelta(minutes=10),
                window_to=t0 + timedelta(minutes=20),
            )
            db.add_all([r1, r2])
            await db.commit()

            # Next check while r2 is running: since r2 is running, last completed is still r1
            w_from, w_to, is_ready, _ = await MassEvaluationService.get_automation_next_window(db, aut, now=t0 + timedelta(minutes=25))
            # If r2 fails, next run will safely restart from r1's window_to (10:10)
            self.assertEqual(w_from, t0 + timedelta(minutes=10))

    async def test_window_not_ready_guard(self):
        """When window_to <= window_from, cleanly marks as not ready and returns skipped."""
        t0 = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)

        async with AsyncSession(self.engine) as db:
            aut = MassAnalysisAutomation(
                automation_id=9006,
                name="Not Ready Auto",
                is_active=True,
                interval_minutes=10,
                lookback_minutes=10,
                delay_minutes=5,
                service_id=901,
                prompt_id=901,
                job_id=901,
                created_at=t0,
            )
            db.add(aut)
            await db.flush()

            # Completed run already covered up to 10:00
            r1 = MassAnalysisAutomationRun(
                automation_run_id=90061,
                automation_id=9006,
                status="completed",
                window_from=t0 - timedelta(minutes=10),
                window_to=t0,
            )
            db.add(r1)
            await db.commit()

            # Now is 10:03. max_window_to = now - 5m = 09:58.
            # window_from is 10:00. window_to = min(10:10, 09:58) = 09:58 <= window_from.
            now = t0 + timedelta(minutes=3)
            w_from, w_to, is_ready, _ = await MassEvaluationService.get_automation_next_window(db, aut, now=now)
            self.assertFalse(is_ready)

            # Test run_automation_run returns skipped with not_due_window_not_ready
            auto_run = await MassEvaluationService.run_automation_run(db, aut, trigger_type="scheduled")
            self.assertEqual(auto_run.status, "skipped")
            self.assertEqual(auto_run.error_message, "not_due_window_not_ready")


if __name__ == "__main__":
    unittest.main()
