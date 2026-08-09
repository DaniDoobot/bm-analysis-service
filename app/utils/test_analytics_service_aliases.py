"""
Test suite for service parameter alias resolution (service=front -> service_id=1) in Analytics V2 endpoints.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///analytics_aliases_test.db"

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


class TestAnalyticsServiceAliases(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("analytics_aliases_test.db"):
            try:
                os.remove("analytics_aliases_test.db")
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

            s1 = Service(service_id=1, service_name="Front", service_key="front", company_id=1)
            s2 = Service(service_id=2, service_name="Experiencia Paciente", service_key="experiencia_paciente", company_id=1)
            db.add_all([s1, s2])
            await db.flush()

            u_super = User(
                user_id=1,
                username="super_alias",
                email="super_alias@test.com",
                password_hash="dummy",
                role="superadmin",
                company_id=1,
                is_active=True
            )
            db.add(u_super)
            await db.flush()

            now = datetime.now(timezone.utc)
            r1 = MassEvaluationResult(
                mass_analysis_id=1,
                run_id=1,
                job_id=1,
                company_id=1,
                service_id=1,
                service_key="front",
                service_name="Front",
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_alias_1",
                hubspot_owner_id="1375831791",
                agent_name="Eugenia Carreno",
                call_timestamp=now,
                analysis_timestamp=now,
                evaluacion_global=8.0,
                status="completed",
                result_json={"evaluacion_global": 8.0},
                items_json=[]
            )
            r2 = MassEvaluationResult(
                mass_analysis_id=2,
                run_id=1,
                job_id=1,
                company_id=1,
                service_id=2,
                service_key="experiencia_paciente",
                service_name="Experiencia Paciente",
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call_alias_2",
                hubspot_owner_id="1459417733",
                agent_name="Santiago Taboada",
                call_timestamp=now,
                analysis_timestamp=now,
                evaluacion_global=9.0,
                status="completed",
                result_json={"evaluacion_global": 9.0},
                items_json=[]
            )
            db.add_all([r1, r2])
            await db.commit()

        self.token_super = create_access_token({"user_id": 1, "email": "super_alias@test.com"})

    async def test_agents_comparison_service_alias(self):
        """GET /bm/analytics/agents-comparison?service=front resolves service_id=1."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/analytics/agents-comparison?service=front",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            # Only Front agent Eugenia Carreno (1375831791) should be returned
            agent_ids = [a["hubspot_owner_id"] for a in data["agents"]]
            self.assertIn("1375831791", agent_ids)
            self.assertNotIn("1459417733", agent_ids)

    async def test_filter_options_service_alias(self):
        """GET /bm/analytics/filter-options?service=front filters agents by service_id=1."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/analytics/filter-options?service=front",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            agent_ids = [a["hubspot_owner_id"] for a in data["agents"]]
            self.assertIn("1375831791", agent_ids)
            self.assertNotIn("1459417733", agent_ids)

    async def test_invalid_service_alias_returns_422(self):
        """GET /bm/analytics/agents-comparison?service=invalid_svc returns 422 Unprocessable Entity."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/analytics/agents-comparison?service=invalid_svc",
                headers={"Authorization": f"Bearer {self.token_super}"}
            )
            self.assertEqual(res.status_code, 422, res.text)


if __name__ == "__main__":
    unittest.main()
