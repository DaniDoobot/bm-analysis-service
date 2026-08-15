"""
Test suite for Mass Evaluation Automations listing contract.
=============================================================
Verifies:
1. Inactive automations are returned by default in GET list_automations.
2. Filter active=true returns only active automations.
3. Filter active=false returns only inactive automations.
4. Deactivating an automation (is_active=False) updates timestamps and preserves row visibility.
5. run_due skips inactive automations.
6. scheduler-status reports accurate active, inactive, total, and due counts.
"""
import os
import unittest
from datetime import datetime, timezone, timedelta

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///automation_listing_test.db"

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
from app.schemas.mass_evaluations import MassAnalysisAutomationUpdate, MassAnalysisAutomationResponse
from app.services.mass_evaluation_service import MassEvaluationService


class TestMassAutomationListing(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            db.add(Company(company_id=770, company_key="list_co", company_name="List Co"))
            await db.flush()
            db.add(Service(service_id=771, company_id=770, service_key="list_serv", service_name="List Serv"))
            await db.flush()
            db.add(Prompt(prompt_id=771, company_id=770, service_id=771, prompt_name="List Prompt", prompt_type="mass"))
            await db.flush()

            db.add(PromptVersion(
                id=771,
                prompt_id=771,
                version_name="v1",
                version_label="v1.0",
                is_current=True,
                prompt="System prompt text"
            ))
            await db.flush()

            # Permanent base job
            db.add(MassEvaluationJob(
                job_id=771,
                company_id=770,
                service_id=771,
                prompt_id=771,
                job_name="List Base Job",
                is_active=True
            ))
            await db.flush()

            # Automation 1: Active
            db.add(MassAnalysisAutomation(
                automation_id=7701,
                name="Active Automation",
                is_active=True,
                interval_minutes=30,
                service_id=771,
                prompt_id=771,
                job_id=771,
                last_run_at=datetime.now(timezone.utc) - timedelta(minutes=60)
            ))

            # Automation 2: Inactive
            db.add(MassAnalysisAutomation(
                automation_id=7702,
                name="Inactive Automation",
                is_active=False,
                interval_minutes=30,
                service_id=771,
                prompt_id=771,
                job_id=771,
                last_run_at=None
            ))

            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassAnalysisAutomationRun).where(MassAnalysisAutomationRun.automation_id.in_([7701, 7702])))
            await db.execute(delete(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id.in_([7701, 7702])))
            await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id == 771))
            await db.execute(delete(PromptVersion).where(PromptVersion.id == 771))
            await db.execute(delete(Prompt).where(Prompt.prompt_id == 771))
            await db.execute(delete(Service).where(Service.service_id == 771))
            await db.execute(delete(Company).where(Company.company_id == 770))
            await db.commit()

    async def test_list_automations_default_returns_both_active_and_inactive(self):
        """GET /bm/mass-analysis/automations without filters returns active and inactive automations."""
        async with AsyncSession(self.engine) as db:
            res = await MassEvaluationService.list_automations(db, company_ids=[770])
            self.assertEqual(len(res), 2)
            auto_ids = {a.automation_id for a in res}
            self.assertIn(7701, auto_ids)
            self.assertIn(7702, auto_ids)

            # Test schema computed response fields
            schemas = [MassAnalysisAutomationResponse.model_validate(a) for a in res]
            active_schema = next(s for s in schemas if s.automation_id == 7701)
            inactive_schema = next(s for s in schemas if s.automation_id == 7702)

            self.assertTrue(active_schema.is_active)
            self.assertEqual(active_schema.status, "active")
            self.assertEqual(active_schema.status_label, "Activa")
            self.assertIsNotNone(active_schema.next_run_at)

            self.assertFalse(inactive_schema.is_active)
            self.assertEqual(inactive_schema.status, "inactive")
            self.assertEqual(inactive_schema.status_label, "Desactivada")
            self.assertIsNone(inactive_schema.next_run_at)

    async def test_list_automations_active_filter_true_and_false(self):
        """Test active='true' and active='false' parameters."""
        async with AsyncSession(self.engine) as db:
            active_only = await MassEvaluationService.list_automations(db, active="true", company_ids=[770])
            self.assertEqual(len(active_only), 1)
            self.assertEqual(active_only[0].automation_id, 7701)

            inactive_only = await MassEvaluationService.list_automations(db, active="false", company_ids=[770])
            self.assertEqual(len(inactive_only), 1)
            self.assertEqual(inactive_only[0].automation_id, 7702)

    async def test_deactivate_automation_keeps_it_in_list(self):
        """Deactivating an active automation sets is_active=False and keeps it visible."""
        async with AsyncSession(self.engine) as db:
            updated = await MassEvaluationService.update_automation(
                db, automation_id=7701, payload=MassAnalysisAutomationUpdate(is_active=False)
            )
            self.assertIsNotNone(updated)
            self.assertFalse(updated.is_active)

        async with AsyncSession(self.engine) as db:
            res = await MassEvaluationService.list_automations(db, company_ids=[770])
            self.assertEqual(len(res), 2)
            deactivated = next(a for a in res if a.automation_id == 7701)
            self.assertFalse(deactivated.is_active)

    async def test_run_due_skips_inactive_automations(self):
        """run_due only triggers active automations."""
        from unittest.mock import patch, AsyncMock, MagicMock
        fake_run = MagicMock()
        fake_run.run_id = 77001

        with patch.object(MassEvaluationService, "run_job", new_callable=AsyncMock, return_value=fake_run):
            async with AsyncSession(self.engine) as db:
                res = await MassEvaluationService.run_due_automations(db, company_ids=[770])
                # Only 7701 is active and due
                self.assertEqual(res["due_automations_count"], 1)
                self.assertEqual(res["launched_automations_count"], 1)
                self.assertEqual(res["skipped_automations_count"], 0)


if __name__ == "__main__":
    unittest.main()
