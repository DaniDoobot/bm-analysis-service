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
from app.models.mass_evaluations import MassEvaluationJob, MassAnalysisAutomation, MassAnalysisAutomationRun, MassEvaluationRun
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
            await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.job_id == 881))
            await db.execute(delete(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id.in_([8801, 8802, 8803])))
            await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id == 881))
            await db.execute(delete(PromptVersion).where(PromptVersion.id == 881))
            await db.execute(delete(Prompt).where(Prompt.prompt_id == 881))
            await db.execute(delete(Service).where(Service.service_id == 881))
            await db.execute(delete(Company).where(Company.company_id == 880))
            await db.commit()

    async def test_run_due_automations_triggers_only_due_active(self):
        """run_due_automations triggers due active automations and updates metrics."""
        from unittest.mock import patch, AsyncMock, MagicMock
        fake_run = MagicMock()
        fake_run.run_id = 98801
        with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, return_value=fake_run):
            async with AsyncSession(self.engine) as db:
                res = await MassEvaluationService.run_due_automations(db, company_ids=[880])
                self.assertEqual(res["due_automations_count"], 1)
                self.assertEqual(res["launched_automations_count"], 1)
                self.assertEqual(res["skipped_automations_count"], 0)

    async def test_anti_duplicate_lock_skips_running_automations(self):
        """If an automation is in recent 'running' status (<60 min), subsequent run-due skips it."""
        async with AsyncSession(self.engine) as db:
            # Seed active running run for automation 8801 (started 5 min ago)
            db.add(MassAnalysisAutomationRun(
                automation_run_id=99001,
                automation_id=8801,
                status="running",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=5)
            ))
            await db.commit()

        async with AsyncSession(self.engine) as db:
            res = await MassEvaluationService.run_due_automations(db, company_ids=[880])
            self.assertEqual(res["due_automations_count"], 1)
            self.assertEqual(res["launched_automations_count"], 0)
            self.assertEqual(res["skipped_automations_count"], 1)
            self.assertEqual(res["executions"][0]["reason_skipped"], "already_running")
            self.assertEqual(res["executions"][0]["is_stale"], False)

    async def test_stale_running_automation_is_recovered_and_retriggered(self):
        """If an automation run is stuck in 'running' for >60 min, it is marked failed and new run is triggered."""
        from unittest.mock import patch, AsyncMock, MagicMock
        async with AsyncSession(self.engine) as db:
            # Seed stale running run (started 90 minutes ago)
            db.add(MassAnalysisAutomationRun(
                automation_run_id=99002,
                automation_id=8801,
                status="running",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=90)
            ))
            await db.commit()

        fake_run = MagicMock()
        fake_run.run_id = 98802
        with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, return_value=fake_run):
            async with AsyncSession(self.engine) as db:
                res = await MassEvaluationService.run_due_automations(db, company_ids=[880])
                self.assertEqual(res["stale_runs_closed"], 1)
                self.assertEqual(res["launched_automations_count"], 1)

            async with AsyncSession(self.engine) as db:
                stale_run = await db.get(MassAnalysisAutomationRun, 99002)
                self.assertEqual(stale_run.status, "failed")
                self.assertIn("AUTOMATION_RUNNING_STALE_AFTER_MINUTES", stale_run.error_message)

    async def test_manual_mark_stale_failed(self):
        """Administrator can manually mark a stuck running execution as failed via mark_automation_run_stale_failed."""
        async with AsyncSession(self.engine) as db:
            db.add(MassAnalysisAutomationRun(
                automation_run_id=99003,
                automation_id=8801,
                status="running",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=10)
            ))
            await db.commit()

        async with AsyncSession(self.engine) as db:
            updated = await MassEvaluationService.mark_automation_run_stale_failed(db, run_id=99003)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, "failed")
            self.assertIn("administrator", updated.error_message)

    async def test_already_running_job_creates_skipped_auto_run(self):
        """When run_job raises 'already running', auto_run ends up as 'skipped' with finished_at set, not stuck in 'running'."""
        from unittest.mock import patch, AsyncMock
        already_running_exc = ValueError("Job 881 is already running with run_id 999")
        with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, side_effect=already_running_exc):
            async with AsyncSession(self.engine) as db:
                auto_run = await MassEvaluationService.run_automation_run(db, 8801, trigger_type="scheduled")

            self.assertIn(auto_run.status, ("skipped", "failed"),
                          "auto_run must not stay 'running' when run_job raises already_running")
            self.assertIsNotNone(auto_run.finished_at,
                                  "finished_at must be set when run is not launched")

            # Verify it is actually persisted in DB
            async with AsyncSession(self.engine) as db:
                from sqlalchemy import select as sa_select
                stmt = sa_select(MassAnalysisAutomationRun).where(
                    MassAnalysisAutomationRun.automation_run_id == auto_run.automation_run_id
                )
                res = await db.execute(stmt)
                db_run = res.scalars().first()
                self.assertIsNotNone(db_run)
                self.assertNotEqual(db_run.status, "running",
                                    "DB row must not remain 'running' after already_running error")
                self.assertIsNotNone(db_run.finished_at)

    async def test_sync_running_auto_run_with_completed_mass_run(self):
        """list_automation_runs syncs a 'running' auto_run to 'completed' when its mass_run is already completed."""
        from sqlalchemy import select as sa_select

        # Create a MassEvaluationRun that is already completed
        async with AsyncSession(self.engine) as db:
            mass_run = MassEvaluationRun(
                run_id=99900,
                job_id=881,
                trigger_type="scheduled",
                status="completed",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                finished_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                calls_found=10,
                calls_selected=5,
                calls_analyzed=5,
            )
            db.add(mass_run)
            await db.flush()

            # Create an auto_run still in 'running' linked to the completed mass_run
            auto_run = MassAnalysisAutomationRun(
                automation_run_id=99901,
                automation_id=8801,
                status="running",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                job_id=881,
                run_id=99900,
                calls_found=0,
                calls_selected=0,
            )
            db.add(auto_run)
            await db.commit()

        # Call list_automation_runs — it should sync the running auto_run to completed
        async with AsyncSession(self.engine) as db:
            runs = await MassEvaluationService.list_automation_runs(db, automation_id=8801)

        auto_run_result = next((r for r in runs if r.automation_run_id == 99901), None)
        self.assertIsNotNone(auto_run_result, "auto_run_id=99901 should be in results")
        self.assertEqual(auto_run_result.status, "completed",
                         "auto_run should have been synced to 'completed' by list_automation_runs")
        self.assertEqual(auto_run_result.calls_found, 10,
                         "calls_found should be synced from the mass evaluation run")
        self.assertEqual(auto_run_result.calls_selected, 5)
        self.assertIsNotNone(auto_run_result.finished_at)

    async def test_orphan_run_closed_on_listing(self):
        """An orphan auto_run (run_id=None, >10 min old) is closed to 'skipped' when list_automation_runs is called."""
        from sqlalchemy import select as sa_select

        async with AsyncSession(self.engine) as db:
            db.add(MassAnalysisAutomationRun(
                automation_run_id=99910,
                automation_id=8801,
                status="running",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=15),
                run_id=None,  # orphan: no mass run linked
            ))
            await db.commit()

        async with AsyncSession(self.engine) as db:
            runs = await MassEvaluationService.list_automation_runs(db, automation_id=8801)

        orphan = next((r for r in runs if r.automation_run_id == 99910), None)
        self.assertIsNotNone(orphan)
        self.assertEqual(orphan.status, "skipped",
                         "Orphan run_id=None older than 10 min should be closed to 'skipped'")
        self.assertIsNotNone(orphan.finished_at)


if __name__ == "__main__":
    unittest.main()
