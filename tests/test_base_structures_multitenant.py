import os
import unittest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select, text
from httpx import AsyncClient, ASGITransport

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.prompts import PromptBaseStructure
from app.utils.security import create_access_token, hash_password
from app.main import app
from app.dependencies import get_db

class TestBaseStructuresMultitenant(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            pwd = hash_password("Password123!")

            # 1. Companies
            self.c_boston = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c_demo = Company(company_id=2, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True, is_active=True)
            db.add_all([self.c_boston, self.c_demo])
            await db.flush()

            # 2. Services
            self.s_boston = Service(service_id=10, service_name="Front Boston", service_key="front-boston", company_id=1, is_active=True)
            self.s_demo = Service(service_id=20, service_name="Atención Demo", service_key="atencion-demo", company_id=2, is_active=True)
            db.add_all([self.s_boston, self.s_demo])
            await db.flush()

            # 3. Users
            self.u_super = User(user_id=1, username="superadmin", email="super@test.com", role="super_admin", password_hash=pwd, is_active=True)
            self.u_admin_boston = User(user_id=2, username="admin_boston", email="admin_boston@test.com", role="company_admin", company_id=1, password_hash=pwd, is_active=True)
            self.u_admin_demo = User(user_id=3, username="admin_demo", email="admin_demo@test.com", role="company_admin", company_id=2, password_hash=pwd, is_active=True)
            db.add_all([self.u_super, self.u_admin_boston, self.u_admin_demo])
            await db.flush()

            # 4. Existing structures prior to migration fix
            # Boston Medical private structures
            self.bs_bm1 = PromptBaseStructure(
                id=1, structure_key="boston_medical_audio", structure_name="Boston Medical Audio",
                base_prompt="BM Audio", prompt_type="text", service_id=10, company_id=1, is_global=False, is_active=True, owner_user_id=2
            )
            self.bs_bm2 = PromptBaseStructure(
                id=2, structure_key="boston_medical_appointment", structure_name="Boston Medical Appointment",
                base_prompt="BM Cita", prompt_type="text", service_id=10, company_id=1, is_global=False, is_active=True, owner_user_id=2
            )

            # Generic catalog templates (before backfill, company_id was 1 and is_global was False)
            self.bs_gen = PromptBaseStructure(
                id=3, structure_key="generic_customer_service", structure_name="Atención al cliente genérico",
                base_prompt="Generic Prompt", prompt_type="text", service_id=None, company_id=1, is_global=False, is_active=True, owner_user_id=1
            )
            self.bs_comm = PromptBaseStructure(
                id=4, structure_key="commercial_quality", structure_name="Calidad comercial",
                base_prompt="Commercial Prompt", prompt_type="text", service_id=None, company_id=1, is_global=False, is_active=True, owner_user_id=1
            )
            self.bs_blank = PromptBaseStructure(
                id=5, structure_key="blank", structure_name="Prompt desde cero",
                base_prompt="", prompt_type="text", service_id=None, company_id=1, is_global=False, is_active=True, owner_user_id=1
            )

            # Structure created previously by Empresa Demo user before fix (saved with company_id=None, is_global=False)
            self.bs_orphan_demo = PromptBaseStructure(
                id=6, structure_key="demo_created_previously", structure_name="Estructura Demo Previa",
                base_prompt="Demo Prompt", prompt_type="text", service_id=20, company_id=None, is_global=False, is_active=True, owner_user_id=3
            )

            db.add_all([self.bs_bm1, self.bs_bm2, self.bs_gen, self.bs_comm, self.bs_blank, self.bs_orphan_demo])
            await db.commit()

        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_admin_boston = create_access_token({"user_id": 2, "email": "admin_boston@test.com"})
        self.t_admin_demo = create_access_token({"user_id": 3, "email": "admin_demo@test.com"})

        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://test")

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        await self.client.aclose()
        await self.engine.dispose()

    # ── 1. Company admin creates base structure with company_id ───────────────
    async def test_company_admin_creates_base_structure_with_company_id(self):
        res = await self.client.post(
            "/bm/prompt-base-structures",
            json={
                "structure_key": "new_demo_struct",
                "structure_name": "Nueva Base Demo",
                "service_id": 20
            },
            headers={"Authorization": f"Bearer {self.t_admin_demo}"}
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["company_id"], 2)
        self.assertFalse(data["is_global"])
        self.assertTrue(data["access"]["can_view"])
        self.assertTrue(data["access"]["can_edit"])

        # Check in DB
        async with self.session_maker() as db:
            row = (await db.execute(select(PromptBaseStructure).where(PromptBaseStructure.structure_key == "new_demo_struct"))).scalars().first()
            self.assertIsNotNone(row)
            self.assertEqual(row.company_id, 2)
            self.assertFalse(row.is_global)
            self.assertEqual(row.owner_user_id, 3)

    # ── 2. Company admin cannot assign other company ──────────────────────────
    async def test_company_admin_cannot_assign_other_company(self):
        # Attempt to pass company_id = 1 (Boston Medical) while authenticated as Demo admin
        res = await self.client.post(
            "/bm/prompt-base-structures",
            json={
                "structure_key": "spoof_company_struct",
                "structure_name": "Spoofed Company",
                "company_id": 1,
                "service_id": 20
            },
            headers={"Authorization": f"Bearer {self.t_admin_demo}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # TenantContext company_id MUST prevail
        self.assertEqual(data["company_id"], 2)

        async with self.session_maker() as db:
            row = (await db.execute(select(PromptBaseStructure).where(PromptBaseStructure.structure_key == "spoof_company_struct"))).scalars().first()
            self.assertEqual(row.company_id, 2)

    # ── 3. Company admin sees own base structures ─────────────────────────────
    async def test_company_admin_sees_own_base_structures(self):
        # Create an explicitly owned base structure
        async with self.session_maker() as db:
            s = PromptBaseStructure(
                structure_key="demo_owned_test", structure_name="Demo Owned",
                base_prompt="P", prompt_type="text", service_id=20, company_id=2, is_global=False, is_active=True, owner_user_id=3
            )
            db.add(s)
            await db.commit()

        res = await self.client.get(
            "/bm/prompt-base-structures",
            headers={"Authorization": f"Bearer {self.t_admin_demo}"}
        )
        self.assertEqual(res.status_code, 200)
        keys = [item["structure_key"] for item in res.json()]
        self.assertIn("demo_owned_test", keys)

    # ── 4. Company admin sees global base structures ──────────────────────────
    async def test_company_admin_sees_global_base_structures(self):
        # Create a global base structure
        async with self.session_maker() as db:
            s = PromptBaseStructure(
                structure_key="global_test_template", structure_name="Plantilla Global Test",
                base_prompt="Global P", prompt_type="text", service_id=None, company_id=None, is_global=True, is_active=True, owner_user_id=1
            )
            db.add(s)
            await db.commit()

        res = await self.client.get(
            "/bm/prompt-base-structures",
            headers={"Authorization": f"Bearer {self.t_admin_demo}"}
        )
        self.assertEqual(res.status_code, 200)
        keys = [item["structure_key"] for item in res.json()]
        self.assertIn("global_test_template", keys)
        item = next(i for i in res.json() if i["structure_key"] == "global_test_template")
        self.assertTrue(item["is_global"])
        self.assertTrue(item["access"]["can_view"])
        self.assertTrue(item["access"]["can_use"])
        # Non-superadmin cannot delete global structure
        self.assertFalse(item["access"]["can_delete"])

    # ── 5. Company admin does not see other company base structures ───────────
    async def test_company_admin_does_not_see_other_company_base_structures(self):
        res = await self.client.get(
            "/bm/prompt-base-structures",
            headers={"Authorization": f"Bearer {self.t_admin_demo}"}
        )
        self.assertEqual(res.status_code, 200)
        keys = [item["structure_key"] for item in res.json()]
        self.assertNotIn("boston_medical_audio", keys)
        self.assertNotIn("boston_medical_appointment", keys)

    # ── 6. Service must belong to company ─────────────────────────────────────
    async def test_service_must_belong_to_company(self):
        # Demo admin attempts to link a base structure to Boston Medical service (service_id=10)
        res = await self.client.post(
            "/bm/prompt-base-structures",
            json={
                "structure_key": "cross_company_svc",
                "structure_name": "Cross Svc",
                "service_id": 10
            },
            headers={"Authorization": f"Bearer {self.t_admin_demo}"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("no pertenece a tu empresa", res.json()["detail"])

    # ── 7. Global structure is not company owned ──────────────────────────────
    async def test_global_structure_is_not_company_owned(self):
        # Superadmin can create global structure
        res_super = await self.client.post(
            "/bm/prompt-base-structures",
            json={
                "structure_key": "super_created_global",
                "structure_name": "Global by Super",
                "is_global": True
            },
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res_super.status_code, 200, res_super.text)
        data = res_super.json()
        self.assertTrue(data["is_global"])
        self.assertIsNone(data["company_id"])

        # Company admin CANNOT create global structure
        res_admin = await self.client.post(
            "/bm/prompt-base-structures",
            json={
                "structure_key": "admin_tried_global",
                "structure_name": "Global by Admin",
                "is_global": True
            },
            headers={"Authorization": f"Bearer {self.t_admin_demo}"}
        )
        self.assertEqual(res_admin.status_code, 403)
        self.assertIn("Solo los superadministradores", res_admin.json()["detail"])

    # ── 8. Existing demo structure backfill ────────────────────────────────────
    async def test_existing_demo_structure_backfill(self):
        # Run the backfill logic on the orphan structure
        async with self.session_maker() as db:
            await db.execute(text("""
                UPDATE bm_prompt_base_structures
                SET company_id = 2
                WHERE owner_user_id = 3 AND company_id IS NULL AND is_global = 0
            """))
            await db.commit()

        # Demo admin should now immediately see it
        res = await self.client.get(
            "/bm/prompt-base-structures",
            headers={"Authorization": f"Bearer {self.t_admin_demo}"}
        )
        self.assertEqual(res.status_code, 200)
        keys = [item["structure_key"] for item in res.json()]
        self.assertIn("demo_created_previously", keys)
        item = next(i for i in res.json() if i["structure_key"] == "demo_created_previously")
        self.assertEqual(item["company_id"], 2)
        self.assertTrue(item["access"]["can_view"])

    # ── 9. Existing global catalog backfill ────────────────────────────────────
    async def test_existing_global_catalog_backfill(self):
        # Execute migration 019 backfill logic for global catalog
        async with self.session_maker() as db:
            await db.execute(text("""
                UPDATE bm_prompt_base_structures
                SET is_global = 1, company_id = NULL
                WHERE structure_key IN ('generic_customer_service', 'commercial_quality', 'blank')
            """))
            await db.commit()

        # Both Empresa Demo and Boston Medical should see all 3 global templates
        for token in [self.t_admin_demo, self.t_admin_boston]:
            res = await self.client.get("/bm/prompt-base-structures", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(res.status_code, 200)
            keys = [item["structure_key"] for item in res.json()]
            self.assertIn("generic_customer_service", keys)
            self.assertIn("commercial_quality", keys)
            self.assertIn("blank", keys)

    # ── 10. Boston Medical structures remain private ──────────────────────────
    async def test_boston_medical_structures_remain_private(self):
        # Empresa Demo admin must NOT see BM structures
        res_demo = await self.client.get("/bm/prompt-base-structures", headers={"Authorization": f"Bearer {self.t_admin_demo}"})
        self.assertEqual(res_demo.status_code, 200)
        keys_demo = [item["structure_key"] for item in res_demo.json()]
        self.assertNotIn("boston_medical_audio", keys_demo)
        self.assertNotIn("boston_medical_appointment", keys_demo)

        # Boston Medical admin MUST see BM structures
        res_bm = await self.client.get("/bm/prompt-base-structures", headers={"Authorization": f"Bearer {self.t_admin_boston}"})
        self.assertEqual(res_bm.status_code, 200)
        keys_bm = [item["structure_key"] for item in res_bm.json()]
        self.assertIn("boston_medical_audio", keys_bm)
        self.assertIn("boston_medical_appointment", keys_bm)


if __name__ == "__main__":
    unittest.main()
