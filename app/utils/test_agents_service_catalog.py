"""
Unit test suite for /bm/agents service-scoped agent catalog.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agents_catalog_test.db"

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


class TestAgentsServiceCatalog(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("agents_catalog_test.db"):
            try:
                os.remove("agents_catalog_test.db")
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
            s3 = Service(service_id=3, service_name="Comerciales", service_key="comerciales", company_id=1)
            db.add_all([s1, s2, s3])
            await db.flush()

            u_admin = User(
                user_id=1,
                username="admin_catalog",
                email="admin_catalog@test.com",
                role="superadmin",
                company_id=1,
                is_active=True,
                password_hash="dummy"
            )
            db.add(u_admin)

            # Front Agents
            u_ld = User(user_id=10, username="luci", email="ld@test.com", name="Luci Dos Santos", role="agent", company_id=1, primary_service_id=1, hubspot_owner_id="1375831790", agent_initials="LD", is_active=True, password_hash="dummy")
            u_ec = User(user_id=11, username="eugenia", email="ec@test.com", name="Eugenia Carreno", role="agent", company_id=1, primary_service_id=1, hubspot_owner_id="1375831791", agent_initials="EC", is_active=True, password_hash="dummy")
            u_cm = User(user_id=12, username="cristina", email="cm@test.com", name="Cristina Montenegro", role="agent", company_id=1, primary_service_id=1, hubspot_owner_id="33013276", agent_initials="CM", is_active=True, password_hash="dummy")

            # ExpPa Agents
            u_fl = User(user_id=20, username="flavio", email="fl@test.com", name="Flavio Lucich", role="agent", company_id=1, primary_service_id=2, hubspot_owner_id="owner_fl", agent_initials="FL", is_active=True, password_hash="dummy")
            u_mg = User(user_id=21, username="marcelo", email="mg@test.com", name="Marcelo García", role="agent", company_id=1, primary_service_id=2, hubspot_owner_id="owner_mg", agent_initials="MG", is_active=True, password_hash="dummy")
            u_mo = User(user_id=22, username="maria", email="mo@test.com", name="María Olvera", role="agent", company_id=1, primary_service_id=2, hubspot_owner_id="owner_mo", agent_initials="MO", is_active=True, password_hash="dummy")
            u_va = User(user_id=23, username="victoria", email="va@test.com", name="Victoria Arellano", role="agent", company_id=1, primary_service_id=2, hubspot_owner_id="owner_va", agent_initials="VA", is_active=True, password_hash="dummy")

            # Comerciales Agents
            u_ja = User(user_id=30, username="juan", email="ja@test.com", name="Juan Antonio García", role="agent", company_id=1, primary_service_id=3, hubspot_owner_id="owner_ja", agent_initials="JA", is_active=True, password_hash="dummy")
            u_jm = User(user_id=31, username="jose", email="jm@test.com", name="José Moneo", role="agent", company_id=1, primary_service_id=3, hubspot_owner_id="owner_jm", agent_initials="JM", is_active=True, password_hash="dummy")

            db.add_all([u_ld, u_ec, u_cm, u_fl, u_mg, u_mo, u_va, u_ja, u_jm])
            await db.flush()

            # Evaluations: LD in Front (5 evals), FL in ExpPa (3 evals)
            now = datetime.now(timezone.utc)
            for i in range(5):
                db.add(MassEvaluationResult(
                    mass_analysis_id=100 + i, run_id=1, job_id=1, company_id=1, service_id=1, service_key="front",
                    prompt_id=1, prompt_snapshot="{}", call_id=f"call_ld_{i}", hubspot_owner_id="1375831790",
                    agent_name="Luci Dos Santos", call_timestamp=now, analysis_timestamp=now, evaluacion_global=8.5, status="completed", result_json={}, items_json=[]
                ))
            for i in range(3):
                db.add(MassEvaluationResult(
                    mass_analysis_id=200 + i, run_id=1, job_id=1, company_id=1, service_id=2, service_key="experiencia_paciente",
                    prompt_id=1, prompt_snapshot="{}", call_id=f"call_fl_{i}", hubspot_owner_id="owner_fl",
                    agent_name="Flavio Lucich", call_timestamp=now, analysis_timestamp=now, evaluacion_global=9.0, status="completed", result_json={}, items_json=[]
                ))

            await db.commit()

        self.token = create_access_token({"user_id": 1, "email": "admin_catalog@test.com"})

    async def test_get_agents_all_services(self):
        """GET /bm/agents without service returns all active visible agents."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/agents", headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            owner_ids = [a["hubspot_owner_id"] for a in data]
            self.assertIn("1375831790", owner_ids)  # LD
            self.assertIn("owner_fl", owner_ids)    # FL
            self.assertIn("owner_ja", owner_ids)    # JA
            # Ghost agents ST (1459417733) and RG (1375831787) must NOT appear
            self.assertNotIn("1459417733", owner_ids)
            self.assertNotIn("1375831787", owner_ids)

    async def test_get_agents_front(self):
        """GET /bm/agents?service=front returns only Front agents."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/agents?service=front", headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            owner_ids = [a["hubspot_owner_id"] for a in data]
            self.assertEqual(sorted(owner_ids), sorted(["1375831790", "1375831791", "33013276"]))
            # ExpPa agents must NOT appear
            self.assertNotIn("owner_fl", owner_ids)

    async def test_get_agents_experiencia_paciente(self):
        """GET /bm/agents?service=experiencia-paciente returns only ExpPa agents (FL, MG, MO, VA)."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/agents?service=experiencia-paciente", headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            owner_ids = [a["hubspot_owner_id"] for a in data]
            self.assertEqual(sorted(owner_ids), sorted(["owner_fl", "owner_mg", "owner_mo", "owner_va"]))
            
            # Check FL count is 3.0 and VA count is 0.0
            fl_agent = next(a for a in data if a["hubspot_owner_id"] == "owner_fl")
            va_agent = next(a for a in data if a["hubspot_owner_id"] == "owner_va")
            self.assertEqual(fl_agent["total_analyses"], 3.0)
            self.assertEqual(va_agent["total_analyses"], 0.0)

            # Front agents must NOT appear
            self.assertNotIn("1375831790", owner_ids)

    async def test_get_agents_comerciales(self):
        """GET /bm/agents?service=comerciales returns only Comerciales agents (JA, JM)."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/agents?service=comerciales", headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            owner_ids = [a["hubspot_owner_id"] for a in data]
            self.assertEqual(sorted(owner_ids), sorted(["owner_ja", "owner_jm"]))


if __name__ == "__main__":
    unittest.main()
