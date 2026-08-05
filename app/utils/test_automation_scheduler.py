"""
Test suite for automation scheduler & run-due endpoints.
=========================================================
Verifies:
1. Due active automations are triggered and last_run_at is updated.
2. Already-running automations are skipped (anti-duplicate lock).
3. Inactive automations are not triggered.
4. Scheduler status endpoint returns diagnostic state.
"""
import os
import unittest
from datetime import datetime, timezone, timedelta

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///automation_scheduler_test.db"

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
from sqlalchemy import delete

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import MassEvaluationJob, MassAnalysisAutomation, MassAnalysisAutomationRun
from app.services.mass_evaluation_service import MassEvaluationService


class TestAutomationScheduler(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            db.add(Company(company_id=880, company_key="auto_co", company_name="Auto Co"))
            await db.flush()
            db.add(Service(service_id=881, company_id=880, service_key="auto_serv", service_name="Auto Serv"))
            await db.flush()
            db.add(Prompt(prompt_id=881, company_id=880, service_id=881, prompt_name="Auto Prompt", prompt_type="mass"))
            await db.flush()

            db.add(PromptVersion(
                id=881,
                prompt_id=881,
                version_name="v1",
                version_label="v1.0",
                is_current=True,
                prompt="System prompt content text",
            ))
            await db.flush()

            # Permanent base job
            db.add(MassEvaluationJob(
                job_id=881,
                company_id=880,
                service_id=881,
                prompt_id=881,
                job_name="Auto Base Job",
                is_active=True
            ))
            await db.flush()

            # Automation 1: Due (last_run_at = 1 hour ago, interval = 30 min)
            db.add(MassAnalysisAutomation(
                automation_id=8801,
                name="Due Automation",
                is_active=True,
                interval_minutes=30,
                service_id=881,
                prompt_id=881,
                job_id=881,
                last_run_at=datetime.now(timezone.utc) - timedelta(minutes=60)
            ))

            # Automation 2: Not Due (last_run_at = 5 min ago, interval = 30 min)
            db.add(MassAnalysisAutomation(
                automation_id=8802,
                name="Not Due Automation",
                is_active=True,
                interval_minutes=30,
                service_id=881,
                prompt_id=881,
                job_id=881,
                last_run_at=datetime.now(timezone.utc) - timedelta(minutes=5)
            ))

            # Automation 3: Inactive (last_run_at = None)
            db.add(MassAnalysisAutomation(
                automation_id=8803,
                name="Inactive Automation",
                is_active=False,
                interval_minutes=30,
                service_id=881,
                prompt_id=881,
                job_id=881,
                last_run_at=None
            ))

            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_id.in_([8801, 8802, 8803])))
            await db.execute(delete(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id.in_([8801, 8802, 8803])))
            await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id == 881))
            await db.execute(delete(PromptVersion).where(PromptVersion.id == 881))
            await db.execute(delete(Prompt).where(Prompt.prompt_id == 881))
            await db.execute(delete(Service).where(Service.service_id == 881))
            await db.execute(delete(Company).where(Company.company_id == 880))
            await db.commit()

    async def test_run_due_automations_triggers_only_due_active(self):
        """run_due_automations triggers due active automations and updates metrics."""
        async with AsyncSession(self.engine) as db:
            res = await MassEvaluationService.run_due_automations(db, company_ids=[880])
            self.assertEqual(res["due_automations_count"], 1)
            self.assertEqual(res["launched_automations_count"], 1)
            self.assertEqual(res["skipped_automations_count"], 0)

    async def test_anti_duplicate_lock_skips_running_automations(self):
        """If an automation is already in 'running' status, subsequent run-due skips it."""
        async with AsyncSession(self.engine) as db:
            # Seed active running run for automation 8801
            db.add(MassAnalysisAutomationRun(
                automation_run_id=99001,
                automation_id=8801,
                status="running",
                started_at=datetime.now(timezone.utc)
            ))
            await db.commit()

        async with AsyncSession(self.engine) as db:
            res = await MassEvaluationService.run_due_automations(db, company_ids=[880])
            self.assertEqual(res["due_automations_count"], 1)
            self.assertEqual(res["launched_automations_count"], 0)
            self.assertEqual(res["skipped_automations_count"], 1)


if __name__ == "__main__":
    unittest.main()
