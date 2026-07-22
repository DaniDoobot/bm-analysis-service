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


if __name__ == "__main__":
    import asyncio
    asyncio.run(unittest.main())
