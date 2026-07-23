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

            # 10g. Trainer simulations & evaluation configs
            res_sim = await ac.get("/bm/trainer/simulations")
            self.assertEqual(res_sim.status_code, 200)

            res_sim_bad = await ac.get("/bm/trainer/simulations?service_id=3")
            self.assertEqual(res_sim_bad.status_code, 403)

            res_cfg = await ac.get("/bm/trainer/evaluation-configs")
            self.assertEqual(res_cfg.status_code, 200)

            res_cfg_bad = await ac.get("/bm/trainer/evaluation-configs?service_id=3")
            self.assertEqual(res_cfg_bad.status_code, 403)


if __name__ == "__main__":
    unittest.main()

