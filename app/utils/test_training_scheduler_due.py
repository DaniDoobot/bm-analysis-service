"""
Regression test for MissingGreenlet in PersonalizedTrainingService.run_due_training_jobs.
Validates that:
1. expire_on_commit=True expires the ORM object upon db.commit().
2. run_due_training_jobs captures run_id and run_status BEFORE commit.
3. No MissingGreenlet exception is raised.
4. Returns correct dictionary {"triggered": True, "run_id": ..., "reason": ...}.
5. db_settings.last_status is set to "completed" (not "failed").
6. next_run_at is updated.
"""
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///training_scheduler_due_test.db"
os.environ["APP_ENV"] = "test"

import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, attributes
from sqlalchemy import select, BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base
from app.models.personalized_training import (
    TrainingRun,
    TrainingSchedulerSetting,
)
from app.services.personalized_training_service import PersonalizedTrainingService


class TestTrainingSchedulerDue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["APP_ENV"] = "test"
        from app.config import get_settings
        settings = get_settings()
        settings.enable_training_scheduler = True

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Explicitly enforce expire_on_commit=True to mirror production SQLAlchemy session lifecycle
        self.session_factory = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=True,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_run_due_training_jobs_success_no_missing_greenlet(self):
        """
        Tests that when a training run is due, the scheduler commits db_settings
        and returns the run_id without triggering sqlalchemy.exc.MissingGreenlet
        from accessing expired ORM attributes.
        """
        now = datetime.now(timezone.utc)

        async with self.session_factory() as db:
            # 1. Setup scheduler settings: due now
            db_settings = TrainingSchedulerSetting(
                is_enabled=True,
                interval_days=14,
                lookback_days=14,
                last_run_at=now - timedelta(days=15),
                next_run_at=now - timedelta(hours=1),
                last_status="completed",
            )
            db.add(db_settings)

            captured_run = []

            async def fake_run_pass(*args, **kwargs):
                mock_run = TrainingRun(
                    training_run_id=101,
                    period_start=now - timedelta(days=14),
                    period_end=now,
                    status="completed",
                    triggered_by="scheduler",
                    started_at=now,
                    finished_at=now,
                    agents_total=1,
                    agents_completed=1,
                    agents_failed=0,
                )
                db.add(mock_run)
                await db.commit()
                await db.refresh(mock_run)
                captured_run.append(mock_run)
                return mock_run

            # Mock run_personalized_training_pass to simulate real pass execution
            with patch.object(
                PersonalizedTrainingService,
                "run_personalized_training_pass",
                side_effect=fake_run_pass,
            ):
                # Execute the scheduler check
                result = await PersonalizedTrainingService.run_due_training_jobs(db)

            # 3. Assert return dictionary
            self.assertIsInstance(result, dict)
            self.assertTrue(result["triggered"])
            self.assertEqual(result["run_id"], 101)
            self.assertIn("at or past next scheduled run", result["reason"])

            # 4. Verify that the TrainingRun instance was indeed expired by db.commit() in run_due_training_jobs
            run_instance = captured_run[0]
            state = attributes.instance_state(run_instance)
            self.assertTrue(
                state.expired,
                "mock_run must be expired after db.commit() to prove test verifies against MissingGreenlet",
            )

            # 5. Verify db_settings was committed with status='completed' (and NOT overridden to 'failed')
            stmt = select(TrainingSchedulerSetting).limit(1)
            res = await db.execute(stmt)
            updated_settings = res.scalars().first()

            self.assertEqual(updated_settings.last_status, "completed")
            self.assertNotEqual(updated_settings.last_status, "failed")
            next_run = updated_settings.next_run_at
            if next_run and next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
            self.assertIsNotNone(next_run)
            self.assertTrue(next_run > now)

            # 6. Explicitly demonstrate that accessing an attribute on the expired instance without greenlet WOULD raise MissingGreenlet
            from sqlalchemy.exc import MissingGreenlet
            with self.assertRaises(MissingGreenlet):
                _ = run_instance.training_run_id

    async def test_run_due_training_jobs_not_due(self):
        """When not due, returns triggered=False and does not run pass."""
        now = datetime.now(timezone.utc)

        async with self.session_factory() as db:
            db_settings = TrainingSchedulerSetting(
                is_enabled=True,
                interval_days=14,
                lookback_days=14,
                last_run_at=now,
                next_run_at=now + timedelta(days=14),
                last_status="completed",
            )
            db.add(db_settings)
            await db.commit()

            with patch.object(
                PersonalizedTrainingService,
                "run_personalized_training_pass",
                new_callable=AsyncMock,
            ) as mock_pass:
                result = await PersonalizedTrainingService.run_due_training_jobs(db)
                mock_pass.assert_not_called()

            self.assertFalse(result["triggered"])
            self.assertIn("Next scheduled run is at", result["reason"])


if __name__ == "__main__":
    unittest.main()
