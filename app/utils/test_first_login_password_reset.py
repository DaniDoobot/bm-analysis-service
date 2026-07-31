"""
Unit test suite for first login with provisional password and endpoint restriction.
"""
import os
import unittest
import httpx

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_first_login_db_test.db"

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
from app.main import app
from app.db import Base, get_engine
from app.models.users import User
from app.models.companies import Company
from app.utils.security import hash_password
from sqlalchemy.ext.asyncio import AsyncSession


class TestFirstLoginPasswordReset(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine, expire_on_commit=False) as session:
            company = Company(
                company_id=1,
                company_name="Test Company",
                company_key="test_company",
                is_active=True
            )
            session.add(company)

            user = User(
                user_id=10,
                username="provisional_user",
                email="provisional@example.com",
                password_hash=hash_password("Provisional123!"),
                role="company_admin",
                company_id=1,
                is_active=True,
                must_reset_password=True
            )
            session.add(user)
            await session.commit()

        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        if os.path.exists("./test_first_login_db_test.db"):
            try:
                os.remove("./test_first_login_db_test.db")
            except Exception:
                pass

    async def test_provisional_login_and_endpoint_restrictions(self):
        # 1. Login with wrong password -> 401
        resp_wrong = await self.client.post("/bm/auth/login", json={
            "username": "provisional_user",
            "password": "WrongPassword!"
        })
        self.assertEqual(resp_wrong.status_code, 401)

        # 2. Login with valid provisional password -> 200 OK + token + reset flags
        resp = await self.client.post("/bm/auth/login", json={
            "username": "provisional_user",
            "password": "Provisional123!"
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("access_token", data)
        self.assertTrue(data.get("must_reset_password"))
        self.assertTrue(data.get("requires_password_change"))
        self.assertTrue(data.get("password_change_required"))

        token = data["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 3. Access allowed endpoint /bm/me -> 200 OK
        resp_me = await self.client.get("/bm/me", headers=headers)
        self.assertEqual(resp_me.status_code, 200)

        # 4. Access restricted operational endpoint /bm/companies -> 403 Forbidden with password_change_required
        resp_comp = await self.client.get("/bm/companies", headers=headers)
        self.assertEqual(resp_comp.status_code, 403)
        self.assertEqual(resp_comp.json().get("detail"), "password_change_required")

        # 5. Execute self change password -> 200 OK
        resp_change = await self.client.post("/bm/me/change-password", headers=headers, json={
            "current_password": "Provisional123!",
            "new_password": "NewSecurePassword123!"
        })
        self.assertEqual(resp_change.status_code, 200)
        self.assertTrue(resp_change.json().get("ok"))

        # 6. Now operational endpoint /bm/companies is accessible!
        resp_comp_after = await self.client.get("/bm/companies", headers=headers)
        self.assertEqual(resp_comp_after.status_code, 200)


if __name__ == "__main__":
    unittest.main()
