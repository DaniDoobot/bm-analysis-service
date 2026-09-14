"""
Test Suite: Company is_demo Security Flag
Tests:
A) Legacy/existing company has is_demo == False.
B) Normal API creation via POST /bm/companies yields is_demo == False.
C) CompanyResponse and AdminCompanyResponse include is_demo field.
D) PATCH /bm/companies/{id} cannot alter is_demo (neither escalate nor demote).
E) External POST /bm/companies payload cannot force is_demo=True.
F) Internal DB/ORM operation CAN persist is_demo=True (for future seeder).
G) Migration v016 DDL structure and startup safety verification.
"""
import os
import sys
import unittest

TEST_DB_NAME = "company_is_demo_test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_NAME}"
os.environ["APP_ENV"] = "test"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.users import User
from app.dependencies import get_current_user
from app.main import app


class TestCompanyIsDemo(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Engine URL points to production!"

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine) as db:
            # Seed 1: Regular normal company
            self.c_regular = Company(
                company_name="Regular Corp",
                company_key="regular-corp",
                is_active=True,
            )
            # Seed 2: Internal Demo company (created internally with is_demo=True)
            self.c_demo = Company(
                company_name="Empresa Demo",
                company_key="empresa-demo",
                is_active=True,
                is_demo=True,
            )
            db.add_all([self.c_regular, self.c_demo])
            await db.flush()
            self.regular_id = self.c_regular.company_id
            self.demo_id = self.c_demo.company_id

            # Seed super admin user for API calls
            self.admin_user = User(
                user_id=1,
                username="superadmin_test",
                email="superadmin@test.com",
                name="Super Admin",
                role="super_admin",
                company_id=self.regular_id,
                is_active=True,
                password_hash="dummy_hash",
            )
            db.add(self.admin_user)
            await db.commit()

        async def _override_user():
            eng = get_engine()
            async with AsyncSession(eng) as s:
                return await s.get(User, 1)

        app.dependency_overrides[get_current_user] = _override_user
        self.transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=self.transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def test_legacy_or_default_company_has_is_demo_false(self):
        """Test A: Legacy/existing company defaults to is_demo=False."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            stmt = select(Company).where(Company.company_id == self.regular_id)
            res = await db.execute(stmt)
            company = res.scalar()
            self.assertIsNotNone(company)
            self.assertFalse(company.is_demo)
            self.assertIs(company.is_demo, False)

    async def test_internal_layer_can_persist_is_demo_true(self):
        """Test F: Internal operation (ORM / seeder) can persist is_demo=True."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            stmt = select(Company).where(Company.company_id == self.demo_id)
            res = await db.execute(stmt)
            demo_company = res.scalar()
            self.assertIsNotNone(demo_company)
            self.assertTrue(demo_company.is_demo)
            self.assertIs(demo_company.is_demo, True)

    async def test_api_create_normal_company_yields_is_demo_false(self):
        """Test B & C: POST /bm/companies defaults to is_demo=False in response and in DB."""
        payload = {
            "company_name": "Hospital Norte",
            "company_key": "hospital-norte",
            "is_active": True,
        }
        resp = await self.client.post("/bm/companies", json=payload)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertIn("is_demo", data)
        self.assertFalse(data["is_demo"])

        # Check in DB
        engine = get_engine()
        async with AsyncSession(engine) as db:
            stmt = select(Company).where(Company.company_key == "hospital-norte")
            res = await db.execute(stmt)
            c = res.scalar()
            self.assertIsNotNone(c)
            self.assertFalse(c.is_demo)

    async def test_api_create_ignores_is_demo_in_payload(self):
        """Test E: External POST payload with is_demo=True cannot escalate to is_demo=True."""
        payload = {
            "company_name": "Malicious Demo Attempt",
            "company_key": "malicious-demo",
            "is_active": True,
            "is_demo": True,  # Extra/unauthorized field in payload
        }
        resp = await self.client.post("/bm/companies", json=payload)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertFalse(data["is_demo"])

        # Check in DB: must remain False
        engine = get_engine()
        async with AsyncSession(engine) as db:
            stmt = select(Company).where(Company.company_key == "malicious-demo")
            res = await db.execute(stmt)
            c = res.scalar()
            self.assertIsNotNone(c)
            self.assertFalse(c.is_demo)

    async def test_api_update_cannot_escalate_is_demo(self):
        """Test D.1: PATCH /bm/companies/{id} with is_demo=True cannot turn a normal company into demo."""
        payload = {"is_demo": True}
        resp = await self.client.patch(f"/bm/companies/{self.regular_id}", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["is_demo"])

        # Check in DB: must remain False
        engine = get_engine()
        async with AsyncSession(engine) as db:
            stmt = select(Company).where(Company.company_id == self.regular_id)
            res = await db.execute(stmt)
            c = res.scalar()
            self.assertFalse(c.is_demo)

    async def test_api_update_cannot_demote_is_demo(self):
        """Test D.2: PATCH /bm/companies/{id} with is_demo=False cannot turn a demo company into normal."""
        payload = {"is_demo": False}
        resp = await self.client.patch(f"/bm/companies/{self.demo_id}", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["is_demo"])

        # Check in DB: must remain True
        engine = get_engine()
        async with AsyncSession(engine) as db:
            stmt = select(Company).where(Company.company_id == self.demo_id)
            res = await db.execute(stmt)
            c = res.scalar()
            self.assertTrue(c.is_demo)

    async def test_api_get_and_list_expose_is_demo(self):
        """Test C: GET /bm/companies and GET /bm/companies/{id} expose is_demo accurately."""
        # 1. Detail
        r_detail = await self.client.get(f"/bm/companies/{self.demo_id}")
        self.assertEqual(r_detail.status_code, 200)
        self.assertTrue(r_detail.json()["is_demo"])

        r_reg_detail = await self.client.get(f"/bm/companies/{self.regular_id}")
        self.assertEqual(r_reg_detail.status_code, 200)
        self.assertFalse(r_reg_detail.json()["is_demo"])

        # 2. List
        r_list = await self.client.get("/bm/companies")
        self.assertEqual(r_list.status_code, 200)
        items = r_list.json()
        demo_item = next((x for x in items if x["company_id"] == self.demo_id), None)
        reg_item = next((x for x in items if x["company_id"] == self.regular_id), None)
        self.assertIsNotNone(demo_item)
        self.assertIsNotNone(reg_item)
        self.assertTrue(demo_item["is_demo"])
        self.assertFalse(reg_item["is_demo"])

    def test_migration_v016_sql_file_exists_and_valid(self):
        """Test G: Migration v016 exists and contains valid DDL statements."""
        mig_path = os.path.join("migrations", "v016_add_company_is_demo.sql")
        self.assertTrue(os.path.exists(mig_path), f"Migration file {mig_path} missing")
        with open(mig_path, "r", encoding="utf-8") as f:
            sql_text = f.read()
        self.assertIn("ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE", sql_text)
        self.assertIn("DROP COLUMN is_demo", sql_text)


if __name__ == "__main__":
    unittest.main()
