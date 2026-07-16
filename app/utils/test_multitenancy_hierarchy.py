import os
import sys
import unittest
from datetime import datetime
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///multitenancy_hierarchy_test.db"

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


class TestMultitenancyHierarchy(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        # Clean old DB file if exists
        if os.path.exists("multitenancy_hierarchy_test.db"):
            try:
                os.remove("multitenancy_hierarchy_test.db")
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

            # Save IDs
            self.company1_id = self.c1.company_id
            self.company2_id = self.c2.company_id

            # 2. Services
            self.s1 = Service(service_name="Front Desk", service_key="front", company_id=self.company1_id)
            self.s2 = Service(service_name="Experiencia Paciente", service_key="experiencia", company_id=self.company2_id)
            db.add_all([self.s1, self.s2])
            await db.flush()

            self.service1_id = self.s1.service_id
            self.service2_id = self.s2.service_id

            # 3. Teams
            self.t1 = Team(team_name="Equipo A", company_id=self.company1_id, service_id=self.service1_id)
            self.t2 = Team(team_name="Equipo B", company_id=self.company2_id, service_id=self.service2_id)
            db.add_all([self.t1, self.t2])
            await db.flush()

            self.team1_id = self.t1.team_id
            self.team2_id = self.t2.team_id

            # 4. Users
            # Super Admin
            self.u_super = User(username="super_admin", email="super@test.com", role="administrador", password_hash="dummy")
            # Company Admin
            self.u_comp = User(username="company_admin", email="company@test.com", role="company_admin", company_id=self.company1_id, password_hash="dummy")
            # Service Manager
            self.u_mgr = User(username="srv_manager", email="mgr@test.com", role="responsable_servicio", company_id=self.company1_id, password_hash="dummy")
            # Team Coordinator
            self.u_coor = User(username="team_coor", email="coor@test.com", role="coordinador_equipo", company_id=self.company1_id, password_hash="dummy")
            # Agent
            self.u_agent = User(username="agent_user", email="agent@test.com", role="agente", company_id=self.company1_id, hubspot_owner_id="owner_123", password_hash="dummy")

            db.add_all([self.u_super, self.u_comp, self.u_mgr, self.u_coor, self.u_agent])
            await db.flush()

            self.super_id = self.u_super.user_id
            self.comp_id = self.u_comp.user_id
            self.mgr_id = self.u_mgr.user_id
            self.coor_id = self.u_coor.user_id
            self.agent_id = self.u_agent.user_id

            # 5. Associations
            assoc_svc = UserServiceAssociation(user_id=self.mgr_id, service_id=self.service1_id)
            assoc_team = UserTeamAssociation(user_id=self.coor_id, team_id=self.team1_id)
            assoc_agent = AgentTeamAssociation(user_id=self.agent_id, team_id=self.team1_id)

            db.add_all([assoc_svc, assoc_team, assoc_agent])
            await db.commit()

    async def asyncTearDown(self):
        # Reset dependency overrides
        app.dependency_overrides.clear()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        # Clean DB file
        if os.path.exists("multitenancy_hierarchy_test.db"):
            try:
                os.remove("multitenancy_hierarchy_test.db")
            except Exception:
                pass

    def test_role_normalization(self):
        """1. Validate normalization of Raw legacy roles into canonical internal roles."""
        self.assertEqual(normalize_role("administrador"), InternalRole.SUPER_ADMIN)
        self.assertEqual(normalize_role("admin"), InternalRole.SUPER_ADMIN)
        self.assertEqual(normalize_role("company_admin"), InternalRole.COMPANY_ADMIN)
        self.assertEqual(normalize_role("responsable_servicio"), InternalRole.SERVICE_MANAGER)
        self.assertEqual(normalize_role("coordinador_equipo"), InternalRole.TEAM_COORDINATOR)
        self.assertEqual(normalize_role("agente"), InternalRole.AGENT)

    async def test_tenant_context_flags(self):
        """2. Test that get_my_tenant_context returns can_manage_users accurately."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_comp = await db.get(User, self.comp_id)
            u_mgr = await db.get(User, self.mgr_id)

        # A. Company Admin
        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["can_manage_users"])
            self.assertTrue(data["can_manage_company"])
            self.assertFalse(data["can_manage_companies"])

        # B. Service Manager
        app.dependency_overrides[get_current_user] = lambda: u_mgr
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertFalse(data["can_manage_users"])
            self.assertTrue(data["can_manage_services"])

    async def test_services_endpoints_multiempresa(self):
        """3. Test GET, POST, PATCH /bm/services endpoints."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)
            u_comp = await db.get(User, self.comp_id)

        # A. GET list - Company Admin should only see their company's services
        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/services")
            self.assertEqual(res.status_code, 200)
            services = res.json()
            self.assertEqual(len(services), 1)
            self.assertEqual(services[0]["company_id"], self.company1_id)

            # Test query params filter cross-company should be forbidden for company admin
            res_err = await ac.get(f"/bm/services?company_id={self.company2_id}")
            self.assertEqual(res_err.status_code, 403)

        # B. POST Create - Company Admin can create inside their company
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            new_svc = {
                "service_name": "New Svc",
                "service_key": "new-svc-slug",
                "company_id": self.company1_id,
                "is_active": True
            }
            res_post = await ac.post("/bm/services", json=new_svc)
            self.assertEqual(res_post.status_code, 201)
            data = res_post.json()
            new_service_id = data["service_id"]
            self.assertEqual(data["service_key"], "new-svc-slug")

            # Try to create with an existing key (unique constraint)
            res_post_dup = await ac.post("/bm/services", json=new_svc)
            self.assertEqual(res_post_dup.status_code, 400)

            # Try to create in another company (GesDent) -> Forbidden
            bad_svc = {
                "service_name": "Bad Svc",
                "service_key": "bad-svc-slug",
                "company_id": self.company2_id,
                "is_active": True
            }
            res_post_bad = await ac.post("/bm/services", json=bad_svc)
            self.assertEqual(res_post_bad.status_code, 403)

        # C. PATCH Update
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Edit partial description/name
            res_patch = await ac.patch(f"/bm/services/{new_service_id}", json={"description": "Updated Svc Desc"})
            self.assertEqual(res_patch.status_code, 200)
            self.assertEqual(res_patch.json()["description"], "Updated Svc Desc")

            # Try to change company -> Forbidden
            res_patch_co = await ac.patch(f"/bm/services/{new_service_id}", json={"company_id": self.company2_id})
            self.assertEqual(res_patch_co.status_code, 403)

    async def test_admin_users_management_endpoints(self):
        """4. Test User list and editing routes under /bm/admin/users."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)
            u_comp = await db.get(User, self.comp_id)

        # A. GET list - Company Admin should only see users of their company
        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/admin/users")
            self.assertEqual(res.status_code, 200)
            users_list = res.json()
            # Users in company 1: company_admin, srv_manager, team_coor, agent_user (4 users)
            self.assertEqual(len(users_list), 4)
            for u in users_list:
                self.assertEqual(u["company_id"], self.company1_id)
                self.assertNotIn("password_hash", u) # safe payload

            # Trying to query company 2 -> Forbidden
            res_err = await ac.get(f"/bm/admin/users?company_id={self.company2_id}")
            self.assertEqual(res_err.status_code, 403)

        # B. PATCH User
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Update name of coordinator
            res_patch = await ac.patch(f"/bm/admin/users/{self.coor_id}", json={"name": "New Coordinator Name"})
            self.assertEqual(res_patch.status_code, 200)
            self.assertEqual(res_patch.json()["name"], "New Coordinator Name")

            # Try to promote anyone to super_admin -> Forbidden for company_admin
            res_bad_role = await ac.patch(f"/bm/admin/users/{self.coor_id}", json={"role": "administrador"})
            self.assertEqual(res_bad_role.status_code, 403)

            # Try to change user company -> Forbidden for company_admin
            res_bad_comp = await ac.patch(f"/bm/admin/users/{self.coor_id}", json={"company_id": self.company2_id})
            self.assertEqual(res_bad_comp.status_code, 403)

            # Try to demote self -> Forbidden
            res_self_demote = await ac.patch(f"/bm/admin/users/{self.comp_id}", json={"role": "agente"})
            self.assertEqual(res_self_demote.status_code, 403)

    async def test_admin_user_service_assignments(self):
        """5. Test UserService assignments."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_comp = await db.get(User, self.comp_id)

        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # A. GET list - coordinator has no services currently
            res_get = await ac.get(f"/bm/admin/users/{self.coor_id}/services")
            self.assertEqual(res_get.status_code, 200)
            self.assertEqual(len(res_get.json()), 0)

            # B. POST assign service 1 (same company)
            res_post = await ac.post(f"/bm/admin/users/{self.coor_id}/services/{self.service1_id}")
            self.assertEqual(res_post.status_code, 201)

            # Check now assigned
            res_get2 = await ac.get(f"/bm/admin/users/{self.coor_id}/services")
            self.assertEqual(len(res_get2.json()), 1)
            self.assertEqual(res_get2.json()[0]["service_id"], self.service1_id)

            # C. POST assign service 2 (cross-company) -> Forbidden
            res_post_bad = await ac.post(f"/bm/admin/users/{self.coor_id}/services/{self.service2_id}")
            self.assertEqual(res_post_bad.status_code, 403)

            # D. DELETE unassign
            res_del = await ac.delete(f"/bm/admin/users/{self.coor_id}/services/{self.service1_id}")
            self.assertEqual(res_del.status_code, 200)

            res_get3 = await ac.get(f"/bm/admin/users/{self.coor_id}/services")
            self.assertEqual(len(res_get3.json()), 0)

    async def test_admin_user_team_assignments(self):
        """6. Test UserTeam assignments (coordinators)."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_comp = await db.get(User, self.comp_id)

        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # A. GET list - coordinator is already assigned to team1 in setup
            res_get = await ac.get(f"/bm/admin/users/{self.coor_id}/teams")
            self.assertEqual(res_get.status_code, 200)
            self.assertEqual(len(res_get.json()), 1)
            self.assertEqual(res_get.json()[0]["team_id"], self.team1_id)

            # B. POST assign team 2 (cross-company) -> Forbidden
            res_post_bad = await ac.post(f"/bm/admin/users/{self.coor_id}/teams/{self.team2_id}")
            self.assertEqual(res_post_bad.status_code, 403)

            # C. DELETE unassign team 1
            res_del = await ac.delete(f"/bm/admin/users/{self.coor_id}/teams/{self.team1_id}")
            self.assertEqual(res_del.status_code, 200)

            res_get2 = await ac.get(f"/bm/admin/users/{self.coor_id}/teams")
            self.assertEqual(len(res_get2.json()), 0)

    async def test_role_options(self):
        """7. Test Role Options retrieval and company admin filtering."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)
            u_comp = await db.get(User, self.comp_id)

        # Super admin gets all options
        app.dependency_overrides[get_current_user] = lambda: u_super
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/admin/users/role-options")
            self.assertEqual(res.status_code, 200)
            opts = res.json()
            values = [o["value"] for o in opts]
            self.assertIn("super_admin", values)
            self.assertIn("company_admin", values)
            self.assertIn("agente", values)

        # Company admin gets options except super_admin
        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/admin/users/role-options")
            self.assertEqual(res.status_code, 200)
            opts = res.json()
            values = [o["value"] for o in opts]
            self.assertNotIn("super_admin", values)
            self.assertIn("company_admin", values)
            self.assertIn("agente", values)

    async def test_create_user_success_and_restrictions(self):
        """8. Test creation of users with hierarchical roles, uniqueness checks and permission scopes."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)
            u_comp = await db.get(User, self.comp_id)

        # A. Super admin creates super_admin (company_id forced to None)
        app.dependency_overrides[get_current_user] = lambda: u_super
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            payload = {
                "username": "new_super",
                "email": "new_super@test.com",
                "role": "super_admin",
                "company_id": 99,  # Should be ignored and set to None
                "name": "New Super Admin"
            }
            res = await ac.post("/bm/admin/users", json=payload)
            self.assertEqual(res.status_code, 201)
            user_data = res.json()
            self.assertIsNone(user_data["company_id"])
            self.assertEqual(user_data["username"], "new_super")
            self.assertNotIn("password_hash", user_data)

            # B. Super admin creates company_admin
            payload = {
                "username": "new_comp_admin",
                "email": "new_comp_admin@test.com",
                "role": "company_admin",
                "company_id": self.company1_id
            }
            res = await ac.post("/bm/admin/users", json=payload)
            self.assertEqual(res.status_code, 201)
            self.assertEqual(res.json()["company_id"], self.company1_id)

            # C. Non-superadmin role requires company_id
            payload_bad_comp = {
                "username": "no_comp",
                "email": "nocomp@test.com",
                "role": "agente"
            }
            res_bad = await ac.post("/bm/admin/users", json=payload_bad_comp)
            self.assertEqual(res_bad.status_code, 400)
            self.assertIn("company_id", res_bad.json()["detail"])

        # D. Company admin restrictions
        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Cannot create super_admin
            payload = {
                "username": "comp_creating_super",
                "email": "comp_super@test.com",
                "role": "super_admin"
            }
            res = await ac.post("/bm/admin/users", json=payload)
            self.assertEqual(res.status_code, 403)

            # Cannot create user in company 2
            payload = {
                "username": "comp_creating_gesdent",
                "email": "comp_gesdent@test.com",
                "role": "agente",
                "company_id": self.company2_id
            }
            res = await ac.post("/bm/admin/users", json=payload)
            self.assertEqual(res.status_code, 403)

            # Can create agente in company 1
            payload = {
                "username": "new_agent_ok",
                "email": "agent_ok@test.com",
                "role": "agente",
                "company_id": self.company1_id
            }
            res = await ac.post("/bm/admin/users", json=payload)
            self.assertEqual(res.status_code, 201)

            # Email uniqueness check
            payload_dup_email = {
                "username": "another_uname",
                "email": "agent_ok@test.com",
                "role": "agente",
                "company_id": self.company1_id
            }
            res_dup = await ac.post("/bm/admin/users", json=payload_dup_email)
            self.assertEqual(res_dup.status_code, 400)
            self.assertIn("ya está en uso", res_dup.json()["detail"])

    async def test_patch_user_transitions(self):
        """9. Test transition validations and cleanup during PATCH update."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)
            u_agent = await db.get(User, self.agent_id)

        app.dependency_overrides[get_current_user] = lambda: u_super
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # A. Change agent to super_admin (should automatically clear company_id)
            res = await ac.patch(f"/bm/admin/users/{self.agent_id}", json={"role": "super_admin"})
            self.assertEqual(res.status_code, 200)
            self.assertIsNone(res.json()["company_id"])

            # B. Change super_admin back to agent (requires company_id)
            res_bad = await ac.patch(f"/bm/admin/users/{self.agent_id}", json={"role": "agente"})
            self.assertEqual(res_bad.status_code, 400)
            self.assertIn("company_id", res_bad.json()["detail"])

            # C. Now supply company_id -> should succeed
            res_ok = await ac.patch(f"/bm/admin/users/{self.agent_id}", json={"role": "agente", "company_id": self.company1_id})
            self.assertEqual(res_ok.status_code, 200)
            self.assertEqual(res_ok.json()["company_id"], self.company1_id)

            # D. Degrade/deactivate the last active super_admin is blocked
            res_degrade = await ac.patch(f"/bm/admin/users/{self.super_id}", json={"role": "agente", "company_id": self.company1_id})
            self.assertEqual(res_degrade.status_code, 400)
            self.assertIn("Super Administrador activo", res_degrade.json()["detail"])


if __name__ == "__main__":
    unittest.main()
