"""
Test suite for automation scheduler concurrency (two workers running simultaneously).
=============================================================================
Verifies:
1. Two workers executing run_due_automations at the same time: only one launches a job,
   the other is skipped due to the advisory lock (or asyncio.Lock fallback in SQLite).
2. No duplicate automation_run rows are created in a concurrent tick.
"""
import os
import unittest
import asyncio
import threading
from datetime import datetime, timezone, timedelta

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///automation_concurrency_test.db"

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import MassEvaluationJob, MassAnalysisAutomation, MassAnalysisAutomationRun
from app.services.mass_evaluation_service import MassEvaluationService


class TestAutomationConcurrency(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Reset the threading lock so each test starts fresh (unlocked)
        MassEvaluationService._threading_scheduler_lock = threading.Lock()

        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            db.add(Company(company_id=770, company_key="conc_co", company_name="Concurrent Co"))
            await db.flush()
            db.add(Service(service_id=771, company_id=770, service_key="conc_serv", service_name="Concurrent Serv"))
            await db.flush()
            db.add(Prompt(prompt_id=771, company_id=770, service_id=771, prompt_name="Conc Prompt", prompt_type="mass"))
            await db.flush()
            db.add(PromptVersion(
                id=771,
                prompt_id=771,
                version_name="v1",
                version_label="v1.0",
                is_current=True,
                prompt="Test prompt for concurrency",
            ))
            await db.flush()
            db.add(MassEvaluationJob(
                job_id=771,
                company_id=770,
                service_id=771,
                prompt_id=771,
                job_name="Conc Base Job",
                is_active=True
            ))
            await db.flush()
            db.add(MassAnalysisAutomation(
                automation_id=7701,
                name="Concurrent Automation",
                is_active=True,
                interval_minutes=30,
                service_id=771,
                prompt_id=771,
                job_id=771,
                last_run_at=datetime.now(timezone.utc) - timedelta(minutes=60)
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_id == 7701))
            await db.execute(delete(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == 7701))
            await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id == 771))
            await db.execute(delete(PromptVersion).where(PromptVersion.id == 771))
            await db.execute(delete(Prompt).where(Prompt.prompt_id == 771))
            await db.execute(delete(Service).where(Service.service_id == 771))
            await db.execute(delete(Company).where(Company.company_id == 770))
            await db.commit()

        # Reset the threading lock after each test
        MassEvaluationService._threading_scheduler_lock = threading.Lock()

    async def test_two_workers_only_one_wins_lock(self):
        """Two concurrent run_due_automations calls: only one should launch a job; the other skips (global lock held)."""
        from unittest.mock import patch, AsyncMock, MagicMock

        fake_run = MagicMock()
        fake_run.run_id = 77001

        launched_count = 0
        skipped_count = 0
        lock_held_count = 0
        results = []

        async def worker():
            with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, return_value=fake_run):
                async with AsyncSession(self.engine) as db:
                    res = await MassEvaluationService.run_due_automations(db, company_ids=[770])
                    results.append(res)

        # Run two workers concurrently — they will race for the asyncio.Lock
        await asyncio.gather(worker(), worker())

        for res in results:
            if res.get("skip_reason") == "global_lock_held":
                lock_held_count += 1
            else:
                launched_count += res.get("launched_automations_count", 0)

        # Exactly one worker should have been blocked by the lock
        self.assertEqual(lock_held_count, 1,
                         f"Expected exactly 1 worker to be blocked by global lock, got {lock_held_count}. Results: {results}")

        # Exactly one worker should have launched (or attempted to launch) the automation
        total_due_processed = sum(r.get("due_automations_count", 0) for r in results if r.get("skip_reason") != "global_lock_held")
        self.assertGreaterEqual(total_due_processed, 1,
                                "The non-blocked worker should have processed due automations")

        # There must NOT be two automation_run rows both in 'running' for the same automation
        async with AsyncSession(self.engine) as db:
            stmt = select(MassAnalysisAutomationRun).where(
                MassAnalysisAutomationRun.automation_id == 7701,
                MassAnalysisAutomationRun.status == "running"
            )
            res_db = await db.execute(stmt)
            running_rows = res_db.scalars().all()

        self.assertLessEqual(len(running_rows), 1,
                             f"At most 1 running row should exist after concurrent tick, found {len(running_rows)}")

    async def test_subsequent_tick_acquires_lock_successfully(self):
        """After a tick finishes, the subsequent tick acquires the lock without any retention."""
        from unittest.mock import patch, AsyncMock, MagicMock

        fake_run = MagicMock()
        fake_run.run_id = 77002

        with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, return_value=fake_run):
            # Tick 1
            async with AsyncSession(self.engine) as db1:
                res1 = await MassEvaluationService.run_due_automations(db1, company_ids=[770])
                self.assertNotEqual(res1.get("skip_reason"), "global_lock_held", "Tick 1 should acquire lock")

            # Tick 2 immediately after Tick 1
            async with AsyncSession(self.engine) as db2:
                res2 = await MassEvaluationService.run_due_automations(db2, company_ids=[770])
                self.assertNotEqual(res2.get("skip_reason"), "global_lock_held", "Tick 2 must acquire lock without being blocked")

            # Tick 3
            async with AsyncSession(self.engine) as db3:
                res3 = await MassEvaluationService.run_due_automations(db3, company_ids=[770])
                self.assertNotEqual(res3.get("skip_reason"), "global_lock_held", "Tick 3 must acquire lock without being blocked")

    async def test_postgresql_xact_lock_dialect_flow(self):
        """Verifies that under PostgreSQL dialect, pg_try_advisory_xact_lock is executed within a transaction block."""
        from unittest.mock import patch, AsyncMock, MagicMock
        from sqlalchemy import text

        mock_conn = MagicMock()
        mock_trans = MagicMock()
        mock_trans.__aenter__ = AsyncMock(return_value=mock_trans)
        mock_trans.__aexit__ = AsyncMock(return_value=None)
        mock_conn.begin = MagicMock(return_value=mock_trans)

        # Mock execute results: lock acquired True, pid 45678
        mock_result_lock = MagicMock()
        mock_result_lock.scalar.return_value = True
        mock_result_pid = MagicMock()
        mock_result_pid.scalar.return_value = 45678

        async def mock_execute(stmt, params=None):
            sql = str(stmt)
            if "pg_try_advisory_xact_lock" in sql:
                return mock_result_lock
            elif "pg_backend_pid" in sql:
                return mock_result_pid
            return MagicMock()

        mock_conn.execute = AsyncMock(side_effect=mock_execute)

        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"
        mock_engine.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.connect.return_value.__aexit__ = AsyncMock(return_value=None)

        async with AsyncSession(self.engine) as db:
            with patch.object(db, "get_bind", return_value=mock_engine):
                with patch.object(MassEvaluationService, "_run_due_automations_inner", new_callable=AsyncMock) as mock_inner:
                    mock_inner.return_value = {
                        "due_automations_count": 1,
                        "launched_automations_count": 1,
                        "skipped_automations_count": 0,
                        "stale_runs_closed": 0,
                        "executions": [],
                    }
                    res = await MassEvaluationService.run_due_automations(db, company_ids=[770])

                    self.assertEqual(res["launched_automations_count"], 1)
                    mock_conn.begin.assert_called_once()
                    mock_inner.assert_called_once()

    async def test_postgresql_xact_lock_skipped_when_held(self):
        """Verifies that under PostgreSQL dialect, if lock is held, worker skips immediately."""
        from unittest.mock import patch, AsyncMock, MagicMock

        mock_conn = MagicMock()
        mock_trans = MagicMock()
        mock_trans.__aenter__ = AsyncMock(return_value=mock_trans)
        mock_trans.__aexit__ = AsyncMock(return_value=None)
        mock_conn.begin = MagicMock(return_value=mock_trans)

        # Lock returns False (held by another worker)
        mock_result_lock = MagicMock()
        mock_result_lock.scalar.return_value = False
        mock_conn.execute = AsyncMock(return_value=mock_result_lock)

        mock_engine = MagicMock()
        mock_engine.dialect.name = "postgresql"
        mock_engine.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.connect.return_value.__aexit__ = AsyncMock(return_value=None)

        async with AsyncSession(self.engine) as db:
            with patch.object(db, "get_bind", return_value=mock_engine):
                with patch.object(MassEvaluationService, "_run_due_automations_inner", new_callable=AsyncMock) as mock_inner:
                    res = await MassEvaluationService.run_due_automations(db, company_ids=[770])

                    self.assertEqual(res.get("skip_reason"), "global_lock_held")
                    self.assertEqual(res.get("launched_automations_count"), 0)
                    mock_inner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
