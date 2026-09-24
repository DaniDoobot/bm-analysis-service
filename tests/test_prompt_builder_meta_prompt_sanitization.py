import unittest
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from httpx import AsyncClient, ASGITransport

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion, PromptBaseStructure
from app.models.typologies import Typology
from app.services.prompt_builder import _build_meta_prompt, build_prompt_with_ai
from app.utils.security import create_access_token, hash_password
from app.main import app
from app.dependencies import get_db

class TestPromptBuilderMetaPromptSanitization(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        class TypoMock:
            def __init__(self, key, name):
                self.typology_key = key
                self.key = key
                self.typology_name = name
                self.description = f"Llamada clasificada como {name}."
                self.sort_order = 1

        self.demo_typologies = [
            TypoMock("consulta_general", "Consulta General"),
            TypoMock("soporte_tecnico", "Soporte Técnico"),
            TypoMock("reclamacion_incidencia", "Reclamación o Incidencia"),
            TypoMock("facturacion_cobros", "Facturación y Cobros"),
        ]

        self.legacy_typos = [
            "informacion_sin_cita",
            "falta_con_reagendo",
            "falta_sin_reagendo",
            "no_interesado",
            "no_apto",
        ]

    def test_meta_prompt_does_not_contain_legacy_typos(self):
        """1. Verify that _build_meta_prompt does NOT contain any legacy typo keywords."""
        meta_prompt = _build_meta_prompt(
            current_prompt_text=None,
            criteria=[],
            general_instructions="Instrucciones para atención",
            draft_data=None,
            base_structure=None,
            typologies=self.demo_typologies,
            criterion_typologies_map={},
            sanitized_base_prompt=None,
        )

        for lt in self.legacy_typos:
            self.assertNotIn(
                lt,
                meta_prompt,
                f"Legacy typology '{lt}' was found in the generated meta-prompt!"
            )

    def test_meta_prompt_includes_exact_active_typologies_and_strict_prohibition(self):
        """2. Verify meta-prompt includes exact active typologies and clear prohibition without mentioning forbidden words."""
        meta_prompt = _build_meta_prompt(
            current_prompt_text=None,
            criteria=[],
            general_instructions=None,
            draft_data=None,
            base_structure=None,
            typologies=self.demo_typologies,
            criterion_typologies_map={},
            sanitized_base_prompt=None,
        )

        # Check allowed typologies are present
        for t in self.demo_typologies:
            self.assertIn(t.typology_key, meta_prompt)

        # Check prohibition phrasing is present
        self.assertIn("prohibir taxativamente el uso o invención de cualquier otra tipología", meta_prompt)


class TestPromptBuilderAsyncIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            pwd = hash_password("Password123!")

            # 1. Company
            c_demo = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True, is_active=True)
            db.add(c_demo)
            await db.flush()

            # 2. Service
            s_demo = Service(service_id=10, service_name="Atención al Cliente", service_key="atencion-demo", company_id=7, is_active=True)
            db.add(s_demo)
            await db.flush()

            # 3. User
            u_admin = User(user_id=1, username="admin_demo", email="admin_demo@test.com", role="company_admin", company_id=7, password_hash=pwd, is_active=True)
            db.add(u_admin)
            await db.flush()

            # 4. Typologies
            t1 = Typology(typology_id=101, service_id=10, typology_key="consulta_general", typology_name="Consulta General", is_active=True, sort_order=1)
            t2 = Typology(typology_id=102, service_id=10, typology_key="soporte_tecnico", typology_name="Soporte Técnico", is_active=True, sort_order=2)
            t3 = Typology(typology_id=103, service_id=10, typology_key="reclamacion_incidencia", typology_name="Reclamación o Incidencia", is_active=True, sort_order=3)
            t4 = Typology(typology_id=104, service_id=10, typology_key="facturacion_cobros", typology_name="Facturación y Cobros", is_active=True, sort_order=4)
            db.add_all([t1, t2, t3, t4])
            await db.flush()

            # 5. Prompt & Version
            p_demo = Prompt(prompt_id=70, prompt_name="EE Att Cliente Demo1", prompt_type="audio", service_id=10, company_id=7, is_active=True)
            db.add(p_demo)
            await db.flush()

            v_demo = PromptVersion(id=1, prompt_id=70, prompt="Prompt inicial", version_label="v1.0", version_name="Versión Inicial", is_current=True)
            db.add(v_demo)
            await db.commit()

    async def asyncTearDown(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self.engine.dispose()

    @patch("app.services.openai_service.complete_text")
    async def test_build_prompt_with_ai_success_when_clean(self, mock_complete_text):
        """3. Confirm build_prompt_with_ai succeeds when LLM output is clean of legacy typologies."""
        clean_llm_json = """{
            "generated_name": "Versión Limpia Demo",
            "change_summary": "Estructura generada correctamente",
            "generated_prompt": "### REGLAS\\nEl analizador clasifica en: consulta_general, soporte_tecnico, reclamacion_incidencia, facturacion_cobros.\\nQueda taxativamente prohibido inventar cualquier otra tipología.\\n### FORMATO\\n{\\"tipo_llamada\\": \\"consulta_general\\"}",
            "improved_criteria_descriptions": []
        }"""
        mock_complete_text.return_value = clean_llm_json

        async with self.session_maker() as db:
            result = await build_prompt_with_ai(
                db=db,
                prompt_id=70,
                instructions="Refuerza la atención al cliente",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["generated_name"], "Versión Limpia Demo")

    @patch("app.services.openai_service.complete_text")
    async def test_regression_real_legacy_leak_is_still_blocked(self, mock_complete_text):
        """4. Regression: confirm that if LLM really leaks a forbidden legacy typology, the validator blocks it."""
        leaky_llm_json = """{
            "generated_name": "Versión Con Fuga",
            "change_summary": "Intento de colar tipología legacy",
            "generated_prompt": "### REGLAS\\nTipos permitidos: consulta_general, informacion_sin_cita, no_interesado.\\n### FORMATO\\n{\\"tipo_llamada\\": \\"informacion_sin_cita\\"}",
            "improved_criteria_descriptions": []
        }"""
        mock_complete_text.return_value = leaky_llm_json

        async with self.session_maker() as db:
            result = await build_prompt_with_ai(
                db=db,
                prompt_id=70,
                instructions=None,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "error")
            self.assertIn("legacy", result["error_message"].lower())

    @patch("app.services.openai_service.complete_text")
    async def test_http_endpoint_post_build_with_ai(self, mock_complete_text):
        """5. Confirm POST /bm/prompt/build-with-ai endpoint returns 200 with expected schema."""
        clean_llm_json = """{
            "generated_name": "Versión Endpoint Test",
            "change_summary": "Generado vía API",
            "generated_prompt": "### REGLAS\\nTipos: consulta_general, soporte_tecnico.\\nProhibido inventar tipos.\\n### FORMATO\\n{\\"tipo_llamada\\": \\"consulta_general\\"}",
            "improved_criteria_descriptions": []
        }"""
        mock_complete_text.return_value = clean_llm_json

        token = create_access_token({"user_id": 1, "username": "admin_demo", "role": "company_admin", "company_id": 7})
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/bm/prompt/build-with-ai",
                    headers=headers,
                    json={"prompt_id": 70, "instructions": "Prueba API"}
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data.get("ok"))
                self.assertEqual(data.get("prompt_id"), 70)
                self.assertEqual(data.get("generated_name"), "Versión Endpoint Test")
        finally:
            app.dependency_overrides.pop(get_db, None)

if __name__ == "__main__":
    unittest.main()
