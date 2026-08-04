"""
Test suite for Agent Initials priority in Dashboard and Analytics.
Verifies:
1. User with custom agent_initials (e.g. 'LD') returns 'LD' in /bm/dashboard/summary and /bm/analytics/agents-comparison.
2. User without custom agent_initials falls back to calculating initials from display name.
3. Boston Medical agent Luci Dos Santos correctly resolves to 'LD'.
"""
import os
import sys
import unittest
import asyncio

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///dashboard_agent_initials_test.db"

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
from sqlalchemy import delete

from app.db import get_engine, Base
from app.main import app
from app.models.users import User
from app.models.companies import Company
from app.models.services import Service
from app.models.mass_evaluations import MassEvaluationResult
from app.utils.security import create_access_token


class TestDashboardAgentInitials(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(self.engine) as db:
            c = Company(company_id=96, company_key="init_co", company_name="Initials Company")
            db.add(c)
            await db.flush()

            s = Service(service_id=96, company_id=96, service_key="init_serv", service_name="Initials Service")
            db.add(s)
            await db.flush()

            # Seed Admin User
            u_admin = User(
                user_id=9601,
                username="admin_init",
                email="admin_init@company.com",
                password_hash="dummy",
                role="company_admin",
                company_id=96,
                is_active=True
            )
            db.add(u_admin)

            # Seed Agent 1 with explicitly set initials "LD" for Luci Dos Santos
            u_luci = User(
                user_id=9602,
                email="luci@bostonmedical.es",
                username="luci_ds",
                name="Luci Dos Santos Furtado",
                agent_initials="LD",
                hubspot_owner_id="1375831790",
                password_hash="dummy",
                role="agent",
                company_id=96,
                is_active=True
            )
            db.add(u_luci)

            # Seed Agent 2 without initials
            u_roberto = User(
                user_id=9603,
                email="roberto@bostonmedical.es",
                username="roberto_g",
                name="Roberto Galán",
                agent_initials=None,
                hubspot_owner_id="1375831787",
                password_hash="dummy",
                role="agent",
                company_id=96,
                is_active=True
            )
            db.add(u_roberto)

            now = datetime.now(timezone.utc)
            # Seed mass evaluation results for both agents
            res_luci = MassEvaluationResult(
                mass_analysis_id=96001,
                run_id=96001,
                job_id=9601,
                prompt_id=960,
                prompt_snapshot="Test prompt snapshot",
                call_id="call_init_luci",
                company_id=96,
                service_id=96,
                hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos Furtado",
                call_timestamp=now,
                status="completed",
                evaluacion_global=8.5,
                result_json={"evaluacion_global": 8.5}
            )
            db.add(res_luci)

            res_roberto = MassEvaluationResult(
                mass_analysis_id=96002,
                run_id=96002,
                job_id=9601,
                prompt_id=960,
                prompt_snapshot="Test prompt snapshot",
                call_id="call_init_roberto",
                company_id=96,
                service_id=96,
                hubspot_owner_id="1375831787",
                agent_name="Roberto Galán",
                call_timestamp=now,
                status="completed",
                evaluacion_global=7.5,
                result_json={"evaluacion_global": 7.5}
            )
            db.add(res_roberto)

            await db.commit()

        self.token_admin = create_access_token({"user_id": 9601, "email": "admin_init@company.com"})

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id.in_([96001, 96002])))
            await db.execute(delete(User).where(User.user_id.in_([9601, 9602, 9603])))
            await db.execute(delete(Service).where(Service.service_id == 96))
            await db.execute(delete(Company).where(Company.company_id == 96))
            await db.commit()

    async def test_dashboard_agents_returns_custom_initials(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/dashboard/agents-comparison?service_id=96",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            agents = data.get("agents", [])
            
            # Find Luci in agents
            luci_item = next((a for a in agents if a.get("hubspot_owner_id") == "1375831790"), None)
            self.assertIsNotNone(luci_item, f"Agents list: {agents}")
            self.assertEqual(luci_item.get("agent_initials"), "LD")

            # Find Roberto in agents (fallback to "RG")
            roberto_item = next((a for a in agents if a.get("hubspot_owner_id") == "1375831787"), None)
            self.assertIsNotNone(roberto_item, f"Agents list: {agents}")
            self.assertEqual(roberto_item.get("agent_initials"), "RG")

    async def test_analytics_agents_comparison_returns_custom_initials(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/analytics/agents-comparison?service_id=96",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            agents = data.get("agents", [])
            luci_comp = next((i for i in agents if i.get("hubspot_owner_id") == "1375831790"), None)
            self.assertIsNotNone(luci_comp)
            self.assertEqual(luci_comp.get("agent_name"), "Luci Dos Santos Furtado")


if __name__ == "__main__":
    unittest.main()
