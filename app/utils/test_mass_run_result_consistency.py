"""
Test suite for Mass Evaluation Run and Results consistency.
Verifies that:
1. Completed runs with N selected calls match N persisted records in mass_evaluation_results.
2. Querying by run_id returns total=N and items=N.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_run_consistency_test.db"

db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

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

from app.db import get_engine, Base
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.utils.security import create_access_token


class TestMassRunResultConsistency(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("mass_run_consistency_test.db"):
            try:
                os.remove("mass_run_consistency_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=1, company_name="Consistency Co", company_key="cons_co", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=1, service_name="Front", service_key="front", company_id=1)
            db.add(s1)
            await db.flush()

            u_super = User(
                user_id=1,
                username="super_cons",
                email="super_cons@test.com",
                password_hash="dummy",
                role="superadmin",
                company_id=1,
                is_active=True
            )
            db.add(u_super)
            await db.flush()

            job = MassEvaluationJob(job_id=1, job_name="Job Cons", company_id=1, service_id=1, prompt_id=1, is_active=True)
            run = MassEvaluationRun(
                run_id=10,
                job_id=1,
                company_id=1,
                service_id=1,
                trigger_type="manual",
                status="completed",
                calls_found=5,
                calls_selected=5,
                calls_analyzed=5,
                calls_skipped=0,
                calls_failed=0
            )
            db.add_all([job, run])
            await db.flush()

            now = datetime.now(timezone.utc)
            results = []
            for i in range(1, 6):
                res = MassEvaluationResult(
                    mass_analysis_id=100 + i,
                    run_id=10,
                    job_id=1,
                    company_id=1,
                    service_id=1,
                    prompt_id=1,
                    prompt_snapshot="{}",
                    call_id=f"call_cons_{i}",
                    hubspot_owner_id="owner_1",
                    agent_name="Agent Cons",
                    call_timestamp=now,
                    analysis_timestamp=now,
                    evaluacion_global=8.0,
                    status="completed",
                    items_json=[]
                )
                results.append(res)
            db.add_all(results)
            await db.commit()

        self.token_super = create_access_token({"user_id": 1, "email": "super_cons@test.com"})

    async def test_run_results_consistency(self):
        """Verifies that run_id=10 returns total=5 matching calls_selected=5."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?run_id=10",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["total"], 5)
            self.assertEqual(len(data["items"]), 5)


if __name__ == "__main__":
    unittest.main()
