import os
import sys
import unittest
from datetime import datetime
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///multitenancy_test.db"

# Safety Confirmation Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution was blocked because DATABASE_URL points to production!")

# Setup path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite Type Compilers for Compatibility
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.services import Service
from app.core.roles import normalize_role, InternalRole
from app.core.tenant_context import TenantContext
from app.dependencies import get_current_user, get_db
from app.main import app

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class TestMultitenancyAuth(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        # Clean old DB file if exists
        if os.path.exists("multitenancy_test.db"):
            try:
                os.remove("multitenancy_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Populate test data
        self.session_factory = get_engine()
        async with AsyncSession(self.session_factory) as db:
            # 1. Companies
            self.c1 = Company(company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c2 = Company(company_name="GesDent", company_key="gesdent", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()

            # 2. Services
            self.s1 = Service(service_name="Front Desk", service_key="front", company_id=self.c1.company_id)
            self.s2 = Service(service_name="Experiencia Paciente", service_key="experiencia", company_id=self.c2.company_id)
            db.add_all([self.s1, self.s2])
            await db.flush()

            # 3. Teams
            self.t1 = Team(team_name="Equipo A", company_id=self.c1.company_id, service_id=self.s1.service_id)
            self.t2 = Team(team_name="Equipo B", company_id=self.c2.company_id, service_id=self.s2.service_id)
            db.add_all([self.t1, self.t2])
            await db.flush()

            # 4. Users
            # Super Admin (Legacy 'admin' role)
            self.u_super = User(username="super_admin", email="super@test.com", role="admin", password_hash="dummy")
            # Company Admin
            self.u_comp = User(username="company_admin", email="company@test.com", role="administrador", company_id=self.c1.company_id, password_hash="dummy")
            # Service Manager
            self.u_mgr = User(username="srv_manager", email="mgr@test.com", role="responsable_servicio", company_id=self.c1.company_id, password_hash="dummy")
            # Team Coordinator
            self.u_coor = User(username="team_coor", email="coor@test.com", role="coordinador_equipo", company_id=self.c1.company_id, password_hash="dummy")
            # Agent (Legacy 'agente' role)
            self.u_agent = User(username="agent_user", email="agent@test.com", role="agente", company_id=self.c1.company_id, hubspot_owner_id="owner_123", password_hash="dummy")

            db.add_all([self.u_super, self.u_comp, self.u_mgr, self.u_coor, self.u_agent])
            await db.flush()

            # 5. Associations
            # Service Manager assigned to Service 1 (Front)
            assoc_svc = UserServiceAssociation(user_id=self.u_mgr.user_id, service_id=self.s1.service_id)
            # Team Coordinator assigned to Team 1 (Equipo A)
            assoc_team = UserTeamAssociation(user_id=self.u_coor.user_id, team_id=self.t1.team_id)
            # Agent assigned to Team 1 (Equipo A)
            assoc_agent = AgentTeamAssociation(user_id=self.u_agent.user_id, team_id=self.t1.team_id)

            db.add_all([assoc_svc, assoc_team, assoc_agent])
            await db.flush()

            # Store IDs for tests before committing to avoid greenlet/lazy-loading issues
            self.super_id = self.u_super.user_id
            self.comp_id = self.u_comp.user_id
            self.mgr_id = self.u_mgr.user_id
            self.coor_id = self.u_coor.user_id
            self.agent_id = self.u_agent.user_id
            self.company1_id = self.c1.company_id
            self.company2_id = self.c2.company_id
            self.team1_id = self.t1.team_id
            self.team2_id = self.t2.team_id
            self.service1_id = self.s1.service_id
            self.service2_id = self.s2.service_id

            await db.commit()

    async def asyncTearDown(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        if os.path.exists("multitenancy_test.db"):
            try:
                os.remove("multitenancy_test.db")
            except Exception:
                pass
        app.dependency_overrides.clear()

    def test_role_normalization(self):
        """1. Test role normalization helper mappings."""
        self.assertEqual(normalize_role("admin"), InternalRole.SUPER_ADMIN)
        self.assertEqual(normalize_role("super_admin"), InternalRole.SUPER_ADMIN)
        self.assertEqual(normalize_role("administrador"), InternalRole.COMPANY_ADMIN)
        self.assertEqual(normalize_role("responsable_servicio"), InternalRole.SERVICE_MANAGER)
        self.assertEqual(normalize_role("coordinador_equipo"), InternalRole.TEAM_COORDINATOR)
        self.assertEqual(normalize_role("agente"), InternalRole.AGENT)
        self.assertEqual(normalize_role("agent"), InternalRole.AGENT)
        self.assertEqual(normalize_role("unknown"), InternalRole.AGENT)

    async def test_tenant_context_builder(self):
        """2. Test TenantContext property extraction per role."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            # Superadmin
            u_super = await db.get(User, self.super_id)
            context = await TenantContext.build(u_super, db)
            self.assertTrue(context.is_super_admin)
            self.assertIsNone(context.company_id)
            self.assertIn(self.company1_id, context.allowed_company_ids)
            self.assertIn(self.company2_id, context.allowed_company_ids)
            self.assertIsNone(context.allowed_service_ids) # Unrestricted

            # Company Admin
            u_comp = await db.get(User, self.comp_id)
            context = await TenantContext.build(u_comp, db)
            self.assertFalse(context.is_super_admin)
            self.assertEqual(context.company_id, self.company1_id)
            self.assertEqual(context.allowed_company_ids, [self.company1_id])
            self.assertIsNone(context.allowed_service_ids) # Unrestricted within company

            # Service Manager
            u_mgr = await db.get(User, self.mgr_id)
            context = await TenantContext.build(u_mgr, db)
            self.assertEqual(context.allowed_service_ids, [self.service1_id])
            self.assertEqual(context.allowed_team_ids, [self.team1_id])

            # Team Coordinator
            u_coor = await db.get(User, self.coor_id)
            context = await TenantContext.build(u_coor, db)
            self.assertEqual(context.allowed_team_ids, [self.team1_id])
            self.assertEqual(context.allowed_service_ids, [self.service1_id])

            # Agent
            u_agent = await db.get(User, self.agent_id)
            context = await TenantContext.build(u_agent, db)
            self.assertEqual(context.allowed_team_ids, [self.team1_id])
            self.assertEqual(context.allowed_agent_ids, ["owner_123"])

    async def test_tenant_context_endpoint(self):
        """3. Test /bm/me/tenant-context route."""
        # Setup Dependency Override
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_agent = await db.get(User, self.agent_id)

        app.dependency_overrides[get_current_user] = lambda: u_agent

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["user_id"], self.agent_id)
            self.assertEqual(data["normalized_role"], "agent")
            self.assertEqual(data["company_id"], self.company1_id)
            self.assertFalse(data["is_super_admin"])
            self.assertFalse(data["can_manage_companies"])
            self.assertFalse(data["can_manage_teams"])

    async def test_companies_endpoints_superadmin(self):
        """4. Test /bm/companies routes with Super Admin."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)

        app.dependency_overrides[get_current_user] = lambda: u_super

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # List all
            res = await ac.get("/bm/companies")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(len(res.json()), 2)

            # Create Company
            new_comp = {"company_name": "New Clinic", "company_key": "new-clinic", "is_active": True}
            res_post = await ac.post("/bm/companies", json=new_comp)
            self.assertEqual(res_post.status_code, 201)
            data_post = res_post.json()
            self.assertEqual(data_post["company_name"], "New Clinic")

            # Patch Company
            res_patch = await ac.patch(f"/bm/companies/{data_post['company_id']}", json={"company_name": "Clinic V2"})
            self.assertEqual(res_patch.status_code, 200)
            self.assertEqual(res_patch.json()["company_name"], "Clinic V2")

    async def test_companies_endpoints_agent_forbidden(self):
        """5. Test /bm/companies write routes are forbidden for Agent."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_agent = await db.get(User, self.agent_id)

        app.dependency_overrides[get_current_user] = lambda: u_agent

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_post = await ac.post("/bm/companies", json={"company_name": "Hack Clinic", "company_key": "hack-clinic"})
            self.assertEqual(res_post.status_code, 403)

    async def test_teams_crud_and_agents_association(self):
        """6. Test Team CRUD and agent associations."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)
            u_agent = await db.get(User, self.agent_id)

        app.dependency_overrides[get_current_user] = lambda: u_super

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Create Team
            new_team = {"team_name": "New Team A", "company_id": self.company1_id, "service_id": self.service1_id}
            res = await ac.post("/bm/teams", json=new_team)
            self.assertEqual(res.status_code, 201)
            team_data = res.json()
            self.assertEqual(team_data["team_name"], "New Team A")

            # Associate Agent to Team
            res_assoc = await ac.post(f"/bm/teams/{team_data['team_id']}/agents/{self.agent_id}")
            self.assertEqual(res_assoc.status_code, 201)

            # List agents of team
            res_list = await ac.get(f"/bm/teams/{team_data['team_id']}/agents")
            self.assertEqual(res_list.status_code, 200)
            agents = res_list.json()
            self.assertEqual(len(agents), 1)
            self.assertEqual(agents[0]["user_id"], self.agent_id)

            # Delete association
            res_del = await ac.delete(f"/bm/teams/{team_data['team_id']}/agents/{self.agent_id}")
            self.assertEqual(res_del.status_code, 200)

            # List agents of team (should be 0)
            res_list2 = await ac.get(f"/bm/teams/{team_data['team_id']}/agents")
            self.assertEqual(len(res_list2.json()), 0)
