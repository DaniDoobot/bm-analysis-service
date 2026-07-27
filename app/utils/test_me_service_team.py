"""Unit test suite for GET /bm/me and GET /bm/me/tenant-context service & team resolution."""
import os
import unittest
import httpx

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_me_service_team_db_test.db"

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
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.utils.security import create_access_token, hash_password
from sqlalchemy.ext.asyncio import AsyncSession


class TestMeServiceTeamResolutionApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            c10 = Company(company_id=10, company_name="Boston Medical", company_key="boston_medical", is_active=True)
            db.add(c10)
            await db.flush()

            s102 = Service(service_id=102, service_name="Experiencia de Paciente", service_key="exppa", company_id=10)
            s103 = Service(service_id=103, service_name="Ventas Especiales", service_key="ventas", company_id=10)
            db.add_all([s102, s103])
            await db.flush()

            t1002 = Team(team_id=1002, team_name="Equipo ExpPa Principal", company_id=10, service_id=102, is_active=True)
            t1003 = Team(team_id=1003, team_name="Equipo ExpPa Secundario", company_id=10, service_id=102, is_active=True)
            db.add_all([t1002, t1003])
            await db.flush()

            # Seed 5 users
            pass_hash = hash_password("pass123")
            u_super = User(user_id=1, username="superadmin", email="super@doobot.ai", role="super_admin", company_id=None, is_active=True, password_hash=pass_hash)
            u_cadmin = User(user_id=2, username="adminboston", email="admin@boston.es", role="company_admin", company_id=10, is_active=True, password_hash=pass_hash)
            u_smanager = User(user_id=3, username="juanjo", email="juanjo@boston.es", role="service_manager", company_id=10, is_active=True, password_hash=pass_hash)
            u_tcoord = User(user_id=4, username="jcerdan", email="jcerdan@boston.es", role="team_coordinator", company_id=10, is_active=True, password_hash=pass_hash)
            u_agent = User(user_id=5, username="mariaolvera", email="molvera@boston.es", role="agent", company_id=10, hubspot_owner_id="76997586", is_active=True, password_hash=pass_hash)

            db.add_all([u_super, u_cadmin, u_smanager, u_tcoord, u_agent])
            await db.flush()

            # Associations
            # agent María -> AgentTeamAssociation with t1002
            db.add(AgentTeamAssociation(user_id=5, team_id=1002))

            # team_coordinator jcerdan -> UserTeamAssociation with t1002 and t1003
            db.add_all([UserTeamAssociation(user_id=4, team_id=1002), UserTeamAssociation(user_id=4, team_id=1003)])

            # service_manager juanjo -> UserServiceAssociation with s102
            db.add(UserServiceAssociation(user_id=3, service_id=102))

            await db.commit()

        self.t_super = create_access_token({"sub": "superadmin", "user_id": 1, "role": "super_admin"})
        self.t_cadmin = create_access_token({"sub": "adminboston", "user_id": 2, "role": "company_admin", "company_id": 10})
        self.t_smanager = create_access_token({"sub": "juanjo", "user_id": 3, "role": "service_manager", "company_id": 10})
        self.t_tcoord = create_access_token({"sub": "jcerdan", "user_id": 4, "role": "team_coordinator", "company_id": 10})
        self.t_agent = create_access_token({"sub": "mariaolvera", "user_id": 5, "role": "agent", "company_id": 10})

        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_01_agent_me_endpoint_returns_service_and_team(self):
        """Verify agent (María Olvera) gets team and service info in GET /bm/me and /bm/me/tenant-context."""
        r = await self.client.get("/bm/me", headers={"Authorization": f"Bearer {self.t_agent}"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["primary_team_id"], 1002)
        self.assertEqual(data["primary_team_name"], "Equipo ExpPa Principal")
        self.assertEqual(data["primary_service_id"], 102)
        self.assertEqual(data["primary_service_name"], "Experiencia de Paciente")
        self.assertEqual(data["display_service_team"], "Experiencia de Paciente · Equipo ExpPa Principal")
        self.assertGreater(len(data["allowed_teams"]), 0)
        self.assertGreater(len(data["allowed_services"]), 0)

        # Check tenant-context
        r_ctx = await self.client.get("/bm/me/tenant-context", headers={"Authorization": f"Bearer {self.t_agent}"})
        self.assertEqual(r_ctx.status_code, 200)
        data_ctx = r_ctx.json()
        self.assertEqual(data_ctx["display_service_team"], "Experiencia de Paciente · Equipo ExpPa Principal")
        self.assertEqual(data_ctx["primary_team_name"], "Equipo ExpPa Principal")

    async def test_02_team_coordinator_multiple_teams(self):
        """Verify team coordinator returns coordinated teams and derived services."""
        r = await self.client.get("/bm/me", headers={"Authorization": f"Bearer {self.t_tcoord}"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["allowed_team_ids"]), 2)
        self.assertIn(1002, data["allowed_team_ids"])
        self.assertIn(1003, data["allowed_team_ids"])
        self.assertIsNotNone(data["display_service_team"])

    async def test_03_service_manager_services(self):
        """Verify service manager returns assigned service."""
        r = await self.client.get("/bm/me", headers={"Authorization": f"Bearer {self.t_smanager}"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["primary_service_id"], 102)
        self.assertEqual(data["primary_service_name"], "Experiencia de Paciente")
        self.assertIsNotNone(data["display_service_team"])

    async def test_04_company_admin(self):
        """Verify company admin does not crash and gets company display_service_team."""
        r = await self.client.get("/bm/me", headers={"Authorization": f"Bearer {self.t_cadmin}"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["company_name"], "Boston Medical")
        self.assertIsNotNone(data["display_service_team"])

    async def test_05_super_admin_global_context(self):
        """Verify super admin returns global display_service_team."""
        r = await self.client.get("/bm/me", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["display_service_team"], "Doobot.ai_ Global")


if __name__ == "__main__":
    unittest.main()
