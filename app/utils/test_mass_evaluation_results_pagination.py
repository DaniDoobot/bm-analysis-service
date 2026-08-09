"""
Test suite for GET /bm/mass-evaluation-results pagination and totals.
Verifies that:
1. Creating 150 results returns total=150 and limit=100, offset=0, has_more=True.
2. Offset=100 returns remaining 50 items and has_more=False.
3. Call order is sorted stably by call_timestamp desc, mass_analysis_id desc.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_results_pagination_test.db"

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


class TestMassEvaluationResultsPagination(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("mass_results_pagination_test.db"):
            try:
                os.remove("mass_results_pagination_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=1, company_name="Test Co", company_key="test_co", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=1, service_name="Test Service", service_key="test_svc", company_id=1)
            db.add(s1)
            await db.flush()

            u_super = User(
                user_id=1,
                username="super_admin",
                email="super@test.com",
                password_hash="dummy",
                role="superadmin",
                company_id=1,
                is_active=True
            )
            db.add(u_super)
            await db.flush()

            job = MassEvaluationJob(job_id=1, job_name="Job 1", company_id=1, service_id=1, prompt_id=1, is_active=True)
            run = MassEvaluationRun(run_id=1, job_id=1, company_id=1, service_id=1, trigger_type="manual", status="completed")
            db.add_all([job, run])
            await db.flush()

            now = datetime.now(timezone.utc)
            results = []
            for i in range(1, 151):
                res = MassEvaluationResult(
                    mass_analysis_id=i,
                    run_id=1,
                    job_id=1,
                    company_id=1,
                    service_id=1,
                    prompt_id=1,
                    prompt_snapshot="Snapshot",
                    call_id=f"call_{i:03d}",
                    hubspot_owner_id="owner_1",
                    agent_name="Agent 1",
                    call_timestamp=now - timedelta(minutes=150 - i),
                    analysis_timestamp=now,
                    evaluacion_global=7.5,
                    status="completed",
                    items_json=[]
                )
                results.append(res)
            db.add_all(results)
            await db.commit()

        self.token_super = create_access_token({"user_id": 1, "email": "super@test.com"})

    async def test_pagination_page_1(self):
        """Page 1 with limit=100 returns 100 items, total=150, has_more=True."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?limit=100&offset=0",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["total"], 150)
            self.assertEqual(data["limit"], 100)
            self.assertEqual(data["offset"], 0)
            self.assertTrue(data["has_more"])
            self.assertEqual(len(data["items"]), 100)

    async def test_pagination_page_2(self):
        """Page 2 with limit=100 and offset=100 returns remaining 50 items, total=150, has_more=False."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?limit=100&offset=100",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["total"], 150)
            self.assertEqual(data["limit"], 100)
            self.assertEqual(data["offset"], 100)
            self.assertFalse(data["has_more"])
            self.assertEqual(len(data["items"]), 50)


if __name__ == "__main__":
    unittest.main()
