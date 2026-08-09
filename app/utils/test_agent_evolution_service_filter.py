"""
Unit test suite for /bm/agents/{owner_id}/evolution service scoping.
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agent_evolution_svc_test.db"

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
from app.models.mass_evaluations import MassEvaluationResult
from app.utils.security import create_access_token


class TestAgentEvolutionServiceFilter(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("agent_evolution_svc_test.db"):
            try:
                os.remove("agent_evolution_svc_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston_medical", is_active=True)
            db.add(c1)
            await db.flush()

            s1 = Service(service_id=1, service_name="Front", service_key="front", company_id=1)
            s2 = Service(service_id=2, service_name="Experiencia de Paciente", service_key="experiencia_paciente", company_id=1)
            db.add_all([s1, s2])
            await db.flush()

            u_admin = User(
                user_id=1,
                username="admin_evo",
                email="admin_evo@test.com",
                role="superadmin",
                company_id=1,
                is_active=True,
                password_hash="dummy"
            )
            db.add(u_admin)

            # Front Agent LD
            u_ld = User(user_id=10, username="luci", email="ld@test.com", name="Luci Dos Santos", role="agent", company_id=1, primary_service_id=1, hubspot_owner_id="1375831790", agent_initials="LD", is_active=True, password_hash="dummy")
            db.add(u_ld)
            await db.flush()

            call_time = datetime.now(timezone.utc) - timedelta(days=1)
            # 2 evals for LD in Front (service_id=1)
            r1 = MassEvaluationResult(
                mass_analysis_id=101, run_id=1, job_id=1, company_id=1, service_id=1, service_key="front",
                prompt_id=1, prompt_snapshot="{}", call_id="call_ld_1", hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos", call_timestamp=call_time, analysis_timestamp=call_time, evaluacion_global=7.0, status="completed", result_json={"evaluacion_global": 7.0}, items_json=[]
            )
            r2 = MassEvaluationResult(
                mass_analysis_id=102, run_id=1, job_id=1, company_id=1, service_id=1, service_key="front",
                prompt_id=1, prompt_snapshot="{}", call_id="call_ld_2", hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos", call_timestamp=call_time, analysis_timestamp=call_time, evaluacion_global=9.0, status="completed", result_json={"evaluacion_global": 9.0}, items_json=[]
            )
            db.add_all([r1, r2])
            await db.commit()

        self.token = create_access_token({"user_id": 1, "email": "admin_evo@test.com"})

    async def test_agent_evolution_service_front(self):
        """GET /bm/agents/1375831790/evolution?service=front returns 2 analyses."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/agents/1375831790/evolution?service=front",
                headers={"Authorization": f"Bearer {self.token}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["summary"]["total_analyses"], 2.0)
            self.assertEqual(data["summary"]["avg_evaluacion_global"], 8.0)

    async def test_agent_evolution_service_exp_pa_returns_clean_empty(self):
        """GET /bm/agents/1375831790/evolution?service=experiencia-paciente returns clean 0 response."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get(
                "/bm/agents/1375831790/evolution?service=experiencia-paciente",
                headers={"Authorization": f"Bearer {self.token}"}
            )
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertEqual(data["summary"]["total_analyses"], 0.0)
            self.assertEqual(data["trend"]["evaluacion_global_direction"], "no_data")


if __name__ == "__main__":
    unittest.main()
