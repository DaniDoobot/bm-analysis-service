"""
Unit and integration tests for safe removal/archival of structures and typologies with history.
Ensures:
- Historical analyses, versions, and criteria remain intact.
- Soft-delete is applied when history exists; hard-delete is applied only when 0 history.
- Typology deactivation unlinks active associations without affecting historical records.
- Inactive/deleted items are excluded from active listings unless include_archived=True.
- Idempotent deletion calls return 200 OK.
- Multitenancy and role-based guards enforce 403 on unauthorized attempts.
"""
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select
from httpx import AsyncClient, ASGITransport

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.prompts import PromptBaseStructure, Prompt, PromptVersion, BaseStructureTypology
from app.models.criteria import PromptCriterion, PromptCriterionTypology
from app.models.typologies import Typology
from app.models.analyses import Analysis, AnalysisCriterionResult
from app.utils.security import create_access_token, hash_password
from app.main import app
from app.dependencies import get_db


class TestSafeDeletionStructuresTypologies(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            pwd = hash_password("Password123!")

            # 1. Companies
            self.c_boston = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c_other = Company(company_id=2, company_name="Other Company", company_key="other-co", is_active=True)
            db.add_all([self.c_boston, self.c_other])
            await db.flush()

            # 2. Services
            self.s_commercial = Service(service_id=3, service_name="Asesores Comerciales", service_key="asesores-comerciales", company_id=1, is_active=True)
            self.s_other = Service(service_id=4, service_name="Otro Servicio", service_key="otro-servicio", company_id=2, is_active=True)
            db.add_all([self.s_commercial, self.s_other])
            await db.flush()

            # 3. Users
            self.u_super = User(user_id=1, username="superadmin", email="super@test.com", role="super_admin", password_hash=pwd, is_active=True)
            self.u_admin_boston = User(user_id=2, username="admin_boston", email="admin_boston@test.com", role="company_admin", company_id=1, password_hash=pwd, is_active=True)
            self.u_admin_other = User(user_id=3, username="admin_other", email="admin_other@test.com", role="company_admin", company_id=2, password_hash=pwd, is_active=True)
            self.u_agent_boston = User(user_id=4, username="agent_boston", email="agent_boston@test.com", role="agent", company_id=1, primary_service_id=3, password_hash=pwd, is_active=True)
            db.add_all([self.u_super, self.u_admin_boston, self.u_admin_other, self.u_agent_boston])
            await db.commit()

        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_admin_boston = create_access_token({"user_id": 2, "email": "admin_boston@test.com"})
        self.t_admin_other = create_access_token({"user_id": 3, "email": "admin_other@test.com"})
        self.t_agent_boston = create_access_token({"user_id": 4, "email": "agent_boston@test.com"})

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

    async def test_base_structure_with_history_safe_retirement(self):
        """
        Verify deleting a base structure with historical analyses:
        - Conflict (409) if confirm=False.
        - Safe retirement when confirm=True + confirm_active=True.
        - Dependent prompt soft-deleted; versions, criteria, and analyses intact.
        - Base structure marked deleted_at, structure_key freed with prefix.
        - Unlinked from listings; cannot spawn new prompts.
        - Original key can be reused for a new base structure.
        """
        # 1. Seed base structure "pruebas", typology, specific prompt, version, criterion, and analysis
        async with self.session_maker() as db:
            bs = PromptBaseStructure(
                id=10,
                structure_key="pruebas",
                structure_name="Pruebas Comerciales",
                base_prompt="System prompt base",
                prompt_type="text",
                service_id=3,
                company_id=1,
                is_global=False,
                is_active=True,
                owner_user_id=2
            )
            typ = Typology(
                typology_id=100,
                service_id=3,
                company_id=1,
                typology_key="venta_inbound",
                typology_name="Venta Inbound",
                is_active=True
            )
            db.add_all([bs, typ])
            await db.flush()

            bs_typ = BaseStructureTypology(base_structure_id=bs.id, typology_id=typ.typology_id)
            db.add(bs_typ)

            prompt = Prompt(
                prompt_id=50,
                company_id=1,
                service_id=3,
                prompt_name="Estructura Específica Pruebas",
                prompt_type="text",
                base_structure_id=bs.id,
                is_active=True,
                is_archived=False
            )
            db.add(prompt)
            await db.flush()

            version = PromptVersion(
                id=501,
                prompt_id=prompt.prompt_id,
                version_label="v1.0",
                is_current=True,
                prompt="Prompt v1 text"
            )
            criterion = PromptCriterion(
                criterion_id=701,
                prompt_id=prompt.prompt_id,
                criterion_key="identificacion_cliente",
                criterion_name="Identificación del cliente",
                criterion_type="boolean",
                is_active=True
            )
            db.add_all([version, criterion])
            await db.flush()

            analysis = Analysis(
                analysis_id=1001,
                company_id=1,
                service_id=3,
                prompt_id=prompt.prompt_id,
                prompt_version_id=version.id,
                call_id="call-test-001",
                evaluacion_global=Decimal("8.50"),
                status="completed"
            )
            db.add(analysis)
            await db.commit()

        headers = {"Authorization": f"Bearer {self.t_admin_boston}"}

        # 2. Try delete without confirm -> Expect 409 Conflict
        res = await self.client.delete("/bm/base-structures/10", headers=headers)
        self.assertEqual(res.status_code, 409)
        data = res.json()
        self.assertEqual(data.get("dependencies_count"), 1)

        # 3. Delete with confirm=true and confirm_active=true -> Expect 200 OK
        res = await self.client.delete("/bm/base-structures/10?confirm=true&confirm_active=true", headers=headers)
        self.assertEqual(res.status_code, 200)
        del_data = res.json()
        self.assertTrue(del_data["ok"])
        self.assertEqual(del_data["details"]["action"], "soft_deleted")
        self.assertTrue(del_data["details"]["deleted"])

        # 4. Verify database state
        async with self.session_maker() as db:
            # Base structure
            bs_check = (await db.execute(select(PromptBaseStructure).where(PromptBaseStructure.id == 10))).scalar_one()
            self.assertFalse(bs_check.is_active)
            self.assertIsNotNone(bs_check.deleted_at)
            self.assertTrue(bs_check.structure_key.startswith("deleted_"))

            # Dependent prompt (safe soft-deleted)
            p_check = (await db.execute(select(Prompt).where(Prompt.prompt_id == 50))).scalar_one()
            self.assertFalse(p_check.is_active)
            self.assertTrue(p_check.is_archived)
            self.assertIsNotNone(p_check.deleted_at)

            # Version, Criterion, Analysis intact
            v_check = (await db.execute(select(PromptVersion).where(PromptVersion.id == 501))).scalar_one_or_none()
            self.assertIsNotNone(v_check)
            c_check = (await db.execute(select(PromptCriterion).where(PromptCriterion.criterion_id == 701))).scalar_one_or_none()
            self.assertIsNotNone(c_check)
            a_check = (await db.execute(select(Analysis).where(Analysis.analysis_id == 1001))).scalar_one()
            self.assertEqual(a_check.prompt_id, 50)
            self.assertEqual(a_check.prompt_version_id, 501)
            self.assertEqual(float(a_check.evaluacion_global), 8.5)

            # BaseStructureTypology unlinked
            bst_check = (await db.execute(select(BaseStructureTypology).where(BaseStructureTypology.base_structure_id == 10))).scalars().all()
            self.assertEqual(len(bst_check), 0)

        # 5. List base structures should NOT return id=10 unless include_archived=true
        list_res = await self.client.get("/bm/prompt-base-structures?service_id=3", headers=headers)
        self.assertEqual(list_res.status_code, 200)
        items = list_res.json()
        self.assertFalse(any(i["id"] == 10 for i in items))

        list_arch_res = await self.client.get("/bm/prompt-base-structures?service_id=3&include_archived=true", headers=headers)
        self.assertEqual(list_arch_res.status_code, 200)
        arch_items = list_arch_res.json()
        self.assertTrue(any(i["id"] == 10 for i in arch_items))

        # 6. Cannot create prompt from deleted base structure
        from_base_res = await self.client.post("/bm/prompts/create-from-base", headers=headers, json={
            "base_structure_id": 10,
            "prompt_name": "Test From Deleted Base"
        })
        self.assertEqual(from_base_res.status_code, 400)

        # 7. Re-create new base structure with key "pruebas" -> Success
        create_res = await self.client.post("/bm/prompt-base-structures", headers=headers, json={
            "structure_key": "pruebas",
            "structure_name": "Nueva Pruebas Comerciales",
            "service_id": 3,
            "company_id": 1,
            "base_prompt": "Nuevo prompt base",
            "prompt_type": "text"
        })
        self.assertIn(create_res.status_code, (200, 201))

    async def test_specific_structure_with_history_safe_archival(self):
        """
        Verify deleting a specific structure that has analysis history:
        - Does NOT raise ValueError / 500 error.
        - Automatically soft-deletes / archives (is_active=False, is_archived=True, deleted_at=now).
        - Preserves versions, criteria, and analysis foreign keys intact.
        """
        async with self.session_maker() as db:
            prompt = Prompt(
                prompt_id=60,
                company_id=1,
                service_id=3,
                prompt_name="Estructura con Histórico",
                prompt_type="text",
                is_active=True,
                is_archived=False
            )
            db.add(prompt)
            await db.flush()

            v = PromptVersion(id=601, prompt_id=60, version_label="v1.0", is_current=True, prompt="v1")
            c = PromptCriterion(criterion_id=801, prompt_id=60, criterion_key="saludo_inicial", criterion_name="Saludo inicial", criterion_type="boolean", is_active=True)
            a = Analysis(analysis_id=2001, company_id=1, service_id=3, prompt_id=60, prompt_version_id=601, evaluacion_global=Decimal("9.0"))
            db.add_all([v, c, a])
            await db.commit()

        headers = {"Authorization": f"Bearer {self.t_admin_boston}"}
        res = await self.client.delete("/bm/prompts/60", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("archived"))
        self.assertFalse(data.get("hard_deleted", False))

        async with self.session_maker() as db:
            p = (await db.execute(select(Prompt).where(Prompt.prompt_id == 60))).scalar_one()
            self.assertFalse(p.is_active)
            self.assertTrue(p.is_archived)
            self.assertIsNotNone(p.deleted_at)

            # Analysis intact
            an = (await db.execute(select(Analysis).where(Analysis.analysis_id == 2001))).scalar_one()
            self.assertEqual(an.prompt_id, 60)
            self.assertEqual(an.prompt_version_id, 601)

    async def test_specific_structure_zero_history_hard_deletion(self):
        """
        Verify deleting a specific structure with ZERO history:
        - Physically removes the prompt, versions, and criteria from DB.
        """
        async with self.session_maker() as db:
            prompt = Prompt(
                prompt_id=70,
                company_id=1,
                service_id=3,
                prompt_name="Estructura Sin Uso",
                prompt_type="text",
                is_active=True,
                is_archived=False
            )
            db.add(prompt)
            await db.flush()

            v = PromptVersion(id=701, prompt_id=70, version_label="v1.0", is_current=True, prompt="v1")
            c = PromptCriterion(criterion_id=901, prompt_id=70, criterion_key="crit_test", criterion_name="Crit", criterion_type="boolean", is_active=True)
            db.add_all([v, c])
            await db.commit()

        headers = {"Authorization": f"Bearer {self.t_admin_boston}"}
        res = await self.client.delete("/bm/prompts/70", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("deleted"))

        async with self.session_maker() as db:
            p = (await db.execute(select(Prompt).where(Prompt.prompt_id == 70))).scalar_one_or_none()
            self.assertIsNone(p)
            v = (await db.execute(select(PromptVersion).where(PromptVersion.id == 701))).scalar_one_or_none()
            self.assertIsNone(v)

    async def test_typology_safe_archival_and_recreation(self):
        """
        Verify typology deactivation:
        - Soft-deletes typology (is_active=False).
        - Unlinks from BaseStructureTypology and PromptCriterionTypology.
        - Historical analyses remain intact.
        - Excluded from default list; included with include_archived=true.
        - Recreating same key restores it.
        """
        async with self.session_maker() as db:
            typ = Typology(
                typology_id=200,
                service_id=3,
                company_id=1,
                typology_key="reclamo_facturacion",
                typology_name="Reclamo Facturación",
                is_active=True
            )
            bs = PromptBaseStructure(
                id=30,
                structure_key="bs_rec",
                structure_name="BS Reclamos",
                base_prompt="BS",
                prompt_type="text",
                service_id=3,
                company_id=1,
                is_active=True
            )
            p = Prompt(prompt_id=80, company_id=1, service_id=3, prompt_name="P Reclamos", prompt_type="text", is_active=True)
            db.add_all([typ, bs, p])
            await db.flush()

            c = PromptCriterion(criterion_id=950, prompt_id=80, criterion_key="crit_reclamo", criterion_name="Crit Reclamo", criterion_type="boolean", is_active=True)
            db.add(c)
            await db.flush()

            bst = BaseStructureTypology(base_structure_id=30, typology_id=200)
            pct = PromptCriterionTypology(criterion_id=950, typology_id=200)
            a = Analysis(analysis_id=3001, company_id=1, service_id=3, evaluacion_global=Decimal("7.0"))
            db.add(a)
            await db.flush()

            acr = AnalysisCriterionResult(
                id=5001,
                analysis_id=3001,
                criterion_id=950,
                prompt_id=80,
                typology_id=200,
                typology_key="reclamo_facturacion"
            )
            db.add_all([bst, pct, acr])
            await db.commit()

        headers = {"Authorization": f"Bearer {self.t_admin_boston}"}

        # Delete typology
        res = await self.client.delete("/bm/typologies/200", headers=headers)
        self.assertEqual(res.status_code, 200)

        # Check DB
        async with self.session_maker() as db:
            t_check = (await db.execute(select(Typology).where(Typology.typology_id == 200))).scalar_one()
            self.assertFalse(t_check.is_active)

            bst_count = (await db.execute(select(BaseStructureTypology).where(BaseStructureTypology.typology_id == 200))).scalars().all()
            self.assertEqual(len(bst_count), 0)

            pct_count = (await db.execute(select(PromptCriterionTypology).where(PromptCriterionTypology.typology_id == 200))).scalars().all()
            self.assertEqual(len(pct_count), 0)

            acr_check = (await db.execute(select(AnalysisCriterionResult).where(AnalysisCriterionResult.analysis_id == 3001))).scalar_one()
            self.assertEqual(acr_check.typology_id, 200)

        # Default listing should not include it
        list_res = await self.client.get("/bm/typologies?service_id=3", headers=headers)
        self.assertEqual(list_res.status_code, 200)
        items = list_res.json()
        self.assertFalse(any(t["typology_id"] == 200 for t in items))

        # Listing with include_archived=true should include it
        list_arch_res = await self.client.get("/bm/typologies?service_id=3&include_archived=true", headers=headers)
        self.assertEqual(list_arch_res.status_code, 200)
        arch_items = list_arch_res.json()
        self.assertTrue(any(t["typology_id"] == 200 for t in arch_items))

        # Recreating restores it
        create_res = await self.client.post("/bm/typologies", headers=headers, json={
            "service_id": 3,
            "typology_key": "reclamo_facturacion",
            "typology_name": "Reclamo Facturación Restaurada"
        })
        self.assertEqual(create_res.status_code, 201)

        async with self.session_maker() as db:
            t_restored = (await db.execute(select(Typology).where(Typology.typology_id == 200))).scalar_one()
            self.assertTrue(t_restored.is_active)
            self.assertEqual(t_restored.typology_name, "Reclamo Facturación Restaurada")

    async def test_idempotent_deletions(self):
        """
        Verify deleting already-deleted items returns clean 200 OK without errors.
        """
        async with self.session_maker() as db:
            bs = PromptBaseStructure(
                id=40,
                structure_key="bs_idemp",
                structure_name="BS Idempotent",
                base_prompt="BS",
                prompt_type="text",
                service_id=3,
                company_id=1,
                is_active=False,
                deleted_at=datetime.now(timezone.utc)
            )
            p = Prompt(
                prompt_id=90,
                company_id=1,
                service_id=3,
                prompt_name="P Idemp",
                prompt_type="text",
                is_active=False,
                is_archived=True,
                deleted_at=datetime.now(timezone.utc)
            )
            db.add_all([bs, p])
            await db.commit()

        headers = {"Authorization": f"Bearer {self.t_admin_boston}"}

        # Delete already-deleted base structure
        bs_res = await self.client.delete("/bm/base-structures/40", headers=headers)
        self.assertEqual(bs_res.status_code, 200)
        self.assertTrue(bs_res.json().get("ok"))

        # Delete already-deleted prompt
        p_res = await self.client.delete("/bm/prompts/90", headers=headers)
        self.assertEqual(p_res.status_code, 200)
        self.assertTrue(p_res.json().get("archived"))

    async def test_role_and_tenant_guards(self):
        """
        Verify security guards:
        - AGENT role gets 403 Forbidden on base structure, prompt, and typology deletion.
        - COMPANY_ADMIN of Company 2 gets 403 Forbidden on resources of Company 1.
        """
        async with self.session_maker() as db:
            bs = PromptBaseStructure(
                id=55,
                structure_key="bs_guarded",
                structure_name="BS Guarded",
                base_prompt="BS",
                prompt_type="text",
                service_id=3,
                company_id=1,
                is_active=True
            )
            p = Prompt(prompt_id=95, company_id=1, service_id=3, prompt_name="P Guarded", prompt_type="text", is_active=True)
            typ = Typology(typology_id=300, service_id=3, company_id=1, typology_key="typ_guarded", typology_name="Typ Guarded", is_active=True)
            db.add_all([bs, p, typ])
            await db.commit()

        agent_headers = {"Authorization": f"Bearer {self.t_agent_boston}"}
        other_admin_headers = {"Authorization": f"Bearer {self.t_admin_other}"}

        # 1. Agent role blocked (403)
        res1 = await self.client.delete("/bm/base-structures/55", headers=agent_headers)
        self.assertEqual(res1.status_code, 403)

        res2 = await self.client.delete("/bm/prompts/95", headers=agent_headers)
        self.assertEqual(res2.status_code, 403)

        res3 = await self.client.delete("/bm/typologies/300", headers=agent_headers)
        self.assertEqual(res3.status_code, 403)

        # 2. Company 2 admin blocked on Company 1 resource (403)
        res4 = await self.client.delete("/bm/base-structures/55", headers=other_admin_headers)
        self.assertEqual(res4.status_code, 403)

        res5 = await self.client.delete("/bm/prompts/95", headers=other_admin_headers)
        self.assertEqual(res5.status_code, 403)

        res6 = await self.client.delete("/bm/typologies/300", headers=other_admin_headers)
        self.assertEqual(res6.status_code, 403)


if __name__ == "__main__":
    unittest.main()
