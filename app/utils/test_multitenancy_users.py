import os
import sys
import unittest
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///multitenancy_users_test.db"

# Safety Confirmation Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution was blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite Type Compilers for Compatibility
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine, Base
from app.models.companies import Company
from app.models.users import User
from app.core.roles import normalize_role, InternalRole
from app.core.tenant_context import TenantContext
from app.dependencies import get_current_user, get_tenant_context, get_db
from app.main import app
from app.utils.security import hash_password
from sqlalchemy.ext.asyncio import AsyncSession


class TestMultitenancyUsers(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("multitenancy_users_test.db"):
            try:
                os.remove("multitenancy_users_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine) as db:
            await db.execute(delete(User))
            await db.commit()

            c1 = (await db.execute(select(Company).where(Company.company_id == 1))).scalars().first()
            if not c1:
                c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston")
                db.add(c1)
            c2 = (await db.execute(select(Company).where(Company.company_id == 2))).scalars().first()
            if not c2:
                c2 = Company(company_id=2, company_name="Clinica Madrid", company_key="madrid")
                db.add(c2)
            await db.flush()

            from app.models.services import Service
            s1 = (await db.execute(select(Service).where(Service.service_id == 1))).scalars().first()
            if not s1:
                s1 = Service(service_id=1, company_id=1, service_key="front_boston", service_name="Front Boston")
                db.add(s1)
            s2 = (await db.execute(select(Service).where(Service.service_id == 2))).scalars().first()
            if not s2:
                s2 = Service(service_id=2, company_id=1, service_key="back_boston", service_name="Back Boston")
                db.add(s2)
            s3 = (await db.execute(select(Service).where(Service.service_id == 3))).scalars().first()
            if not s3:
                s3 = Service(service_id=3, company_id=2, service_key="front_madrid", service_name="Front Madrid")
                db.add(s3)

            pass_h = hash_password("pass123")

            # Super admin
            u_super = User(
                username="superadmin_test", email="super_test@test.com",
                role="super_admin", company_id=None, password_hash=pass_h, is_active=True
            )
            # Company admin Boston Medical
            u_admin_boston = User(
                username="adminboston_test", email="adminboston_test@test.com",
                role="Administrador de empresa", company_id=1, password_hash=pass_h, is_active=True
            )
            # Agent Boston Medical
            u_agent_boston = User(
                username="agentboston_test", email="agentboston_test@test.com",
                role="agente", company_id=1, hubspot_owner_id="901", password_hash=pass_h, is_active=True
            )
            # Company admin Madrid
            u_admin_madrid = User(
                username="adminmadrid_test", email="adminmadrid_test@test.com",
                role="company_admin", company_id=2, password_hash=pass_h, is_active=True
            )
            # Agent Madrid
            u_agent_madrid = User(
                username="agentmadrid_test", email="agentmadrid_test@test.com",
                role="agente", company_id=2, hubspot_owner_id="902", password_hash=pass_h, is_active=True
            )

            db.add_all([u_super, u_admin_boston, u_agent_boston, u_admin_madrid, u_agent_madrid])
            await db.commit()
            await db.refresh(u_super)
            await db.refresh(u_admin_boston)
            await db.refresh(u_agent_boston)
            await db.refresh(u_admin_madrid)
            await db.refresh(u_agent_madrid)

            self.u_super = u_super
            self.u_admin_boston = u_admin_boston
            self.u_agent_boston = u_agent_boston
            self.u_admin_madrid = u_admin_madrid
            self.u_agent_madrid = u_agent_madrid

    async def asyncTearDown(self):
        app.dependency_overrides.clear()

    async def test_login_company_admin_returns_normalized_role_and_company_info(self):
        """Verify POST /bm/auth/login for company_admin succeeds without MissingGreenlet and returns company info."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/auth/login", json={"username": "adminboston_test@test.com", "password": "pass123"})
            self.assertEqual(res.status_code, 200, msg=f"Login failed: {res.text}")
            data = res.json()
            self.assertTrue(data.get("ok"))
            user_data = data.get("user", {})
            self.assertEqual(user_data.get("username"), "adminboston_test")
            self.assertEqual(user_data.get("normalized_role"), "company_admin")
            self.assertEqual(user_data.get("company_id"), 1)
            self.assertEqual(user_data.get("company_name"), "Boston Medical")

    async def test_login_super_admin_returns_null_company_info(self):
        """Verify POST /bm/auth/login for global super_admin succeeds with null company info."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/auth/login", json={"username": "super_test@test.com", "password": "pass123"})
            self.assertEqual(res.status_code, 200, msg=f"Login failed: {res.text}")
            data = res.json()
            self.assertTrue(data.get("ok"))
            user_data = data.get("user", {})
            self.assertEqual(user_data.get("username"), "superadmin_test")
            self.assertEqual(user_data.get("normalized_role"), "super_admin")
            self.assertIsNone(user_data.get("company_id"))
            self.assertIsNone(user_data.get("company_name"))

    async def test_me_endpoint_returns_correct_role_for_company_admin(self):
        """1. Verify GET /bm/me returns normalized_role=company_admin and company info for adminboston."""
        async with AsyncSession(get_engine()) as db:
            user = await db.get(User, self.u_admin_boston.user_id)
            ctx = await TenantContext.build(user, db)

        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/me")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["username"], "adminboston_test")
            self.assertEqual(data["normalized_role"], "company_admin")
            self.assertEqual(data["company_id"], 1)
            self.assertEqual(data["company_name"], "Boston Medical")

    async def test_tenant_context_endpoint_returns_can_manage_users(self):
        """2. Verify GET /bm/me/tenant-context returns can_manage_users=True for company_admin."""
        async with AsyncSession(get_engine()) as db:
            user = await db.get(User, self.u_admin_boston.user_id)
            ctx = await TenantContext.build(user, db)

        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["can_manage_users"])
            self.assertEqual(data["normalized_role"], "company_admin")

    async def test_company_admin_user_listing_scoping(self):
        """3. Verify GET /bm/users for company_admin only returns users of their company, excluding superadmins."""
        async with AsyncSession(get_engine()) as db:
            user = await db.get(User, self.u_admin_boston.user_id)
            ctx = await TenantContext.build(user, db)

        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/users")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            user_list = data["users"]
            usernames = [u["username"] for u in user_list]

            self.assertIn("adminboston_test", usernames)
            self.assertIn("agentboston_test", usernames)
            self.assertNotIn("superadmin_test", usernames)
            self.assertNotIn("adminmadrid_test", usernames)
            self.assertNotIn("agentmadrid_test", usernames)

    async def test_super_admin_user_listing_sees_all(self):
        """4. Verify GET /bm/users for super_admin sees all users across companies."""
        async with AsyncSession(get_engine()) as db:
            user = await db.get(User, self.u_super.user_id)
            ctx = await TenantContext.build(user, db)

        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/users")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            user_list = data["users"]
            usernames = [u["username"] for u in user_list]

            self.assertIn("superadmin_test", usernames)
            self.assertIn("adminboston_test", usernames)
            self.assertIn("agentboston_test", usernames)
            self.assertIn("adminmadrid_test", usernames)
            self.assertIn("agentmadrid_test", usernames)

    async def test_company_admin_user_creation_restrictions(self):
        """5. Verify company_admin user creation rules (own company only, cannot create super_admin)."""
        async with AsyncSession(get_engine()) as db:
            user = await db.get(User, self.u_admin_boston.user_id)
            ctx = await TenantContext.build(user, db)

        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 5a. Successfully create an agent in Boston Medical
            payload_valid = {
                "email": "newagent@boston.com",
                "username": "newagentboston",
                "role": "agente",
                "hubspot_owner_id": "999",
                "must_reset_password": True,
                "password_setup": "invite_link"
            }
            res = await ac.post("/bm/users?allow_unverified_hubspot_id=true", json=payload_valid)
            self.assertEqual(res.status_code, 201, msg=f"Error response: {res.text}")
            created_data = res.json()["user"]
            self.assertEqual(created_data["company_id"], 1)

            # 5b. Attempt to create super_admin -> 403 Forbidden
            payload_super = {
                "email": "rogue@super.com",
                "username": "roguesuper",
                "role": "super_admin",
                "password_setup": "invite_link"
            }
            res_super = await ac.post("/bm/users", json=payload_super)
            self.assertEqual(res_super.status_code, 403)

            # 5c. Attempt to create user for company 2 -> 403 Forbidden
            payload_other = {
                "email": "rogue@madrid.com",
                "username": "roguemadrid",
                "role": "agente",
                "company_id": 2,
                "password_setup": "invite_link"
            }
            res_other = await ac.post("/bm/users", json=payload_other)
            self.assertEqual(res_other.status_code, 403)

    async def test_company_admin_user_edition_restrictions(self):
        """6. Verify company_admin user editing restrictions."""
        async with AsyncSession(get_engine()) as db:
            user = await db.get(User, self.u_admin_boston.user_id)
            ctx = await TenantContext.build(user, db)

        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 6a. Edit user in own company (agentboston_test)
            res_edit = await ac.patch(f"/bm/users/{self.u_agent_boston.user_id}?allow_unverified_hubspot_id=true", json={"name": "Agent Boston Updated"})
            self.assertEqual(res_edit.status_code, 200)
            self.assertEqual(res_edit.json()["user"]["name"], "Agent Boston Updated")

            # 6b. Attempt to edit user in another company (agentmadrid_test) -> 403 Forbidden
            res_other = await ac.patch(f"/bm/users/{self.u_agent_madrid.user_id}", json={"name": "Agent Madrid Hack"})
            self.assertEqual(res_other.status_code, 403)

            # 6c. Attempt to promote user to super_admin -> 403 Forbidden
            res_promote = await ac.patch(f"/bm/users/{self.u_agent_boston.user_id}", json={"role": "super_admin"})
            self.assertEqual(res_promote.status_code, 403)

    async def test_service_manager_service_assignment_validation(self):
        """7. Test creating/updating service_manager requires primary_service_id and enforces company scoping."""
        async with AsyncSession(get_engine()) as db:
            user = await db.get(User, self.u_admin_boston.user_id)
            ctx = await TenantContext.build(user, db)

        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 7a. Creation of service_manager without explicit primary_service_id falls back to company service (service 1) -> 201 Created
            payload_no_svc = {
                "email": "mgr_nosvc@boston.com",
                "username": "mgr_nosvc",
                "role": "responsable_servicio",
                "password_setup": "invite_link"
            }
            res_no_svc = await ac.post("/bm/users", json=payload_no_svc)
            self.assertEqual(res_no_svc.status_code, 201, msg=res_no_svc.text)
            self.assertEqual(res_no_svc.json()["user"]["primary_service_id"], 1)

            # 7b. Fail creation with service belonging to another company (service_id=3 is Madrid) -> 400 or 403
            payload_wrong_svc = {
                "email": "mgr_wrongsvc@boston.com",
                "username": "mgr_wrongsvc",
                "role": "responsable_servicio",
                "primary_service_id": 3,
                "password_setup": "invite_link"
            }
            res_wrong_svc = await ac.post("/bm/users", json=payload_wrong_svc)
            self.assertIn(res_wrong_svc.status_code, (400, 403), msg=res_wrong_svc.text)

            # 7c. Succeed creation with primary_service_id=1 and allowed_service_ids=[1, 2]
            payload_valid_mgr = {
                "email": "mgr_valid@boston.com",
                "username": "mgr_valid",
                "role": "responsable_servicio",
                "primary_service_id": 1,
                "allowed_service_ids": [1, 2],
                "password_setup": "invite_link"
            }
            res_valid_mgr = await ac.post("/bm/users", json=payload_valid_mgr)
            self.assertEqual(res_valid_mgr.status_code, 201, msg=res_valid_mgr.text)
            mgr_data = res_valid_mgr.json()["user"]
            self.assertEqual(mgr_data["primary_service_id"], 1)
            self.assertEqual(mgr_data["primary_service_name"], "Front Boston")
            self.assertIn(1, mgr_data["allowed_service_ids"])
            self.assertIn(2, mgr_data["allowed_service_ids"])
            self.assertEqual(len(mgr_data["allowed_services"]), 2)

    async def test_service_manager_operational_permissions_and_scoping(self):
        """8. Test service_manager operational access: tenant-context, services list, user scoping, agent creation."""
        # First, create a valid service_manager user
        async with AsyncSession(get_engine()) as db:
            admin_user = await db.get(User, self.u_admin_boston.user_id)
            admin_ctx = await TenantContext.build(admin_user, db)

        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_tenant_context] = lambda: admin_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_create = await ac.post("/bm/users", json={
                "email": "responsable_op@boston.com",
                "username": "responsable_op",
                "role": "responsable_servicio",
                "service_id": 1,
                "password_setup": "invite_link"
            })
            self.assertEqual(res_create.status_code, 201, msg=res_create.text)
            mgr_id = res_create.json()["user"]["user_id"]

        # Now act AS the created service_manager
        async with AsyncSession(get_engine()) as db:
            mgr_user = await db.get(User, mgr_id)
            mgr_ctx = await TenantContext.build(mgr_user, db)

        app.dependency_overrides[get_current_user] = lambda: mgr_user
        app.dependency_overrides[get_tenant_context] = lambda: mgr_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 8a. Check GET /bm/me/tenant-context
            res_tc = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res_tc.status_code, 200)
            tc_data = res_tc.json()
            self.assertEqual(tc_data["normalized_role"], "service_manager")
            self.assertEqual(tc_data["primary_service_id"], 1)
            self.assertIn(1, tc_data["allowed_service_ids"])
            self.assertTrue(tc_data["can_manage_users"])
            self.assertEqual(tc_data["allowed_services"][0]["service_id"], 1)

            # 8b. Check GET /bm/services returns only allowed service (service 1)
            res_svc = await ac.get("/bm/services")
            self.assertEqual(res_svc.status_code, 200)
            svcs = res_svc.json()
            svc_ids = [s["service_id"] for s in svcs]
            self.assertIn(1, svc_ids)
            self.assertNotIn(3, svc_ids)

            # 8c. Check GET /bm/users does not return superadmins or company_admin
            res_users = await ac.get("/bm/users")
            self.assertEqual(res_users.status_code, 200)
            returned_users = res_users.json()["users"]
            returned_roles = [u["role"] for u in returned_users]
            self.assertNotIn("super_admin", returned_roles)
            self.assertNotIn("company_admin", returned_roles)

            # 8d. Check service_manager CANNOT create super_admin or company_admin
            res_bad_role = await ac.post("/bm/users", json={
                "email": "illegal_admin@boston.com",
                "username": "illegal_admin",
                "role": "company_admin",
                "password_setup": "invite_link"
            })
            self.assertEqual(res_bad_role.status_code, 403)

            # 8e. Check service_manager CAN create an agent in their service
            res_agent = await ac.post("/bm/users", json={
                "email": "new_agent_under_mgr@boston.com",
                "username": "new_agent_under_mgr",
                "role": "agente",
                "primary_service_id": 1,
                "password_setup": "invite_link"
            })
            self.assertEqual(res_agent.status_code, 201, msg=res_agent.text)

            # 8f. Check service_manager CANNOT assign a service of another company (service 3)
            res_cross_svc = await ac.post("/bm/users", json={
                "email": "cross_agent@boston.com",
                "username": "cross_agent",
                "role": "agente",
                "primary_service_id": 3,
                "password_setup": "invite_link"
            })
            self.assertIn(res_cross_svc.status_code, (400, 403))

    async def test_legacy_service_manager_fallback(self):
        """9. Test existing service_manager created without primary_service_id or bm_user_services falls back to company service."""
        async with AsyncSession(get_engine()) as db:
            legacy_user = User(
                username="legacy_responsable",
                email="legacy_responsable@boston.com",
                password_hash="fake_hash",
                role="responsable_servicio",
                company_id=1,
                primary_service_id=None,
                is_active=True
            )
            db.add(legacy_user)
            await db.commit()
            await db.refresh(legacy_user)
            legacy_id = legacy_user.user_id

        async with AsyncSession(get_engine()) as db:
            legacy_user = await db.get(User, legacy_id)
            legacy_ctx = await TenantContext.build(legacy_user, db)

        # Check tenant context fallback
        self.assertEqual(legacy_ctx.primary_service_id, 1)
        self.assertIn(1, legacy_ctx.allowed_service_ids)
        self.assertEqual(legacy_ctx.allowed_services[0]["service_id"], 1)

        app.dependency_overrides[get_current_user] = lambda: legacy_user
        app.dependency_overrides[get_tenant_context] = lambda: legacy_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_tc = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res_tc.status_code, 200)
            tc_data = res_tc.json()
            self.assertEqual(tc_data["primary_service_id"], 1)
            self.assertEqual(tc_data["allowed_services"][0]["service_id"], 1)

            res_svc = await ac.get("/bm/services")
            self.assertEqual(res_svc.status_code, 200)
            svcs = res_svc.json()
            self.assertEqual(len(svcs), 1)
            self.assertEqual(svcs[0]["service_id"], 1)

    async def test_service_manager_structures_cycles_trainer_scope(self):
        """10. Test service_manager scoped access to structures, cycles, and trainer without requiring hubspot_owner_id."""
        async with AsyncSession(get_engine()) as db:
            mgr_user = User(
                username="mgr_full_scope",
                email="mgr_full_scope@boston.com",
                password_hash="fake_hash",
                role="responsable_servicio",
                company_id=1,
                primary_service_id=1,
                hubspot_owner_id=None,
                is_active=True
            )
            db.add(mgr_user)
            
            # Create an agent under service 1
            agent1 = User(
                username="agent_service1",
                email="agent1@boston.com",
                password_hash="fake_hash",
                role="agente",
                company_id=1,
                primary_service_id=1,
                hubspot_owner_id="hs_agent1_svc1",
                is_active=True
            )
            db.add(agent1)

            # Create an agent under company 2 (other company)
            agent2 = User(
                username="agent_service3",
                email="agent3@madrid.com",
                password_hash="fake_hash",
                role="agente",
                company_id=2,
                primary_service_id=3,
                hubspot_owner_id="hs_agent3_svc3",
                is_active=True
            )
            db.add(agent2)

            await db.commit()
            await db.refresh(mgr_user)
            mgr_id = mgr_user.user_id

        async with AsyncSession(get_engine()) as db:
            mgr_user = await db.get(User, mgr_id)
            mgr_ctx = await TenantContext.build(mgr_user, db)

        app.dependency_overrides[get_current_user] = lambda: mgr_user
        app.dependency_overrides[get_tenant_context] = lambda: mgr_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 10a. Tenant context flags
            res_tc = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res_tc.status_code, 200)
            tc_data = res_tc.json()
            self.assertTrue(tc_data["can_manage_training"])
            self.assertTrue(tc_data["can_manage_trainer"])
            self.assertTrue(tc_data["can_manage_structures"])

            # 10b. Prompts listing with and without service_id
            res_p1 = await ac.get("/bm/prompts?service_id=1")
            self.assertEqual(res_p1.status_code, 200)

            res_p_wrong = await ac.get("/bm/prompts?service_id=3")
            self.assertEqual(res_p_wrong.status_code, 403)

            res_p_all = await ac.get("/bm/prompts")
            self.assertEqual(res_p_all.status_code, 200)

            # 10c. Base structures listing
            res_bs = await ac.get("/bm/prompt-base-structures")
            self.assertEqual(res_bs.status_code, 200)

            # 10d. Cycles admin overview & summary
            res_ov = await ac.get("/bm/training/admin/agents-overview")
            self.assertEqual(res_ov.status_code, 200)

            res_sum = await ac.get("/bm/training/admin/cycles-summary")
            self.assertEqual(res_sum.status_code, 200)

            # 10e. Create manual cycle for agent of service 1 (succeeds)
            res_mc = await ac.post("/bm/training/admin/manual-cycle", json={
                "hubspot_owner_ids": ["hs_agent1_svc1"],
                "title": "Manual Cycle Service 1",
                "objectives": ["Mejorar cierre de llamada"]
            })
            self.assertEqual(res_mc.status_code, 200, msg=res_mc.text)

            # 10f. Fail creating manual cycle for agent of another company/service (403)
            res_mc_bad = await ac.post("/bm/training/admin/manual-cycle", json={
                "hubspot_owner_ids": ["hs_agent3_svc3"],
                "title": "Illegal Cycle Service 3",
                "objectives": ["Intento ilegal"]
            })
            self.assertEqual(res_mc_bad.status_code, 403)

            # 10h. Role options & users visibility / management protection
            res_roles = await ac.get("/bm/admin/users/role-options")
            self.assertEqual(res_roles.status_code, 200)
            role_vals = [r["value"] for r in res_roles.json()]
            self.assertNotIn("super_admin", role_vals)
            self.assertNotIn("company_admin", role_vals)
            self.assertIn("agente", role_vals)

            res_users = await ac.get("/bm/admin/users")
            self.assertEqual(res_users.status_code, 200)
            u_names = [u["username"] for u in res_users.json()]
            self.assertIn("adminboston_test", u_names)  # Company admin visible as superior
            self.assertIn("agent_service1", u_names)    # Service 1 agent visible
            self.assertNotIn("superadmin_test", u_names) # Super admin excluded
            self.assertNotIn("agent_service3", u_names)  # Other company agent excluded

            # Try editing company admin -> forbidden
            res_edit_admin = await ac.patch(f"/bm/admin/users/{self.u_admin_boston.user_id}", json={"name": "Illegal Edit"})
            self.assertEqual(res_edit_admin.status_code, 403)

            # 10i. Prompts active check
            res_act_ok = await ac.get("/bm/prompts/active?type=audio&service_id=1")
            self.assertIn(res_act_ok.status_code, (200, 404))  # Not 403!

            res_act_bad = await ac.get("/bm/prompts/active?type=audio&service_id=3")
            self.assertEqual(res_act_bad.status_code, 403)

            # 10j. Analyses history check
            res_hist_ok = await ac.get("/bm/analyses/history?service_id=1")
            self.assertEqual(res_hist_ok.status_code, 200)

            res_hist_bad = await ac.get("/bm/analyses/history?service_id=3")
            self.assertEqual(res_hist_bad.status_code, 403)

    async def test_team_coordinator_permissions_and_scoping(self):
        """11. Test team_coordinator permissions, tenant-context, and team scoping."""
        from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation
        
        async with AsyncSession(get_engine()) as db:
            # Create two teams under Service 1 in Company 1
            t1 = Team(team_id=10, team_name="Equipo Alfa", company_id=1, service_id=1, is_active=True)
            t2 = Team(team_id=20, team_name="Equipo Beta", company_id=1, service_id=1, is_active=True)
            db.add_all([t1, t2])
            await db.flush()

            # Create agents in Team Alfa vs Team Beta
            agent_alfa = User(
                username="agent_alfa", email="agent_alfa@boston.com", password_hash="hash",
                role="agente", company_id=1, hubspot_owner_id="hs_alfa_10", is_active=True
            )
            agent_beta = User(
                username="agent_beta", email="agent_beta@boston.com", password_hash="hash",
                role="agente", company_id=1, hubspot_owner_id="hs_beta_20", is_active=True
            )
            db.add_all([agent_alfa, agent_beta])
            await db.flush()

            # Associate agents to respective teams
            db.add(AgentTeamAssociation(user_id=agent_alfa.user_id, team_id=10))
            db.add(AgentTeamAssociation(user_id=agent_beta.user_id, team_id=20))

            # Create Team Coordinator assigned ONLY to Team Alfa (team_id=10)
            coord_user = User(
                username="coord_alfa", email="coord_alfa@boston.com", password_hash="hash",
                role="coordinador_equipo", company_id=1, primary_team_id=10, is_active=True
            )
            db.add(coord_user)
            await db.flush()
            coord_id = coord_user.user_id
            db.add(UserTeamAssociation(user_id=coord_id, team_id=10))

            await db.commit()

        # 11a. Test tenant context of team_coordinator
        async with AsyncSession(get_engine()) as db:
            coord_user = await db.get(User, coord_id)
            coord_ctx = await TenantContext.build(coord_user, db)

        self.assertEqual(coord_ctx.primary_team_id, 10)
        self.assertIn(10, coord_ctx.allowed_team_ids)
        self.assertNotIn(20, coord_ctx.allowed_team_ids)
        self.assertIn(1, coord_ctx.allowed_service_ids)  # Service 1 derived from Team 10
        self.assertIn("hs_alfa_10", coord_ctx.allowed_agent_ids)
        self.assertNotIn("hs_beta_20", coord_ctx.allowed_agent_ids)

        app.dependency_overrides[get_current_user] = lambda: coord_user
        app.dependency_overrides[get_tenant_context] = lambda: coord_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 11b. Test GET /bm/me/tenant-context
            res_tc = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res_tc.status_code, 200)
            tc = res_tc.json()
            self.assertEqual(tc["normalized_role"], "team_coordinator")
            self.assertEqual(tc["primary_team_id"], 10)
            self.assertIn(10, tc["allowed_team_ids"])
            self.assertIn(1, tc["allowed_service_ids"])
            self.assertTrue(tc["can_manage_users"])
            self.assertTrue(tc["can_manage_training"])

            # 11c. Test agent listing / visibility: coord sees agent_alfa, NOT agent_beta (different team)
            res_users = await ac.get("/bm/users")
            self.assertEqual(res_users.status_code, 200)
            user_names = [u["username"] for u in res_users.json()["users"]]
            self.assertIn("agent_alfa", user_names)
            self.assertNotIn("agent_beta", user_names)

            # 11d. Cannot create company_admin or service_manager (403)
            res_bad_role = await ac.post("/bm/users", json={
                "email": "bad_admin@boston.com", "username": "bad_admin",
                "role": "company_admin", "password_setup": "invite_link"
            })
            self.assertEqual(res_bad_role.status_code, 403)

            # 11e. Can create agent in their team (team_id=10)
            res_agent = await ac.post("/bm/users", json={
                "email": "new_alfa_agent@boston.com", "username": "new_alfa_agent",
                "role": "agente", "primary_team_id": 10, "password_setup": "invite_link"
            })
            self.assertEqual(res_agent.status_code, 201, msg=res_agent.text)

            # 11f. Cannot create agent in Team Beta (team_id=20) outside their scope
            res_beta_agent = await ac.post("/bm/users", json={
                "email": "new_beta_agent@boston.com", "username": "new_beta_agent",
                "role": "agente", "primary_team_id": 20, "password_setup": "invite_link"
            })
            self.assertEqual(res_beta_agent.status_code, 403)

    async def test_scoped_password_links_and_role_options(self):
        """12. Test password setup link generation and role options scoping for service_manager and team_coordinator."""
        from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation
        
        async with AsyncSession(get_engine()) as db:
            sm = User(
                username="sm_links_test", email="sm_links@boston.com", password_hash="hash",
                role="responsable_servicio", company_id=1, primary_service_id=1, is_active=True
            )
            tc = User(
                username="tc_links_test", email="tc_links@boston.com", password_hash="hash",
                role="coordinador_equipo", company_id=1, primary_service_id=1, primary_team_id=10, is_active=True
            )
            ag = User(
                username="ag_links_test", email="ag_links@boston.com", password_hash="hash",
                role="agente", company_id=1, primary_service_id=1, primary_team_id=10, is_active=True
            )
            db.add_all([sm, tc, ag])
            await db.flush()
            sm_id, tc_id, ag_id = sm.user_id, tc.user_id, ag.user_id

            db.add(UserTeamAssociation(user_id=tc_id, team_id=10))
            db.add(AgentTeamAssociation(user_id=ag_id, team_id=10))
            await db.commit()

        # Part 1: Act as service_manager
        async with AsyncSession(get_engine()) as db:
            sm_user = await db.get(User, sm_id)
            sm_ctx = await TenantContext.build(sm_user, db)

        app.dependency_overrides[get_current_user] = lambda: sm_user
        app.dependency_overrides[get_tenant_context] = lambda: sm_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 12a. service_manager role options
            res_ro = await ac.get("/bm/admin/users/role-options")
            self.assertEqual(res_ro.status_code, 200)
            roles = [r["value"] for r in res_ro.json()]
            self.assertIn("coordinador_equipo", roles)
            self.assertIn("agente", roles)
            self.assertNotIn("usuario", roles)
            self.assertNotIn("company_admin", roles)
            self.assertNotIn("super_admin", roles)

            # 12b. service_manager CAN generate setup link for coordinator in their service
            res_link_tc = await ac.post(f"/bm/users/{tc_id}/password-setup-link")
            self.assertEqual(res_link_tc.status_code, 200, msg=res_link_tc.text)
            self.assertIn("url", res_link_tc.json())

            # 12c. service_manager CAN generate reset link for agent in their service
            res_link_ag = await ac.post(f"/bm/users/{ag_id}/password-reset-link")
            self.assertEqual(res_link_ag.status_code, 200, msg=res_link_ag.text)
            self.assertIn("reset_url", res_link_ag.json())

            # 12d. service_manager CANNOT generate setup link for company_admin or super_admin
            res_link_ca = await ac.post(f"/bm/users/{self.u_admin_boston.user_id}/password-setup-link")
            self.assertEqual(res_link_ca.status_code, 403)

            res_link_sa = await ac.post(f"/bm/users/{self.u_super.user_id}/password-setup-link")
            self.assertEqual(res_link_sa.status_code, 403)

        # Part 2: Act as team_coordinator
        async with AsyncSession(get_engine()) as db:
            tc_user = await db.get(User, tc_id)
            tc_ctx = await TenantContext.build(tc_user, db)

        app.dependency_overrides[get_current_user] = lambda: tc_user
        app.dependency_overrides[get_tenant_context] = lambda: tc_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 12e. team_coordinator role options MUST NOT contain admin roles, team_coordinator, or usuario
            res_tc_ro = await ac.get("/bm/admin/users/role-options")
            self.assertEqual(res_tc_ro.status_code, 200)
            tc_roles = [r["value"] for r in res_tc_ro.json()]
            self.assertIn("agente", tc_roles)
            self.assertNotIn("usuario", tc_roles)
            self.assertNotIn("coordinador_equipo", tc_roles)
            self.assertNotIn("responsable_servicio", tc_roles)
            self.assertNotIn("company_admin", tc_roles)
            self.assertNotIn("super_admin", tc_roles)

            # 12f. team_coordinator CANNOT create user with role "Administrador" or "admin" (403)
            res_create_admin = await ac.post("/bm/users", json={
                "email": "illegal_tc_admin@boston.com", "username": "illegal_tc_admin",
                "role": "Administrador", "password_setup": "invite_link"
            })
            self.assertEqual(res_create_admin.status_code, 403)

            # 12g. team_coordinator CAN generate setup link for agent in their team
            res_tc_link_ag = await ac.post(f"/bm/users/{ag_id}/password-setup-link")
            self.assertEqual(res_tc_link_ag.status_code, 200, msg=res_tc_link_ag.text)

            # 12h. team_coordinator CANNOT generate setup link for service_manager or company_admin
            res_tc_link_sm = await ac.post(f"/bm/users/{sm_id}/password-setup-link")
            self.assertEqual(res_tc_link_sm.status_code, 403)

    async def test_disallow_generic_usuario_creation_and_role_options(self):
        """13. Test that generic 'usuario' role is removed from role-options and blocked on creation, while legacy users remain readable."""
        # 13a. Create legacy user with role="usuario" directly in DB
        async with AsyncSession(get_engine()) as db:
            legacy_user = User(
                username="legacy_generic_user", email="legacy_gen@boston.com", password_hash="hash",
                role="usuario", company_id=1, is_active=True
            )
            db.add(legacy_user)
            await db.commit()
            await db.refresh(legacy_user)
            legacy_id = legacy_user.user_id

        # 13b. Verify super_admin role-options does NOT contain 'usuario'
        async with AsyncSession(get_engine()) as db:
            sa_user = await db.get(User, self.u_super.user_id)
            sa_ctx = await TenantContext.build(sa_user, db)

        app.dependency_overrides[get_current_user] = lambda: sa_user
        app.dependency_overrides[get_tenant_context] = lambda: sa_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_ro = await ac.get("/bm/admin/users/role-options")
            self.assertEqual(res_ro.status_code, 200)
            roles = [r["value"] for r in res_ro.json()]
            self.assertNotIn("usuario", roles)
            self.assertNotIn("user", roles)

            # Attempt to create user with role="usuario" -> 400 Bad Request
            res_create = await ac.post("/bm/users", json={
                "email": "new_generic_user@boston.com", "username": "new_gen_user",
                "role": "usuario", "password_setup": "invite_link"
            })
            self.assertEqual(res_create.status_code, 400)

            # Legacy user is still returned in user listing without error
            res_list = await ac.get("/bm/users")
            self.assertEqual(res_list.status_code, 200)
            usernames = [u["username"] for u in res_list.json()["users"]]
            self.assertIn("legacy_generic_user", usernames)

            # Legacy user single lookup succeeds
            res_get = await ac.get(f"/bm/users/{legacy_id}")
            self.assertEqual(res_get.status_code, 200)
            self.assertEqual(res_get.json()["user"]["role"], "usuario")

    async def test_team_coordinator_scoped_operational_access(self):
        """14. Test team_coordinator operational access to prompts, analyses history, cycles, and trainer scoped to allowed teams/agents."""
        from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation
        from app.models.analyses import Analysis
        from app.models.personalized_training import TrainingAgentSetting
        
        async with AsyncSession(get_engine()) as db:
            team_alpha = Team(team_id=100, team_name="Equipo Alfa Ops", service_id=1, company_id=1, is_active=True)
            team_beta = Team(team_id=200, team_name="Equipo Beta Ops", service_id=1, company_id=1, is_active=True)
            db.add_all([team_alpha, team_beta])
            await db.flush()

            tc_ops = User(
                username="tc_ops_user", email="tc_ops@boston.com", password_hash="hash",
                role="coordinador_equipo", company_id=1, primary_service_id=1, primary_team_id=100, is_active=True
            )
            agent_a = User(
                username="agent_a_user", email="agent_a@boston.com", password_hash="hash",
                role="agente", company_id=1, primary_service_id=1, primary_team_id=100, hubspot_owner_id="hs_owner_a", is_active=True
            )
            agent_b = User(
                username="agent_b_user", email="agent_b@boston.com", password_hash="hash",
                role="agente", company_id=1, primary_service_id=1, primary_team_id=200, hubspot_owner_id="hs_owner_b", is_active=True
            )
            db.add_all([tc_ops, agent_a, agent_b])
            await db.flush()
            tc_ops_id, agent_a_id, agent_b_id = tc_ops.user_id, agent_a.user_id, agent_b.user_id

            db.add(UserTeamAssociation(user_id=tc_ops_id, team_id=100))
            db.add(AgentTeamAssociation(user_id=agent_a_id, team_id=100))
            db.add(AgentTeamAssociation(user_id=agent_b_id, team_id=200))

            an_a = Analysis(
                analysis_id=9001, call_id="CALL_OPS_A", company_id=1, service_id=1, hubspot_owner_id="hs_owner_a",
                agente_telefonico="Agente A", tipo_llamada="Inbound", analysis_type="audio", evaluacion_global=8.5
            )
            an_b = Analysis(
                analysis_id=9002, call_id="CALL_OPS_B", company_id=1, service_id=1, hubspot_owner_id="hs_owner_b",
                agente_telefonico="Agente B", tipo_llamada="Inbound", analysis_type="audio", evaluacion_global=9.0
            )
            db.add_all([an_a, an_b])

            # Create TrainingAgentSetting for agent_a so agents-overview can find it
            setting_a = TrainingAgentSetting(
                hubspot_owner_id="hs_owner_a", agent_name="Agente A", agent_initials="AA",
                company_id=1, is_enabled=True
            )
            db.add(setting_a)
            await db.commit()

        # Build context for tc_ops
        async with AsyncSession(get_engine()) as db:
            tc_user = await db.get(User, tc_ops_id)
            tc_ctx = await TenantContext.build(tc_user, db)

        app.dependency_overrides[get_current_user] = lambda: tc_user
        app.dependency_overrides[get_tenant_context] = lambda: tc_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 14b. Verify /me/tenant-context flags for coordinator
            res_tc_me = await ac.get("/bm/me/tenant-context")
            self.assertEqual(res_tc_me.status_code, 200)
            data_me = res_tc_me.json()
            self.assertTrue(data_me["can_manage_structures"])
            self.assertTrue(data_me["can_manage_training"])
            self.assertEqual(data_me["allowed_team_ids"], [100])

            # 14c. Prompt active: allowed service (service_id=1) returns 200 or 404, NOT 403
            res_prompt_active = await ac.get("/bm/prompts/active?type=audio&service_id=1")
            self.assertIn(res_prompt_active.status_code, (200, 404))

            # 14d. Prompt active: unallowed service (service_id=999) returns 403 Forbidden
            res_prompt_forbidden = await ac.get("/bm/prompts/active?type=audio&service_id=999")
            self.assertEqual(res_prompt_forbidden.status_code, 403)

            # 14e. Analyses history returns analyses for Agent A, but NOT Agent B
            res_history = await ac.get("/bm/analyses/history")
            self.assertEqual(res_history.status_code, 200)
            call_ids = [an["call_id"] for an in res_history.json()]
            self.assertIn("CALL_OPS_A", call_ids)
            self.assertNotIn("CALL_OPS_B", call_ids)

            # 14f. Admin agents overview returns only Agent A
            res_overview = await ac.get("/bm/training/admin/agents-overview")
            self.assertEqual(res_overview.status_code, 200)
            overview_agents = [item["hubspot_owner_id"] for item in res_overview.json()]
            self.assertIn("hs_owner_a", overview_agents)
            self.assertNotIn("hs_owner_b", overview_agents)

            # 14g. Create manual cycle for Agent A (in team 100) -> 200 OK
            res_cycle_a = await ac.post("/bm/training/admin/manual-cycle", json={
                "hubspot_owner_ids": ["hs_owner_a"], "reason": "Test manual cycle Agent A"
            })
            self.assertEqual(res_cycle_a.status_code, 200, msg=res_cycle_a.text)

            # 14h. Create manual cycle for Agent B (outside team) -> 403 Forbidden
            res_cycle_b = await ac.post("/bm/training/admin/manual-cycle", json={
                "hubspot_owner_ids": ["hs_owner_b"], "reason": "Test manual cycle Agent B"
            })
            self.assertEqual(res_cycle_b.status_code, 403)

        # 14i. Test that pure AGENT role is still blocked from structure endpoints
        async with AsyncSession(get_engine()) as db:
            agent_user = await db.get(User, agent_a_id)
            agent_ctx = await TenantContext.build(agent_user, db)

        app.dependency_overrides[get_current_user] = lambda: agent_user
        app.dependency_overrides[get_tenant_context] = lambda: agent_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_agent_prompt = await ac.get("/bm/prompts/active?type=audio&service_id=1")
            self.assertEqual(res_agent_prompt.status_code, 403)


if __name__ == "__main__":
    unittest.main()

