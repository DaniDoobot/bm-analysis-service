"""
Test suite for Dashboard Performance & Response Contracts.
Verifies:
1. /bm/dashboard/summary responds cleanly with 200 OK.
2. /bm/dashboard/objections responds cleanly with 200 OK.
3. /bm/analytics/agents-comparison responds cleanly with 200 OK.
4. /bm/analytics/items responds cleanly with 200 OK.
5. /bm/analytics/items-evolution responds cleanly with 200 OK.
6. /bm/service-evolution responds cleanly with 200 OK.
"""
import os
import sys
import unittest
import asyncio

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///dashboard_perf_contract_test.db"

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
from app.utils.security import create_access_token


class TestDashboardPerformanceContract(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(self.engine) as db:
            c = Company(company_id=95, company_key="perf_co", company_name="Performance Company")
            db.add(c)
            await db.flush()

            s = Service(service_id=95, company_id=95, service_key="perf_serv", service_name="Performance Service")
            db.add(s)
            await db.flush()

            u_admin = User(
                user_id=9501,
                username="admin_perf",
                email="admin_perf@company.com",
                password_hash="dummy",
                role="company_admin",
                company_id=95,
                is_active=True
            )
            db.add(u_admin)
            await db.commit()

        self.token_admin = create_access_token({"user_id": 9501, "email": "admin_perf@company.com"})

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(User).where(User.user_id == 9501))
            await db.execute(delete(Service).where(Service.service_id == 95))
            await db.execute(delete(Company).where(Company.company_id == 95))
            await db.commit()

    async def test_dashboard_summary_contract(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/dashboard/summary",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)

    async def test_dashboard_objections_contract(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/dashboard/objections",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)

    async def test_analytics_agents_comparison_contract(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/analytics/agents-comparison",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)

    async def test_analytics_items_contract(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/analytics/items",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)

    async def test_analytics_items_evolution_contract(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/analytics/items-evolution",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)

    async def test_service_evolution_contract(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            res = await ac.get(
                "/bm/service-evolution",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
