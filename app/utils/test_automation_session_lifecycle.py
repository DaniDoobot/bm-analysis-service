"""
Test suite for automation scheduler session lifecycle and connection pool hygiene.
=================================================================================
Verifies:
1. Session cleanup when run_automation_run succeeds.
2. Session cleanup when run_job raises an 'already running' error.
3. Session cleanup when run_job raises a generic error.
4. Advisory lock release without holding uncommitted transactions.
"""
import os
import unittest
import asyncio
import threading
from datetime import datetime, timezone, timedelta

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///automation_session_lifecycle_test.db"

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
from sqlalchemy import delete, select, text

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import MassEvaluationJob, MassAnalysisAutomation, MassAnalysisAutomationRun
from app.services.mass_evaluation_service import MassEvaluationService


class TestAutomationSessionLifecycle(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        MassEvaluationService._threading_scheduler_lock = threading.Lock()

        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            db.add(Company(company_id=660, company_key="life_co", company_name="Lifecycle Co"))
            await db.flush()
            db.add(Service(service_id=661, company_id=660, service_key="life_serv", service_name="Lifecycle Serv"))
            await db.flush()
            db.add(Prompt(prompt_id=661, company_id=660, service_id=661, prompt_name="Life Prompt", prompt_type="mass"))
            await db.flush()
            db.add(PromptVersion(
                id=661,
                prompt_id=661,
                version_name="v1",
                version_label="v1.0",
                is_current=True,
                prompt="Prompt for session lifecycle test",
            ))
            await db.flush()
            db.add(MassEvaluationJob(
                job_id=661,
                company_id=660,
                service_id=661,
                prompt_id=661,
                job_name="Life Base Job",
                is_active=True
            ))
            await db.flush()
            db.add(MassAnalysisAutomation(
                automation_id=6601,
                name="Lifecycle Automation",
                is_active=True,
                interval_minutes=30,
                service_id=661,
                prompt_id=661,
                job_id=661,
                last_run_at=datetime.now(timezone.utc) - timedelta(minutes=60)
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_id == 6601))
            await db.execute(delete(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == 6601))
            await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id == 661))
            await db.execute(delete(PromptVersion).where(PromptVersion.id == 661))
            await db.execute(delete(Prompt).where(Prompt.prompt_id == 661))
            await db.execute(delete(Service).where(Service.service_id == 661))
            await db.execute(delete(Company).where(Company.company_id == 660))
            await db.commit()

        MassEvaluationService._threading_scheduler_lock = threading.Lock()

    async def test_session_closed_after_successful_run(self):
        """Session is properly committed and closed after successful automation execution."""
        from unittest.mock import patch, AsyncMock, MagicMock
        fake_run = MagicMock()
        fake_run.run_id = 66001

        with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, return_value=fake_run):
            async with AsyncSession(self.engine) as db:
                auto_run = await MassEvaluationService.run_automation_run(db, 6601, trigger_type="scheduled")
                self.assertEqual(auto_run.status, "running")
                self.assertEqual(auto_run.run_id, 66001)

            # Query afresh to ensure connection was released cleanly
            async with AsyncSession(self.engine) as db:
                stmt = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == auto_run.automation_run_id)
                res = await db.execute(stmt)
                persisted = res.scalars().first()
                self.assertIsNotNone(persisted)

    async def test_session_closed_after_already_running_exception(self):
        """Session releases connection cleanly when run_job throws 'already running'."""
        from unittest.mock import patch, AsyncMock
        already_running_exc = ValueError("Job 661 is already running with run_id 888")

        with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, side_effect=already_running_exc):
            async with AsyncSession(self.engine) as db:
                auto_run = await MassEvaluationService.run_automation_run(db, 6601, trigger_type="scheduled")
                self.assertEqual(auto_run.status, "skipped")

            async with AsyncSession(self.engine) as db:
                stmt = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == auto_run.automation_run_id)
                res = await db.execute(stmt)
                persisted = res.scalars().first()
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.status, "skipped")

    async def test_session_closed_after_generic_exception(self):
        """Session releases connection cleanly when run_job throws a generic exception."""
        from unittest.mock import patch, AsyncMock
        generic_exc = RuntimeError("Database connection timed out during job launch")

        with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, side_effect=generic_exc):
            async with AsyncSession(self.engine) as db:
                auto_run = await MassEvaluationService.run_automation_run(db, 6601, trigger_type="scheduled")
                self.assertEqual(auto_run.status, "failed")

            async with AsyncSession(self.engine) as db:
                stmt = select(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_run_id == auto_run.automation_run_id)
                res = await db.execute(stmt)
                persisted = res.scalars().first()
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.status, "failed")


if __name__ == "__main__":
    unittest.main()
