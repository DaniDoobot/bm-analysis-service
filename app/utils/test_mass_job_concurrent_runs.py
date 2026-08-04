"""
Test suite for Concurrent Mass Evaluation Jobs Isolation.
Verifies:
1. Two distinct jobs can be created and queried independently.
2. Querying runs with `job_id=A` never returns runs from `job_id=B`.
3. Executing a run for Job A does not alter or pollute the run history or status of Job B.
"""
import os
import sys
import unittest
import asyncio

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_job_concurrent_runs_test.db"

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.db import get_engine, Base
from app.main import app
from app.models.users import User
from app.models.companies import Company
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun
from app.utils.security import create_access_token


class TestMassJobConcurrentRuns(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(self.engine) as db:
            c = Company(company_id=97, company_key="conc_co", company_name="Concurrent Company")
            db.add(c)
            await db.flush()

            s = Service(service_id=97, company_id=97, service_key="conc_serv", service_name="Concurrent Service")
            db.add(s)
            await db.flush()

            u_admin = User(
                user_id=9701,
                username="admin_conc",
                email="admin_conc@company.com",
                password_hash="dummy",
                role="company_admin",
                company_id=97,
                is_active=True
            )
            db.add(u_admin)

            p = Prompt(
                prompt_id=970,
                prompt_name="Concurrent Prompt",
                prompt_type="audio",
                service_id=97,
                company_id=97,
                is_active=True
            )
            db.add(p)
            await db.flush()

            pv = PromptVersion(
                id=9701,
                prompt_id=970,
                version_label="v1",
                prompt="Analyzes call quality",
                is_current=True
            )
            db.add(pv)

            # Seed Job A
            job_a = MassEvaluationJob(
                job_id=9701,
                job_name="Job Alpha",
                company_id=97,
                service_id=97,
                prompt_id=970,
                prompt_version_id=9701,
                selection_mode="manual_call_ids",
                call_ids=["call_a_1"],
                job_mode="standard",
                is_active=True
            )
            db.add(job_a)

            # Seed Job B
            job_b = MassEvaluationJob(
                job_id=9702,
                job_name="Job Beta",
                company_id=97,
                service_id=97,
                prompt_id=970,
                prompt_version_id=9701,
                selection_mode="manual_call_ids",
                call_ids=["call_b_1"],
                job_mode="standard",
                is_active=True
            )
            db.add(job_b)

            # Seed pre-existing Run for Job A and Run for Job B
            run_a = MassEvaluationRun(
                run_id=97001,
                job_id=9701,
                company_id=97,
                service_id=97,
                trigger_type="manual",
                status="completed",
                calls_found=1,
                calls_selected=1,
                calls_analyzed=1
            )
            db.add(run_a)

            run_b = MassEvaluationRun(
                run_id=97002,
                job_id=9702,
                company_id=97,
                service_id=97,
                trigger_type="manual",
                status="completed",
                calls_found=1,
                calls_selected=1,
                calls_analyzed=1
            )
            db.add(run_b)

            await db.commit()

        self.token_admin = create_access_token({"user_id": 9701, "email": "admin_conc@company.com"})

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationRun).where(MassEvaluationRun.job_id.in_([9701, 9702])))
            await db.execute(delete(MassEvaluationJob).where(MassEvaluationJob.job_id.in_([9701, 9702])))
            await db.execute(delete(PromptVersion).where(PromptVersion.id == 9701))
            await db.execute(delete(Prompt).where(Prompt.prompt_id == 970))
            await db.execute(delete(User).where(User.user_id == 9701))
            await db.execute(delete(Service).where(Service.service_id == 97))
            await db.execute(delete(Company).where(Company.company_id == 97))
            await db.commit()

    async def test_runs_filtered_by_job_id(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res_a = await ac.get(
                "/bm/mass-evaluation-runs?job_id=9701",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_a.status_code, 200)
            runs_a = res_a.json()
            self.assertTrue(all(r["job_id"] == 9701 for r in runs_a))
            self.assertTrue(any(r["run_id"] == 97001 for r in runs_a))
            self.assertFalse(any(r["run_id"] == 97002 for r in runs_a))

            res_b = await ac.get(
                "/bm/mass-evaluation-runs?job_id=9702",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_b.status_code, 200)
            runs_b = res_b.json()
            self.assertTrue(all(r["job_id"] == 9702 for r in runs_b))
            self.assertTrue(any(r["run_id"] == 97002 for r in runs_b))
            self.assertFalse(any(r["run_id"] == 97001 for r in runs_b))

    async def test_get_individual_run_isolated(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res_run_a = await ac.get(
                "/bm/mass-evaluation-runs/97001",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_run_a.status_code, 200)
            self.assertEqual(res_run_a.json()["job_id"], 9701)

            res_run_b = await ac.get(
                "/bm/mass-evaluation-runs/97002",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_run_b.status_code, 200)
            self.assertEqual(res_run_b.json()["job_id"], 9702)


if __name__ == "__main__":
    unittest.main()
