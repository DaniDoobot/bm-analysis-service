"""
Test Suite: Admin Typologies, Base Structures, and Specific Structures.
Tests scoping, creation, association, active prompts routing, and role constraints.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///admin_typologies_test.db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.typologies import Typology
from app.models.prompts import Prompt, PromptBaseStructure, BaseStructureTypology
from app.models.users import User
from app.dependencies import get_current_user
from app.main import app


class TestAdminTypologiesStructures(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Engine URL points to production!"

        if os.path.exists("admin_typologies_test.db"):
            try:
                os.remove("admin_typologies_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine) as db:
            # 1. Companies
            self.c1 = Company(company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c2 = Company(company_name="Gesalux", company_key="gesalux", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()
            self.company1_id = self.c1.company_id
            self.company2_id = self.c2.company_id

            # 2. Services
            self.s1 = Service(service_name="Front", service_key="front", company_id=self.company1_id)
            self.s2 = Service(service_name="Urgencias", service_key="urgencias", company_id=self.company2_id)
            db.add_all([self.s1, self.s2])
            await db.flush()
            self.service1_id = self.s1.service_id
            self.service2_id = self.s2.service_id

            # 3. Typologies
            self.t1 = Typology(typology_name="Cita Med", typology_key="cita-med", service_id=self.service1_id, company_id=self.company1_id, is_active=True)
            self.t2 = Typology(typology_name="Queja Svc", typology_key="queja-svc", service_id=self.service2_id, company_id=self.company2_id, is_active=True)
            db.add_all([self.t1, self.t2])
            await db.flush()
            self.typo1_id = self.t1.typology_id
            self.typo2_id = self.t2.typology_id

            # 4. Base Structures
            self.bs1 = PromptBaseStructure(
                structure_key="base-boston",
                structure_name="Base Boston",
                base_prompt="Base Prompt Boston",
                service_id=self.service1_id,
                company_id=self.company1_id,
                is_active=True,
                is_global=False
            )
            self.bs2 = PromptBaseStructure(
                structure_key="base-gesalux",
                structure_name="Base Gesalux",
                base_prompt="Base Prompt Gesalux",
                service_id=self.service2_id,
                company_id=self.company2_id,
                is_active=True,
                is_global=False
            )
            db.add_all([self.bs1, self.bs2])
            await db.flush()
            self.bs1_id = self.bs1.id
            self.bs2_id = self.bs2.id

            # 5. Prompts / Specific Structures
            self.p1 = Prompt(
                prompt_name="Prompt Boston",
                prompt_type="audio",
                base_structure_id=self.bs1_id,
                service_id=self.service1_id,
                company_id=self.company1_id,
                is_active=True,
                is_global=False
            )
            self.p2 = Prompt(
                prompt_name="Prompt Gesalux",
                prompt_type="audio",
                base_structure_id=self.bs2_id,
                service_id=self.service2_id,
                company_id=self.company2_id,
                is_active=True,
                is_global=False
            )
            db.add_all([self.p1, self.p2])
            await db.flush()
            self.p1_id = self.p1.prompt_id
            self.p2_id = self.p2.prompt_id

            # 6. Users
            self.u_super = User(username="super", email="super@test.com", role="administrador", password_hash="x")
            self.u_comp = User(username="comp_admin", email="comp@test.com", role="company_admin", company_id=self.company1_id, password_hash="x", is_active=True)
            self.u_mgr = User(username="svc_mgr", email="mgr@test.com", role="responsable_servicio", company_id=self.company1_id, password_hash="x", is_active=True)
            self.u_agent = User(username="agent1", email="agent1@test.com", role="agente", company_id=self.company1_id, password_hash="x", is_active=True)
            db.add_all([self.u_super, self.u_comp, self.u_mgr, self.u_agent])
            await db.flush()
            self.super_id = self.u_super.user_id
            self.comp_id = self.u_comp.user_id
            self.mgr_id = self.u_mgr.user_id
            self.agent_id = self.u_agent.user_id

            # Assign service manager to service1
            from app.models.teams import UserServiceAssociation
            db.add(UserServiceAssociation(user_id=self.mgr_id, service_id=self.service1_id))
            await db.commit()

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ── TEST 1: super_admin lists everything ─────────────────────────────────

    async def test_1_super_admin_lists_everything(self):
        """super_admin sees all typologies and base structures."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 1. Typologies
            res_typo = await ac.get("/bm/typologies")
            self.assertEqual(res_typo.status_code, 200)
            self.assertEqual(len(res_typo.json()), 2)

            # 2. Base structures
            res_base = await ac.get("/bm/prompt-base-structures")
            self.assertEqual(res_base.status_code, 200)
            self.assertEqual(len(res_base.json()), 2)

    # ── TEST 2: company_admin only sees their company typologies ─────────────

    async def test_2_company_admin_scoping(self):
        """company_admin only sees typologies and base structures of their company."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 1. Typologies
            res_typo = await ac.get("/bm/typologies")
            self.assertEqual(res_typo.status_code, 200)
            self.assertEqual(len(res_typo.json()), 1)
            self.assertEqual(res_typo.json()[0]["company_id"], self.company1_id)

            # 2. Detail of cross-company base structure -> 403
            res_base = await ac.get(f"/bm/prompt-base-structures/{self.bs2_id}")
            self.assertEqual(res_base.status_code, 403)

    # ── TEST 3: responsable_servicio only sees their service typologies ──────

    async def test_3_service_manager_scoping(self):
        """service_manager only sees typologies of their allowed services."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.mgr_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_typo = await ac.get("/bm/typologies")
            self.assertEqual(res_typo.status_code, 200)
            self.assertEqual(len(res_typo.json()), 1)
            self.assertEqual(res_typo.json()[0]["service_id"], self.service1_id)

    # ── TEST 4: agent cannot access typologies/structures ────────────────────

    async def test_4_agent_denied(self):
        """agent gets 403 on structures and typologies endpoints."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.agent_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Typologies list -> 403
            res_typo = await ac.get("/bm/typologies")
            self.assertEqual(res_typo.status_code, 403)

            # Base structures list -> 403
            res_base = await ac.get("/bm/prompt-base-structures")
            self.assertEqual(res_base.status_code, 403)

    # ── TEST 5: create typology in own service OK ───────────────────────────

    async def test_5_create_typology_own_service(self):
        """company_admin can create a typology in their own company's service."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            payload = {
                "service_id": self.service1_id,
                "typology_key": "nueva-cita",
                "typology_name": "Nueva Cita",
                "is_active": True
            }
            res = await ac.post("/bm/typologies", json=payload)
            self.assertEqual(res.status_code, 201)
            self.assertEqual(res.json()["name"], "Nueva Cita")

            # Verify in DB it has correct company_id
            async with AsyncSession(engine) as db:
                typo_db = (await db.execute(
                    select(Typology).where(Typology.typology_key == "nueva-cita")
                )).scalars().first()
                self.assertIsNotNone(typo_db)
                self.assertEqual(typo_db.company_id, self.company1_id)

    # ── TEST 6: create typology cross-company blocked ────────────────────────

    async def test_6_create_typology_cross_company_blocked(self):
        """company_admin cannot create a typology in another company's service."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            payload = {
                "service_id": self.service2_id,
                "typology_key": "cita-hack",
                "typology_name": "Cita Hack",
                "is_active": True
            }
            res = await ac.post("/bm/typologies", json=payload)
            self.assertEqual(res.status_code, 403)

    # ── TEST 7: associate typology same service OK ───────────────────────────

    async def test_7_associate_typology_same_service(self):
        """Can associate typologies of the same service to a base structure."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            payload = {"typology_ids": [self.typo1_id]}
            res = await ac.patch(f"/bm/base-structures/{self.bs1_id}/typologies", json=payload)
            self.assertEqual(res.status_code, 200)

            # Check mapping exists
            async with AsyncSession(engine) as db:
                m_res = await db.execute(
                    select(BaseStructureTypology).where(BaseStructureTypology.base_structure_id == self.bs1_id)
                )
                self.assertEqual(len(m_res.scalars().all()), 1)

    # ── TEST 8: associate typology cross-service blocked ─────────────────────

    async def test_8_associate_typology_cross_service_blocked(self):
        """Cannot associate typologies from another service or company."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)  # Using superadmin to isolate service checks
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # typo2 belongs to service2/company2, bs1 belongs to service1/company1
            payload = {"typology_ids": [self.typo2_id]}
            res = await ac.patch(f"/bm/base-structures/{self.bs1_id}/typologies", json=payload)
            self.assertEqual(res.status_code, 400)

    # ── TEST 9: prompt activo no se cruza entre empresas ─────────────────────

    async def test_9_active_prompt_isolation(self):
        """Active prompt is resolved correctly inside service/company boundaries."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)
            u_comp = await db.get(User, self.comp_id)

        # 1. Query active prompt for service1 as company_admin
        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Auto-resolves to service1 because user belongs to company1
            res_comp = await ac.get("/bm/prompts/active?type=audio")
            self.assertEqual(res_comp.status_code, 200)
            self.assertEqual(res_comp.json()["prompt_id"], self.p1_id)

            # Query cross-company service -> 403
            res_bad = await ac.get(f"/bm/prompts/active?type=audio&service_id={self.service2_id}")
            self.assertEqual(res_bad.status_code, 403)

        # 2. Query active prompt as super_admin specifying service2
        app.dependency_overrides[get_current_user] = lambda: u_super
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_super = await ac.get(f"/bm/prompts/active?type=audio&service_id={self.service2_id}")
            self.assertEqual(res_super.status_code, 200)
            self.assertEqual(res_super.json()["prompt_id"], self.p2_id)


if __name__ == "__main__":
    unittest.main()
