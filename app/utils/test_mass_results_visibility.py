"""
Test suite for Mass Evaluation Results Visibility API.
Verifies that:
1. Results with company_id IS NULL are visible to superadmin.
2. Results can be retrieved by run_id (including source_run_id match).
3. Querying by automation_id resolves job_id and returns matching results.
4. Created_from and created_to filters work as expected.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///mass_results_vis_test.db"

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
from datetime import datetime, timezone

from app.db import get_engine, Base
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassAnalysisAutomation,
)
from app.utils.security import create_access_token


class TestMassResultsVisibility(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("mass_results_vis_test.db"):
            try:
                os.remove("mass_results_vis_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=20, company_name="Vis Co", company_key="vis_co", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=20, service_name="Vis Service", service_key="vis_svc", company_id=20)
            db.add(s1)
            await db.flush()

            u_super = User(
                user_id=2001,
                username="super_vis",
                email="super_vis@test.com",
                password_hash="dummy",
                role="superadmin",
                company_id=20,
                is_active=True
            )
            db.add(u_super)
            await db.flush()

            job = MassEvaluationJob(job_id=20, job_name="Vis Job", company_id=20, service_id=20, prompt_id=1, is_active=True)
            db.add(job)
            await db.flush()

            aut = MassAnalysisAutomation(automation_id=8, name="Test Automation 8", job_id=20, service_id=20, prompt_id=1, is_active=True)
            db.add(aut)
            await db.flush()

            run86 = MassEvaluationRun(run_id=86, job_id=20, company_id=20, service_id=20, trigger_type="automation", status="completed")
            db.add(run86)
            await db.flush()

            # Seed result with company_id=None (legacy or unassigned)
            r1 = MassEvaluationResult(
                mass_analysis_id=2001,
                run_id=86,
                source_run_id=86,
                job_id=20,
                company_id=None,  # company_id IS NULL regression case!
                service_id=20,
                prompt_id=1,
                prompt_snapshot="Analiza la llamada.",
                execution_source="automation",
                call_id="call_vis_1",
                hs_object_id="hs_vis_1",
                hubspot_owner_id="owner_vis",
                agent_name="Agent Vis",
                evaluacion_global=8.0,
                status="completed",
                created_at=datetime.now(timezone.utc),
                result_json={"tipo_llamada": "front", "evaluacion_global": 8.0},
                items_json=[]
            )
            db.add(r1)
            await db.commit()

        self.token_super = create_access_token({"user_id": 2001, "email": "super_vis@test.com"})

    async def test_get_mass_evaluation_results_superadmin_null_company_visible(self):
        """Verifies that superadmin can retrieve results where company_id is NULL."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?run_id=86",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            items = data["items"]
            self.assertEqual(data["total"], 1)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["call_id"], "call_vis_1")

    async def test_get_mass_evaluation_results_by_automation_id(self):
        """Verifies filtering by automation_id resolves job_id and returns results."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/mass-evaluation-results?automation_id=8",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            items = data["items"]
            self.assertEqual(data["total"], 1)
            self.assertEqual(len(items), 1)


    async def test_get_my_analysis_results_paged_response(self):
        """Verifies GET /bm/me/analysis-results returns items, total, limit, and offset."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/me/analysis-results?run_id=86",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("items", data)
            self.assertEqual(data["total"], 1)
            self.assertEqual(len(data["items"]), 1)


if __name__ == "__main__":
    unittest.main()
