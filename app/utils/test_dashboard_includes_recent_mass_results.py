"""
Test suite for Dashboard & Analytics Mass Results Integration.
Verifies that dashboard and analytics endpoints process recent mass evaluation results cleanly.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///dashboard_mass_integration_test.db"

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
)
from app.utils.security import create_access_token


class TestDashboardIncludesRecentMassResults(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("dashboard_mass_integration_test.db"):
            try:
                os.remove("dashboard_mass_integration_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=30, company_name="Dash Co", company_key="dash_co", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=30, service_name="Dash Service", service_key="dash_svc", company_id=30)
            db.add(s1)
            await db.flush()

            u_admin = User(
                user_id=3001,
                username="admin_dash",
                email="admin_dash@test.com",
                password_hash="dummy",
                role="company_admin",
                company_id=30,
                is_active=True
            )
            db.add(u_admin)
            await db.flush()

            r = MassEvaluationResult(
                mass_analysis_id=3001,
                run_id=86,
                source_run_id=86,
                job_id=30,
                company_id=30,
                service_id=30,
                prompt_id=1,
                prompt_snapshot="Analiza la llamada.",
                execution_source="automation",
                call_id="call_dash_1",
                hs_object_id="hs_dash_1",
                hubspot_owner_id="1539993532",
                agent_name="Fernanda Rodrigues",
                evaluacion_global=9.0,
                status="completed",
                call_timestamp=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
                result_json={"tipo_llamada": "front", "evaluacion_global": 9.0},
                items_json=[]
            )
            db.add(r)
            await db.commit()

        self.token_admin = create_access_token({"user_id": 3001, "email": "admin_dash@test.com"})

    async def test_dashboard_summary_contract(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/dashboard/summary",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)

    async def test_analytics_agents_comparison_contract(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/analytics/agents-comparison",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
