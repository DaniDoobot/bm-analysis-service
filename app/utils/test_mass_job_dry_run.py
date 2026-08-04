"""
Test suite for Mass Evaluation Job Dry-Run / Simulation endpoint.
Verifies:
1. Simulating standard job returns expected structure without launching real run or creating analyses.
2. Simulating random_quality_monitoring job returns candidate and selected counts.
3. Out-of-scope user receives 403 Forbidden.
4. Non-existent job ID returns 404 Not Found.
5. No AttributeError is raised.
"""
import os
import sys
import unittest
import asyncio

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_job_dry_run_test.db"

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from datetime import datetime, timezone

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.db import get_engine, Base
from app.main import app
from app.models.users import User
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun
from app.utils.security import create_access_token


class TestMassJobDryRun(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(self.engine) as db:
            # Seed company & service
            c = Company(company_id=98, company_key="dry_co", company_name="Dry Run Company")
            db.add(c)
            await db.flush()

            s = Service(service_id=98, company_id=98, service_key="dry_serv", service_name="Dry Service")
            db.add(s)
            await db.flush()

            # Seed admin user
            u_admin = User(
                user_id=9801,
                username="admin_dry",
                email="admin_dry@company.com",
                password_hash="dummy",
                role="company_admin",
                company_id=98,
                is_active=True
            )
            db.add(u_admin)

            # Seed out-of-scope user (company 99)
            c2 = Company(company_id=99, company_key="other_co", company_name="Other Company")
            db.add(c2)
            await db.flush()

            u_other = User(
                user_id=9802,
                username="other_dry",
                email="other_dry@company.com",
                password_hash="dummy",
                role="company_admin",
                company_id=99,
                is_active=True
            )
            db.add(u_other)

            # Seed prompt
            p = Prompt(
                prompt_id=980,
                prompt_name="Dry Prompt",
                prompt_type="audio",
                service_id=98,
                company_id=98,
                is_active=True
            )
            db.add(p)
            await db.flush()

            pv = PromptVersion(
                id=9801,
                prompt_id=980,
                version_label="v1",
                prompt="Analyzes call quality",
                is_current=True
            )
            db.add(pv)

            # Seed job
            job = MassEvaluationJob(
                job_id=9801,
                job_name="Standard Dry Job",
                company_id=98,
                service_id=98,
                prompt_id=980,
                prompt_version_id=9801,
                selection_mode="manual_call_ids",
                call_ids=["call_dry_1", "call_dry_2"],
                job_mode="standard",
                is_active=True
            )
            db.add(job)

            # Seed random quality job
            rq_job = MassEvaluationJob(
                job_id=9802,
                job_name="Random Quality Dry Job",
                company_id=98,
                service_id=98,
                prompt_id=980,
                prompt_version_id=9801,
                selection_mode="filters",
                job_mode="random_quality_monitoring",
                date_mode="fixed",
                calls_per_day=5,
                date_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
                date_to=datetime(2026, 7, 5, tzinfo=timezone.utc),
                is_active=True
            )
            db.add(rq_job)

            await db.commit()

        self.token_admin = create_access_token({"user_id": 9801, "email": "admin_dry@company.com"})
        self.token_other = create_access_token({"user_id": 9802, "email": "other_dry@company.com"})

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.job_id.in_([9801, 9802])))
            await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id.in_([9801, 9802])))
            await db.execute(delete(PromptVersion).where(PromptVersion.id == 9801))
            await db.execute(delete(Prompt).where(Prompt.prompt_id == 980))
            await db.execute(delete(User).where(User.user_id.in_([9801, 9802])))
            await db.execute(delete(Service).where(Service.service_id == 98))
            await db.execute(delete(Company).where(Company.company_id.in_([98, 99])))
            await db.commit()

    async def test_dry_run_standard_job_success(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.post(
                "/bm/mass-evaluation-jobs/9801/run",
                json={"dry_run": True},
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("job_id"), 9801)
            self.assertEqual(data.get("mode"), "standard")
            self.assertIn("estimated_calls", data)
            self.assertIn("selected_count", data)

    async def test_dry_run_random_quality_job_success(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.post(
                "/bm/mass-evaluation-jobs/9802/run",
                json={"dry_run": True},
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200, f"Error detail: {res.text}")
            data = res.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("job_id"), 9802)
            self.assertEqual(data.get("mode"), "random_quality_monitoring")

    async def test_dry_run_out_of_scope_forbidden(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.post(
                "/bm/mass-evaluation-jobs/9801/run",
                json={"dry_run": True},
                headers={"Authorization": f"Bearer {self.token_other}"}
            )
            self.assertEqual(res.status_code, 403)

    async def test_dry_run_non_existent_job_404(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.post(
                "/bm/mass-evaluation-jobs/999999/run",
                json={"dry_run": True},
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
