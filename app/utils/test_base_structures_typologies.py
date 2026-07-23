import os
import sys
import unittest
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///base_structures_typologies_test.db"

# Safety Confirmation Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution was blocked because DATABASE_URL points to production!")

# Setup path
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

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.typologies import Typology
from app.models.prompts import PromptBaseStructure, BaseStructureTypology, Prompt, PromptVersion
from app.utils.security import create_access_token
from app.main import app

from sqlalchemy.ext.asyncio import AsyncSession


class TestBaseStructuresTypologies(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("base_structures_typologies_test.db"):
            try:
                os.remove("base_structures_typologies_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = get_engine()
        async with AsyncSession(self.session_factory) as db:
            # 1. Companies
            self.c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c2 = Company(company_id=2, company_name="GesDent", company_key="gesdent", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()

            # 2. Services
            self.s1 = Service(service_id=1, service_name="Front Desk Boston", service_key="front-boston", company_id=self.c1.company_id)
            self.s2 = Service(service_id=2, service_name="Experiencia GesDent", service_key="experiencia-gesdent", company_id=self.c2.company_id)
            db.add_all([self.s1, self.s2])
            await db.flush()

            # 3. Users
            self.u_super = User(user_id=1, username="super_admin", email="super@test.com", role="admin", password_hash="dummy")
            self.u_comp1 = User(user_id=2, username="boston_admin", email="boston_admin@test.com", role="company_admin", company_id=self.c1.company_id, password_hash="dummy")
            self.u_comp2 = User(user_id=3, username="gesdent_admin", email="gesdent_admin@test.com", role="company_admin", company_id=self.c2.company_id, password_hash="dummy")
            self.u_agent = User(user_id=4, username="boston_agent", email="agent@test.com", role="agente", company_id=self.c1.company_id, hubspot_owner_id="owner_1", password_hash="dummy")
            db.add_all([self.u_super, self.u_comp1, self.u_comp2, self.u_agent])
            await db.flush()

            # 4. Typologies for Service 1
            self.typo1 = Typology(typology_id=1, typology_key="citacion", typology_name="Citación", service_id=self.s1.service_id, company_id=self.c1.company_id, description="Llamada de citación", is_active=True)
            self.typo2 = Typology(typology_id=2, typology_key="reclamacion", typology_name="Reclamación", service_id=self.s1.service_id, company_id=self.c1.company_id, description="Llamada de reclamación", is_active=True)
            db.add_all([self.typo1, self.typo2])
            await db.flush()

            await db.commit()

        # Build Tokens
        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_boston_admin = create_access_token({"user_id": 2, "email": "boston_admin@test.com"})
        self.t_gesdent_admin = create_access_token({"user_id": 3, "email": "gesdent_admin@test.com"})
        self.t_agent = create_access_token({"user_id": 4, "email": "agent@test.com"})

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        if os.path.exists("base_structures_typologies_test.db"):
            try:
                os.remove("base_structures_typologies_test.db")
            except Exception:
                pass

    # ── 1. Crear base con tipologías genera esqueleto ─────────────────────────

    async def test_create_base_structure_with_typologies_generates_skeleton(self):
        res = await self.client.post(
            "/bm/prompt-base-structures",
            json={
                "structure_key": "base_with_typos",
                "structure_name": "Base Con Tipologías",
                "service_id": 1,
                "typology_ids": [1, 2]
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        base_prompt = data.get("base_prompt") or data.get("structure", {}).get("base_prompt")
        self.assertIn("### CONTEXTO", base_prompt)
        self.assertIn("### DEFINICIÓN DE TIPOS DE LLAMADA", base_prompt)
        self.assertIn("- citacion: Llamada de citación", base_prompt)
        self.assertIn("- reclamacion: Llamada de reclamación", base_prompt)
        self.assertIn("1. citacion", base_prompt)
        self.assertIn("2. reclamacion", base_prompt)

    # ── 2. Crear base sin tipologías no añade todas las del servicio ─────────

    async def test_create_base_structure_without_typologies_does_not_add_service_typologies(self):
        res = await self.client.post(
            "/bm/prompt-base-structures",
            json={
                "structure_key": "base_empty_typos",
                "structure_name": "Base Sin Tipologías",
                "service_id": 1,
                "typology_ids": []
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        base_prompt = data.get("base_prompt") or data.get("structure", {}).get("base_prompt")
        self.assertIn("### CONTEXTO", base_prompt)
        self.assertIn("No hay tipologías definidas para esta estructura base.", base_prompt)
        self.assertIn("No hay prioridades de tipologías definidas para esta estructura base.", base_prompt)
        for forbidden in ["cita:", "confirmacion:", "cancelacion:", "reagendo:", "falta:", "otros:"]:
            self.assertNotIn(forbidden, base_prompt)

    # ── 3. Estructura específica asociada a base sin tipologías muestra 0 tipologías ──

    async def test_specific_structure_with_empty_base_structure_shows_zero_typologies(self):
        # Create base structure with 0 typologies
        async with AsyncSession(self.session_factory) as db:
            base_struct = PromptBaseStructure(
                structure_key="base_empty_1",
                structure_name="Base Vacía 1",
                prompt_type="text",
                base_prompt="### CONTEXTO\nTest",
                service_id=1,
                company_id=1,
                owner_user_id=2
            )
            db.add(base_struct)
            await db.flush()

            # Create specific prompt linked to this base structure
            p = Prompt(
                prompt_id=101,
                prompt_name="Específica Base Vacía",
                prompt_type="audio",
                base_structure_id=base_struct.id,
                service_id=1,
                company_id=1,
                owner_user_id=2,
                is_active=True
            )
            db.add(p)
            await db.flush()
            pv = PromptVersion(prompt_id=p.prompt_id, prompt="Test prompt", version_label="v1", is_current=True)
            db.add(pv)
            prompt_id = p.prompt_id
            await db.commit()

        # Query active typologies for this specific prompt
        from app.models.prompts import BaseStructureTypology
        from sqlalchemy import select
        async with AsyncSession(self.session_factory) as db:
            p_res = await db.get(Prompt, prompt_id)
            if p_res and p_res.base_structure_id:
                t_stmt = (
                    select(Typology)
                    .join(BaseStructureTypology, BaseStructureTypology.typology_id == Typology.typology_id)
                    .where(
                        BaseStructureTypology.base_structure_id == p_res.base_structure_id,
                        Typology.is_active == True
                    )
                )
                t_res = await db.execute(t_stmt)
                active_typos = t_res.scalars().all()
            else:
                active_typos = []
            self.assertEqual(len(active_typos), 0)

    # ── 4. DELETE sin confirmación teniendo dependencias devuelve 409 Conflict ──

    async def test_delete_base_structure_without_confirm_returns_409_when_dependencies_exist(self):
        async with AsyncSession(self.session_factory) as db:
            base_struct = PromptBaseStructure(
                structure_key="base_dep_test",
                structure_name="Base Con Dependencias",
                prompt_type="text",
                base_prompt="Test",
                service_id=1,
                company_id=1,
                owner_user_id=2
            )
            db.add(base_struct)
            await db.flush()

            p = Prompt(
                prompt_id=102,
                prompt_name="Prompt Dependiente",
                prompt_type="audio",
                base_structure_id=base_struct.id,
                service_id=1,
                company_id=1,
                owner_user_id=2,
                is_active=True
            )
            db.add(p)
            base_id = base_struct.id
            await db.commit()

        # Try to delete without confirm/force
        res = await self.client.delete(
            f"/bm/prompt-base-structures/{base_id}",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 409)
        data = res.json()
        self.assertTrue(data["has_dependencies"])
        self.assertEqual(data["dependencies_count"], 1)

    # ── 5. DELETE con confirmación elimina en cascada dentro del scope ────────

    async def test_delete_base_structure_with_confirm_deletes_cascade_within_scope(self):
        async with AsyncSession(self.session_factory) as db:
            base_struct = PromptBaseStructure(
                structure_key="base_cascade_test",
                structure_name="Base Borrado Cascada",
                prompt_type="text",
                base_prompt="Test",
                service_id=1,
                company_id=1,
                owner_user_id=2
            )
            db.add(base_struct)
            await db.flush()

            p = Prompt(
                prompt_id=103,
                prompt_name="Prompt A Borrar",
                prompt_type="audio",
                base_structure_id=base_struct.id,
                service_id=1,
                company_id=1,
                owner_user_id=2,
                is_active=True
            )
            db.add(p)
            base_id = base_struct.id
            prompt_id = p.prompt_id
            await db.commit()

        # Delete with confirm=true + confirm_active=true (prompt is active → requires explicit confirmation)
        res = await self.client.delete(
            f"/bm/prompt-base-structures/{base_id}?confirm=true&confirm_active=true",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # Response must include count of deleted active prompts
        self.assertEqual(data["details"]["deleted_active_prompts_count"], 1)

        # Verify base structure and prompt are gone from DB
        async with AsyncSession(self.session_factory) as db:
            check_base = await db.get(PromptBaseStructure, base_id)
            check_prompt = await db.get(Prompt, prompt_id)
            self.assertIsNone(check_base)
            self.assertIsNone(check_prompt)

    # ── 6. Usuario fuera de scope no puede borrar ─────────────────────────────

    async def test_delete_base_structure_out_of_scope_user_returns_403(self):
        async with AsyncSession(self.session_factory) as db:
            base_struct = PromptBaseStructure(
                structure_key="base_boston_private",
                structure_name="Base Privada Boston",
                prompt_type="text",
                base_prompt="Test",
                service_id=1,
                company_id=1,
                owner_user_id=2
            )
            db.add(base_struct)
            await db.flush()
            base_id = base_struct.id
            await db.commit()

        # Gesdent admin tries to delete Boston base structure -> 403 Forbidden
        res = await self.client.delete(
            f"/bm/prompt-base-structures/{base_id}?confirm=true",
            headers={"Authorization": f"Bearer {self.t_gesdent_admin}"}
        )
        self.assertEqual(res.status_code, 403)

    # ── Active-prompt consistency tests ──────────────────────────────────────

    async def _create_prompt_with_version(self, db, prompt_id, prompt_type, service_id, company_id, is_active, version_id):
        """Helper: create a Prompt + PromptVersion, return both."""
        p = Prompt(
            prompt_id=prompt_id,
            prompt_name=f"Prompt {prompt_id}",
            prompt_type=prompt_type,
            service_id=service_id,
            company_id=company_id,
            owner_user_id=2,
            is_active=is_active,
        )
        db.add(p)
        await db.flush()
        v = PromptVersion(
            id=version_id,
            prompt_id=p.prompt_id,
            prompt="Test prompt text",
            version_label="v1",
            is_current=is_active,
        )
        db.add(v)
        await db.flush()
        return p, v

    async def test_active_prompt_found_for_service(self):
        """get_active_prompt returns the active prompt for a given service."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            p, v = await self._create_prompt_with_version(
                db, prompt_id=200, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=200
            )
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            result = await prompts_service.get_active_prompt(db, prompt_type="audio", service_id=1)
            self.assertIsNotNone(result, "Expected active audio prompt for service 1 but got None")
            self.assertEqual(result["prompt_id"], 200)

    async def test_activate_deactivates_same_tenant_service_type(self):
        """Activating a prompt deactivates others with same company+service+type."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            # Two audio prompts for same company+service
            p1, v1 = await self._create_prompt_with_version(
                db, prompt_id=301, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=301
            )
            p2, v2 = await self._create_prompt_with_version(
                db, prompt_id=302, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=302
            )
            await db.commit()

        # Activate p2 → p1 must become inactive
        async with AsyncSession(self.session_factory) as db:
            await prompts_service.activate_version(db, version_id=302)

        async with AsyncSession(self.session_factory) as db:
            p1_check = await db.get(Prompt, 301)
            p2_check = await db.get(Prompt, 302)
            self.assertFalse(p1_check.is_active, "p1 (same tenant/service/type) should be deactivated")
            self.assertTrue(p2_check.is_active, "p2 should be active after activation")

    async def test_activate_does_not_deactivate_other_service(self):
        """Activating Front/audio does NOT deactivate Asesores/audio (different service)."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            # Front audio (service 1, company 1) — active
            p1, v1 = await self._create_prompt_with_version(
                db, prompt_id=401, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=401
            )
            # GesDent audio (service 2, company 2) — active
            p2, v2 = await self._create_prompt_with_version(
                db, prompt_id=402, prompt_type="audio",
                service_id=2, company_id=2, is_active=True, version_id=402
            )
            # Another Front audio to activate
            p3, v3 = await self._create_prompt_with_version(
                db, prompt_id=403, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=403
            )
            await db.commit()

        # Activate p3 (Front/audio) → p2 (GesDent/audio) must remain active
        async with AsyncSession(self.session_factory) as db:
            await prompts_service.activate_version(db, version_id=403)

        async with AsyncSession(self.session_factory) as db:
            p1_check = await db.get(Prompt, 401)
            p2_check = await db.get(Prompt, 402)
            p3_check = await db.get(Prompt, 403)
            self.assertFalse(p1_check.is_active, "p1 (same tenant/service) should be deactivated")
            self.assertTrue(p2_check.is_active, "p2 (different company+service) must remain active")
            self.assertTrue(p3_check.is_active, "p3 should be the new active prompt")

    async def test_activate_does_not_deactivate_other_company_same_service_id(self):
        """Activating a prompt does NOT deactivate another company's prompt even if service_id coincides numerically."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            # Company 1, service 1, audio — active
            p1, v1 = await self._create_prompt_with_version(
                db, prompt_id=501, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=501
            )
            # Company 2 also has service_id=2 but same prompt_type; activate company 1's new prompt
            p_other_company, v_other = await self._create_prompt_with_version(
                db, prompt_id=502, prompt_type="audio",
                service_id=2, company_id=2, is_active=True, version_id=502
            )
            # New company 1 prompt to activate
            p_new, v_new = await self._create_prompt_with_version(
                db, prompt_id=503, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=503
            )
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            await prompts_service.activate_version(db, version_id=503)

        async with AsyncSession(self.session_factory) as db:
            p1_check = await db.get(Prompt, 501)
            p_other_check = await db.get(Prompt, 502)
            p_new_check = await db.get(Prompt, 503)
            self.assertFalse(p1_check.is_active, "Old company-1 prompt should be deactivated")
            self.assertTrue(p_other_check.is_active, "Other company prompt must NOT be deactivated")
            self.assertTrue(p_new_check.is_active, "New prompt must be active")


    # ── Regresión: unicidad exclusiva de estructura activa ────────────────────

    async def test_activate_toggle_a_b_deactivates_previous(self):
        """15a. Activar A → A activo, B inactivo. Luego activar B → B activo, A inactivo."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            pA, vA = await self._create_prompt_with_version(
                db, prompt_id=601, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=601
            )
            pB, vB = await self._create_prompt_with_version(
                db, prompt_id=602, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=602
            )
            await db.commit()

        # Activate A
        async with AsyncSession(self.session_factory) as db:
            await prompts_service.activate_version(db, version_id=601)
        async with AsyncSession(self.session_factory) as db:
            self.assertTrue((await db.get(Prompt, 601)).is_active, "A should be active after first activation")
            self.assertFalse((await db.get(Prompt, 602)).is_active, "B should be inactive")

        # Activate B → A must become inactive
        async with AsyncSession(self.session_factory) as db:
            await prompts_service.activate_version(db, version_id=602)
        async with AsyncSession(self.session_factory) as db:
            self.assertFalse((await db.get(Prompt, 601)).is_active, "A must be deactivated when B is activated")
            self.assertTrue((await db.get(Prompt, 602)).is_active, "B should now be active")

    async def test_deactivate_competing_leaves_only_one_active(self):
        """15b. Simulates a direct call to deactivate_competing_prompts: starts with two actives, ends with one."""
        from app.services.prompts_service import deactivate_competing_prompts
        async with AsyncSession(self.session_factory) as db:
            pA, _ = await self._create_prompt_with_version(
                db, prompt_id=611, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=611
            )
            pB, _ = await self._create_prompt_with_version(
                db, prompt_id=612, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=612
            )
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            target = await db.get(Prompt, 612)
            await deactivate_competing_prompts(db, target)
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            # pA should be deactivated, pB untouched
            self.assertFalse((await db.get(Prompt, 611)).is_active, "pA must be deactivated")
            self.assertTrue((await db.get(Prompt, 612)).is_active, "pB (target) must remain active")

    async def test_duplicate_prompt_starts_inactive(self):
        """15c. Duplicating an active prompt must NOT create a second active prompt."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            pOrig, vOrig = await self._create_prompt_with_version(
                db, prompt_id=621, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=621
            )
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            result = await prompts_service.duplicate_prompt(
                db, source_prompt_id=621,
                prompt_name="Copia Front Audio",
                created_by="test",
                created_by_email="test@test.com",
            )

        # Original must still be active; copy must be inactive
        async with AsyncSession(self.session_factory) as db:
            self.assertTrue((await db.get(Prompt, 621)).is_active, "Original must remain active")
            copy_id = result["prompt_id"]
            copy = await db.get(Prompt, copy_id)
            self.assertFalse(copy.is_active, "Duplicated prompt must start as inactive")

    async def test_activating_one_service_does_not_affect_another_service(self):
        """15d. Activating Front/audio does NOT touch Asesores/audio (different service_id)."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            # Service 2 = Experiencia GesDent (already in setUp)
            p_front, v_front = await self._create_prompt_with_version(
                db, prompt_id=631, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=631
            )
            p_asesores_active, v_asesores = await self._create_prompt_with_version(
                db, prompt_id=632, prompt_type="audio",
                service_id=2, company_id=2, is_active=True, version_id=632
            )
            p_front_new, v_front_new = await self._create_prompt_with_version(
                db, prompt_id=633, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=633
            )
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            await prompts_service.activate_version(db, version_id=633)

        async with AsyncSession(self.session_factory) as db:
            self.assertFalse((await db.get(Prompt, 631)).is_active, "Old Front prompt must be deactivated")
            self.assertTrue((await db.get(Prompt, 632)).is_active, "Asesores/audio must remain active (different service)")
            self.assertTrue((await db.get(Prompt, 633)).is_active, "New Front prompt must be active")

    async def test_null_service_id_handled_correctly(self):
        """15e. service_id=NULL: activating prompt with service_id=NULL only deactivates others with same company+NULL service."""
        from app.services.prompts_service import deactivate_competing_prompts
        async with AsyncSession(self.session_factory) as db:
            # Two prompts: same company, no service (NULL)
            pNull1, _ = await self._create_prompt_with_version(
                db, prompt_id=641, prompt_type="audio",
                service_id=None, company_id=1, is_active=True, version_id=641
            )
            pNull2, _ = await self._create_prompt_with_version(
                db, prompt_id=642, prompt_type="audio",
                service_id=None, company_id=1, is_active=True, version_id=642
            )
            # Unrelated: has service_id=1, should NOT be touched
            pWithService, _ = await self._create_prompt_with_version(
                db, prompt_id=643, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=643
            )
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            target = await db.get(Prompt, 642)
            await deactivate_competing_prompts(db, target)
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            self.assertFalse((await db.get(Prompt, 641)).is_active, "pNull1 must be deactivated (same company + NULL service)")
            self.assertTrue((await db.get(Prompt, 642)).is_active, "Target (pNull2) must remain active")
            self.assertTrue((await db.get(Prompt, 643)).is_active, "Prompt with service_id=1 must NOT be touched")

    async def test_get_active_prompt_returns_at_most_one(self):
        """15f. get_active_prompt never returns None when one active exists, and never returns duplicates."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            p1, v1 = await self._create_prompt_with_version(
                db, prompt_id=651, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=651
            )
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            await prompts_service.activate_version(db, version_id=651)

        async with AsyncSession(self.session_factory) as db:
            result = await prompts_service.get_active_prompt(db, prompt_type="audio", service_id=1)
            self.assertIsNotNone(result, "Expected an active prompt for service 1")
            self.assertEqual(result["prompt_id"], 651, "Should return the activated prompt")


    async def test_activating_legacy_null_company_deactivates_tenant_prompt_and_sets_company_id(self):
        """16a. Activar B (company=NULL, service=1) → B asigna company=1, A (company=1, service=1) pasa a inactivo."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            pA, _ = await self._create_prompt_with_version(
                db, prompt_id=701, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=701
            )
            pB, _ = await self._create_prompt_with_version(
                db, prompt_id=702, prompt_type="audio",
                service_id=1, company_id=None, is_active=False, version_id=702
            )
            await db.commit()

        # Activate B (legacy NULL company)
        async with AsyncSession(self.session_factory) as db:
            await prompts_service.set_prompt_active_status(db, prompt_id=702, is_active=True)

        async with AsyncSession(self.session_factory) as db:
            pA_db = await db.get(Prompt, 701)
            pB_db = await db.get(Prompt, 702)
            self.assertFalse(pA_db.is_active, "A must be deactivated when B is activated")
            self.assertTrue(pB_db.is_active, "B must be active")
            self.assertEqual(pB_db.company_id, 1, "B must inherit company_id=1 from service")

        # Activate A again
        async with AsyncSession(self.session_factory) as db:
            await prompts_service.set_prompt_active_status(db, prompt_id=701, is_active=True)

        async with AsyncSession(self.session_factory) as db:
            pA_db = await db.get(Prompt, 701)
            pB_db = await db.get(Prompt, 702)
            self.assertTrue(pA_db.is_active, "A must be active")
            self.assertFalse(pB_db.is_active, "B must be deactivated when A is activated")

    async def test_patch_endpoint_toggles_active_status(self):
        """16b. PATCH /bm/prompts/{id} activa B y desactiva A correctamente."""
        from app.services import prompts_service
        async with AsyncSession(self.session_factory) as db:
            pA, _ = await self._create_prompt_with_version(
                db, prompt_id=711, prompt_type="audio",
                service_id=1, company_id=1, is_active=True, version_id=711
            )
            pB, _ = await self._create_prompt_with_version(
                db, prompt_id=712, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=712
            )
            await db.commit()

        # Call PATCH /bm/prompts/712
        res = await self.client.patch(
            "/bm/prompts/712",
            json={"is_active": True},
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["is_active"])

        async with AsyncSession(self.session_factory) as db:
            self.assertFalse((await db.get(Prompt, 711)).is_active, "pA must be deactivated by PATCH on pB")
            self.assertTrue((await db.get(Prompt, 712)).is_active, "pB must be active")

    async def test_publish_draft_deactivates_legacy_null_competitors(self):
        """16c. publicar borrador desactiva competidores legacy NULL."""
        from app.models.drafts import PromptDraft
        from app.services import drafts_service
        async with AsyncSession(self.session_factory) as db:
            pLegacy, _ = await self._create_prompt_with_version(
                db, prompt_id=721, prompt_type="audio",
                service_id=1, company_id=None, is_active=True, version_id=721
            )
            pNew, _ = await self._create_prompt_with_version(
                db, prompt_id=722, prompt_type="audio",
                service_id=1, company_id=1, is_active=False, version_id=722
            )
            draft = PromptDraft(
                draft_id=7220,
                prompt_id=722,
                draft_name="Draft 722",
                draft_data={"prompt": "Texto nuevo", "version_name": "v2"},
                updated_by="tester",
                updated_by_email="test@test.com",
                status="draft"
            )
            db.add(draft)
            await db.commit()

        async with AsyncSession(self.session_factory) as db:
            await drafts_service.publish_draft(db, draft_id=7220)

        async with AsyncSession(self.session_factory) as db:
            self.assertFalse((await db.get(Prompt, 721)).is_active, "Legacy prompt must be deactivated upon draft publication")
            self.assertTrue((await db.get(Prompt, 722)).is_active, "Published prompt must be active")


if __name__ == "__main__":
    import asyncio
    asyncio.run(unittest.main())

