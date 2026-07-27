"""Unit test suite for Company Branding multi-tenant API contract."""
import os
import unittest
import httpx

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_company_branding_db_test.db"

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


class TestCompanyBrandingApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            # 1. Companies
            c10 = Company(company_id=10, company_name="Boston Medical", company_key="boston_medical", brand_name="Boston Medical", brand_short_name="Boston Medical", app_variant="boston_medical", dashboard_variant="boston_medical", sector="healthcare", is_active=True)
            c20 = Company(company_id=20, company_name="Empresa Demo", company_key="empresa_demo", brand_name="Demo Corporate", brand_short_name="DemoCorp", app_variant="demo_variant", dashboard_variant="demo_dash", is_active=True)
            db.add_all([c10, c20])
            await db.flush()

            # 2. Services
            s102 = Service(service_id=102, service_name="Experiencia Paciente", service_key="exppa", company_id=10)
            s201 = Service(service_id=201, service_name="Ventas Demo", service_key="ventas_demo", company_id=20)
            db.add_all([s102, s201])
            await db.flush()

            # 3. Teams
            t1002 = Team(team_id=1002, team_name="Equipo ExpPa Principal", company_id=10, service_id=102, is_active=True)
            db.add(t1002)
            await db.flush()

            # 4. Users
            u_super = User(user_id=1, username="superadmin", email="super@doobot.ai", role="super_admin", company_id=None, is_active=True, password_hash=hash_password("pass"))
            u_cadmin = User(user_id=2, username="adminboston", email="admin@boston.es", role="company_admin", company_id=10, is_active=True, password_hash=hash_password("pass"))
            u_smanager = User(user_id=3, username="juanjo", email="juanjo@boston.es", role="service_manager", company_id=10, is_active=True, password_hash=hash_password("pass"))
            u_tcoord = User(user_id=4, username="jcerdan", email="jcerdan@boston.es", role="team_coordinator", company_id=10, is_active=True, password_hash=hash_password("pass"))
            u_agent = User(user_id=5, username="victoria", email="victoria@boston.es", role="agent", company_id=10, hubspot_owner_id="31499194", is_active=True, password_hash=hash_password("pass"))
            u_demo_admin = User(user_id=6, username="demoadmin", email="admin@demo.es", role="company_admin", company_id=20, is_active=True, password_hash=hash_password("pass"))
            db.add_all([u_super, u_cadmin, u_smanager, u_tcoord, u_agent, u_demo_admin])
            await db.flush()

            # Associations
            db.add(UserServiceAssociation(user_id=3, service_id=102))
            db.add(UserTeamAssociation(user_id=4, team_id=1002))
            db.add(AgentTeamAssociation(user_id=5, team_id=1002))
            await db.commit()

        self.t_super = create_access_token({"sub": "superadmin", "user_id": 1, "role": "super_admin"})
        self.t_cadmin = create_access_token({"sub": "adminboston", "user_id": 2, "role": "company_admin", "company_id": 10})
        self.t_smanager = create_access_token({"sub": "juanjo", "user_id": 3, "role": "service_manager", "company_id": 10})
        self.t_tcoord = create_access_token({"sub": "jcerdan", "user_id": 4, "role": "team_coordinator", "company_id": 10})
        self.t_agent = create_access_token({"sub": "victoria", "user_id": 5, "role": "agent", "company_id": 10})
        self.t_demo_admin = create_access_token({"sub": "demoadmin", "user_id": 6, "role": "company_admin", "company_id": 20})

        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_01_me_branding_boston_roles(self):
        """Verify GET /bm/me/branding returns Boston Medical for Boston users of all roles."""
        for name, token in [
            ("company_admin", self.t_cadmin),
            ("service_manager", self.t_smanager),
            ("team_coordinator", self.t_tcoord),
            ("agent", self.t_agent),
        ]:
            r = await self.client.get("/bm/me/branding", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(r.status_code, 200, f"Failed for {name}")
            data = r.json()
            self.assertEqual(data["company_id"], 10)
            self.assertEqual(data["brand_name"], "Boston Medical")
            self.assertEqual(data["app_variant"], "boston_medical")
            self.assertFalse(data["is_global_context"])

    async def test_02_me_branding_super_admin_global(self):
        """Verify GET /bm/me/branding returns neutral Doobot.ai_ branding for super_admin without company."""
        r = await self.client.get("/bm/me/branding", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsNone(data["company_id"])
        self.assertEqual(data["brand_name"], "Doobot.ai_")
        self.assertEqual(data["brand_short_name"], "Doobot")
        self.assertEqual(data["app_variant"], "global")
        self.assertTrue(data["is_global_context"])

    async def test_03_me_branding_empresa_demo(self):
        """Verify GET /bm/me/branding returns Demo Corporate for Empresa Demo admin."""
        r = await self.client.get("/bm/me/branding", headers={"Authorization": f"Bearer {self.t_demo_admin}"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["company_id"], 20)
        self.assertEqual(data["brand_name"], "Demo Corporate")
        self.assertEqual(data["brand_short_name"], "DemoCorp")
        self.assertEqual(data["app_variant"], "demo_variant")
        self.assertFalse(data["is_global_context"])

    async def test_04_tenant_context_includes_branding(self):
        """Verify GET /bm/me/tenant-context includes the branding object."""
        r = await self.client.get("/bm/me/tenant-context", headers={"Authorization": f"Bearer {self.t_cadmin}"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("branding", data)
        self.assertIsNotNone(data["branding"])
        self.assertEqual(data["branding"]["brand_name"], "Boston Medical")

    async def test_05_admin_patch_branding(self):
        """Verify PATCH /bm/admin/companies/{company_id}/branding permissions."""
        # 1. super_admin can update Empresa Demo
        r_super = await self.client.patch(
            "/bm/admin/companies/20/branding",
            json={"brand_name": "Demo Updated", "primary_color": "#FF0000"},
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(r_super.status_code, 200)
        self.assertEqual(r_super.json()["brand_name"], "Demo Updated")
        self.assertEqual(r_super.json()["primary_color"], "#FF0000")

        # 2. company_admin can update their own company (Boston Medical = 10)
        r_cadmin = await self.client.patch(
            "/bm/admin/companies/10/branding",
            json={"primary_color": "#0000FF"},
            headers={"Authorization": f"Bearer {self.t_cadmin}"}
        )
        self.assertEqual(r_cadmin.status_code, 200)
        self.assertEqual(r_cadmin.json()["primary_color"], "#0000FF")

        # 3. company_admin trying to update another company -> 403
        r_other = await self.client.patch(
            "/bm/admin/companies/20/branding",
            json={"brand_name": "Hack"},
            headers={"Authorization": f"Bearer {self.t_cadmin}"}
        )
        self.assertEqual(r_other.status_code, 403)

        # 4. service_manager trying to update branding -> 403
        r_sm = await self.client.patch(
            "/bm/admin/companies/10/branding",
            json={"brand_name": "Hack"},
            headers={"Authorization": f"Bearer {self.t_smanager}"}
        )
        self.assertEqual(r_sm.status_code, 403)


if __name__ == "__main__":
    unittest.main()
