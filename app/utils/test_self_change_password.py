"""Unit test suite for POST /bm/me/change-password endpoint."""
import os
import sys
import unittest
import httpx

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_self_change_password_db_test.db"

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.utils.security import create_access_token, hash_password
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class TestSelfChangePasswordApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            c10 = Company(company_id=10, company_name="Boston Medical", company_key="boston_medical", is_active=True)
            db.add(c10)
            await db.flush()

            s102 = Service(service_id=102, service_name="Experiencia Paciente", service_key="exppa", company_id=10)
            db.add(s102)
            await db.flush()

            # Seed 5 users for each role with password 'initial_pass_123'
            u_super = User(user_id=1, username="superadmin", email="super@doobot.ai", role="super_admin", company_id=None, is_active=True, password_hash=hash_password("initial_pass_123"), must_reset_password=True)
            u_cadmin = User(user_id=2, username="adminboston", email="admin@boston.es", role="company_admin", company_id=10, is_active=True, password_hash=hash_password("initial_pass_123"), must_reset_password=True)
            u_smanager = User(user_id=3, username="juanjo", email="juanjo@boston.es", role="service_manager", company_id=10, is_active=True, password_hash=hash_password("initial_pass_123"))
            u_tcoord = User(user_id=4, username="jcerdan", email="jcerdan@boston.es", role="team_coordinator", company_id=10, is_active=True, password_hash=hash_password("initial_pass_123"))
            u_agent = User(user_id=5, username="victoria", email="victoria@boston.es", role="agent", company_id=10, hubspot_owner_id="31499194", is_active=True, password_hash=hash_password("initial_pass_123"), must_reset_password=True, reset_token="old_reset_token")

            db.add_all([u_super, u_cadmin, u_smanager, u_tcoord, u_agent])
            await db.commit()

        self.t_super = create_access_token({"sub": "superadmin", "user_id": 1, "role": "super_admin"})
        self.t_cadmin = create_access_token({"sub": "adminboston", "user_id": 2, "role": "company_admin", "company_id": 10})
        self.t_smanager = create_access_token({"sub": "juanjo", "user_id": 3, "role": "service_manager", "company_id": 10})
        self.t_tcoord = create_access_token({"sub": "jcerdan", "user_id": 4, "role": "team_coordinator", "company_id": 10})
        self.t_agent = create_access_token({"sub": "victoria", "user_id": 5, "role": "agent", "company_id": 10})

        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_01_agent_change_own_password_success(self):
        """Verify agent changes own password with correct current password."""
        # 1. Change password
        r = await self.client.post("/bm/me/change-password", json={
            "current_password": "initial_pass_123",
            "new_password": "new_secure_password_456"
        }, headers={"Authorization": f"Bearer {self.t_agent}"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok"))

        # 2. Check DB state: must_reset_password=False, reset_token=None, password_set_at informed
        engine = get_engine()
        async with AsyncSession(engine) as db:
            res = await db.execute(select(User).where(User.user_id == 5))
            u = res.scalar()
            self.assertFalse(u.must_reset_password)
            self.assertIsNotNone(u.password_set_at)
            self.assertIsNone(u.reset_token)

        # 3. Old password login fails
        r_old = await self.client.post("/bm/auth/login", json={
            "username": "victoria",
            "password": "initial_pass_123"
        })
        self.assertIn(r_old.status_code, (400, 401))

        # 4. New password login succeeds
        r_new = await self.client.post("/bm/auth/login", json={
            "username": "victoria",
            "password": "new_secure_password_456"
        })
        self.assertEqual(r_new.status_code, 200)
        self.assertIn("access_token", r_new.json())

    async def test_02_incorrect_current_password(self):
        """Verify change-password fails with incorrect current_password."""
        r = await self.client.post("/bm/me/change-password", json={
            "current_password": "wrong_password_999",
            "new_password": "new_secure_password_456"
        }, headers={"Authorization": f"Bearer {self.t_agent}"})
        self.assertIn(r.status_code, (400, 401))
        self.assertIn("incorrecta", r.json()["detail"].lower())

    async def test_03_same_new_password_fails(self):
        """Verify new_password equal to current_password fails."""
        r = await self.client.post("/bm/me/change-password", json={
            "current_password": "initial_pass_123",
            "new_password": "initial_pass_123"
        }, headers={"Authorization": f"Bearer {self.t_agent}"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("igual", r.json()["detail"].lower())

    async def test_04_short_new_password_fails(self):
        """Verify new_password shorter than 8 characters fails validation."""
        r = await self.client.post("/bm/me/change-password", json={
            "current_password": "initial_pass_123",
            "new_password": "short"
        }, headers={"Authorization": f"Bearer {self.t_agent}"})
        self.assertEqual(r.status_code, 422)

    async def test_05_all_roles_can_change_own_password(self):
        """Verify all 5 roles can change their own password without admin privileges."""
        for name, token in [
            ("super_admin", self.t_super),
            ("company_admin", self.t_cadmin),
            ("service_manager", self.t_smanager),
            ("team_coordinator", self.t_tcoord),
        ]:
            r = await self.client.post("/bm/me/change-password", json={
                "current_password": "initial_pass_123",
                "new_password": f"new_pass_for_{name}_789"
            }, headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(r.status_code, 200, f"Failed for role {name}")
            self.assertTrue(r.json().get("ok"))


if __name__ == "__main__":
    unittest.main()
