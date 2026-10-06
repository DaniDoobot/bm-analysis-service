import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_chatbot_search.db"

from sqlalchemy import BigInteger, delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.services.trainer_chatbot_service import TrainerChatbotService


class TestTrainerChatbotConversationSearch(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session_maker() as db:
            await db.execute(delete(MassEvaluationCriterionResult))
            await db.execute(delete(MassEvaluationResult))
            await db.execute(delete(MassEvaluationRun))
            await db.execute(delete(MassEvaluationJob))
            await db.execute(delete(User))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            company1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_demo=False)
            company2 = Company(company_id=2, company_name="Other Clinic", company_key="other-clinic", is_demo=False)
            service1 = Service(service_id=1, company_id=1, service_name="Atención", service_key="atencion")
            db.add_all([company1, company2, service1])
            await db.flush()

            # Agents
            user_agent = User(
                user_id=10,
                username="agent1",
                email="agent1@example.com",
                role="AGENT",
                company_id=1,
                hubspot_owner_id="agent_100",
                name="Agente Uno",
                password_hash="fakehash",
                is_active=True,
            )
            user_admin = User(
                user_id=20,
                username="admin1",
                email="admin@example.com",
                role="COMPANY_ADMIN",
                company_id=1,
                hubspot_owner_id="admin_200",
                name="Admin Supervisor",
                password_hash="fakehash",
                is_active=True,
            )
            job = MassEvaluationJob(job_id=1, job_name="Job 1", prompt_id=1, is_active=True, schedule_enabled=False, created_by="tester")
            run = MassEvaluationRun(run_id=1, job_id=1, trigger_type="manual", status="completed")
            db.add_all([user_agent, user_admin, job, run])
            await db.flush()

            # Insert sample calls for agent_100 (Company 1)
            now = datetime.now(timezone.utc)
            call_1 = MassEvaluationResult(
                mass_analysis_id=5001,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call-001-good",
                company_id=1,
                service_id=1,
                service_key="atencion",
                hubspot_owner_id="agent_100",
                agent_name="Agente Uno",
                call_timestamp=now - timedelta(days=2),
                analysis_timestamp=now - timedelta(days=2),
                call_duration_seconds=185,
                direction="inbound",
                typology_name="Consulta General",
                status="completed",
                evaluacion_global=Decimal("8.5"),
                alarma=False,
                result_json={
                    "resumen": "Llamada excelente con gran empatía y manejo fluido.",
                    "transcripcion": "Agente: Buenos días, ¿en qué puedo ayudarle? Paciente: Tenía una duda...",
                },
                items_json=[
                    {"name": "Empatía", "criterion_key": "empatia", "value": 9.0, "feedback": "Muy cordial."},
                    {"name": "Gestión precio", "criterion_key": "claridad_explicacion_economica", "value": 8.0, "feedback": "Claro."},
                ],
            )

            call_2 = MassEvaluationResult(
                mass_analysis_id=5002,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call-002-bad-price",
                company_id=1,
                service_id=1,
                service_key="atencion",
                hubspot_owner_id="agent_100",
                agent_name="Agente Uno",
                call_timestamp=now - timedelta(days=5),
                analysis_timestamp=now - timedelta(days=5),
                call_duration_seconds=240,
                direction="outbound",
                typology_name="Seguimiento",
                status="completed",
                evaluacion_global=Decimal("5.2"),
                alarma=True,
                result_json={
                    "resumen": "Buena empatía inicial pero no supo responder a la objeción sobre el coste de consulta.",
                    "transcripcion": "Paciente: Me parece muy caro. Agente: Pues es lo que hay...",
                },
                items_json=[
                    {"name": "Empatía", "criterion_key": "empatia", "value": 8.0, "feedback": "Buena escucha inicial."},
                    {"name": "Gestión precio", "criterion_key": "claridad_explicacion_economica", "value": 3.0, "feedback": "No desglosó opciones."},
                ],
            )

            # Call belonging to another company (Company 2)
            call_other_company = MassEvaluationResult(
                mass_analysis_id=5003,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="call-999-other-company",
                company_id=2,
                service_id=1,
                service_key="atencion",
                hubspot_owner_id="agent_other",
                agent_name="Otro Agente",
                call_timestamp=now - timedelta(days=1),
                analysis_timestamp=now - timedelta(days=1),
                call_duration_seconds=120,
                direction="inbound",
                status="completed",
                evaluacion_global=Decimal("9.0"),
                alarma=False,
                result_json={"resumen": "Llamada de otra empresa."},
                items_json=[],
            )

            crit_1_emp = MassEvaluationCriterionResult(
                mass_analysis_id=5001,
                run_id=1,
                job_id=1,
                call_id="call-001-good",
                criterion_key="empatia",
                criterion_name="Empatía",
                numeric_value=Decimal("9.0"),
                is_applicable=True,
                not_applicable=False,
            )
            crit_1_price = MassEvaluationCriterionResult(
                mass_analysis_id=5001,
                run_id=1,
                job_id=1,
                call_id="call-001-good",
                criterion_key="claridad_explicacion_economica",
                criterion_name="Explicación precio consulta",
                numeric_value=Decimal("8.0"),
                is_applicable=True,
                not_applicable=False,
            )
            crit_2_emp = MassEvaluationCriterionResult(
                mass_analysis_id=5002,
                run_id=1,
                job_id=1,
                call_id="call-002-bad-price",
                criterion_key="empatia",
                criterion_name="Empatía",
                numeric_value=Decimal("8.0"),
                is_applicable=True,
                not_applicable=False,
            )
            crit_2_price = MassEvaluationCriterionResult(
                mass_analysis_id=5002,
                run_id=1,
                job_id=1,
                call_id="call-002-bad-price",
                criterion_key="claridad_explicacion_economica",
                criterion_name="Explicación precio consulta",
                numeric_value=Decimal("3.0"),
                is_applicable=True,
                not_applicable=False,
            )

            db.add_all([call_1, call_2, call_other_company, crit_1_emp, crit_1_price, crit_2_emp, crit_2_price])
            await db.commit()

        self.agent_user = user_agent
        self.admin_user = user_admin
        self.agent_context = TenantContext(
            user_id=10,
            user_email="agent1@example.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_100"],
        )
        self.admin_context = TenantContext(
            user_id=20,
            user_email="admin@example.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_100", "admin_200"],
        )

    # -------------------------------------------------------------
    # 1. Detection of Call IDs
    # -------------------------------------------------------------
    def test_detect_call_ids_single_and_multi(self):
        # Single calls
        self.assertEqual(TrainerChatbotService.detect_call_ids("Analízame la llamada 123456"), ["123456"])
        self.assertEqual(TrainerChatbotService.detect_call_ids("Revisa la conversación call-abc-789 por favor"), ["call-abc-789"])
        self.assertEqual(TrainerChatbotService.detect_call_ids("id de llamada 987654"), ["987654"])
        self.assertEqual(TrainerChatbotService.detect_call_ids("comprueba la llamada #5544"), ["5544"])

        # Comparisons / multi calls
        self.assertEqual(
            TrainerChatbotService.detect_call_ids("Compara la llamada 123456 con la llamada 789012"),
            ["123456", "789012"],
        )
        self.assertEqual(
            TrainerChatbotService.detect_call_ids("Compara la llamada call-001 con la 789012"),
            ["call-001", "789012"],
        )
        self.assertEqual(
            TrainerChatbotService.detect_call_ids("conversación call_a vs call_b"),
            ["call_a", "call_b"],
        )

    def test_detect_call_ids_rejects_non_call_numbers(self):
        # Phones, scores, dates, counts should NOT match
        self.assertEqual(TrainerChatbotService.detect_call_ids("Mi teléfono es 612345678"), [])
        self.assertEqual(TrainerChatbotService.detect_call_ids("Tengo un 8.5 de nota y 3 llamadas"), [])
        self.assertEqual(TrainerChatbotService.detect_call_ids("Ayer completé 5 llamadas con 100% de éxito"), [])
        self.assertEqual(TrainerChatbotService.detect_call_ids("El día 15 de marzo no trabajé"), [])

    def test_detect_call_ids_regression_bug_cases(self):
        # Mandatory bug regression cases: plural / criteria queries must NOT match any call_id
        self.assertEqual(
            TrainerChatbotService.detect_call_ids(
                "Dame dos conversaciones donde la empatía haya sido buena"
            ),
            [],
        )
        self.assertEqual(
            TrainerChatbotService.detect_call_ids(
                "Dame dos conversaciones del último mes donde empatía y claridad hayan sido buenas pero la gestión del precio haya sido mala"
            ),
            [],
        )
        self.assertEqual(
            TrainerChatbotService.detect_call_ids(
                "Busca conversaciones donde tuve mala claridad"
            ),
            [],
        )
        self.assertEqual(
            TrainerChatbotService.detect_call_ids(
                "Enséñame llamadas donde gestioné mal el precio"
            ),
            [],
        )

        # Preserved single and comparison cases
        self.assertEqual(
            TrainerChatbotService.detect_call_ids("Analízame la conversación 123456"),
            ["123456"],
        )
        self.assertEqual(
            TrainerChatbotService.detect_call_ids("Analízame la llamada 123456"),
            ["123456"],
        )
        self.assertEqual(
            TrainerChatbotService.detect_call_ids(
                "Compara la llamada 123456 con la 789012"
            ),
            ["123456", "789012"],
        )

    # -------------------------------------------------------------
    # 2. Intent and criteria parsing
    # -------------------------------------------------------------
    def test_detect_criterion_search_intent(self):
        self.assertTrue(
            TrainerChatbotService.detect_criterion_search_intent(
                "Dame dos conversaciones donde la empatía haya sido buena pero la gestión del precio mala"
            )
        )
        self.assertTrue(
            TrainerChatbotService.detect_criterion_search_intent("Busca llamadas en las que el cierre fue bueno")
        )
        self.assertTrue(
            TrainerChatbotService.detect_criterion_search_intent("Muéstrame ejemplos de llamadas con objeciones")
        )
        self.assertTrue(
            TrainerChatbotService.detect_criterion_search_intent("Encuentra una conversación donde fallé en el precio")
        )

        # Non-search queries
        self.assertFalse(TrainerChatbotService.detect_criterion_search_intent("¿Cómo he evolucionado este mes?"))
        self.assertFalse(TrainerChatbotService.detect_criterion_search_intent("¿Cuáles son mis puntos fuertes?"))
        self.assertFalse(TrainerChatbotService.detect_criterion_search_intent("Hola, buenos días"))

    def test_extract_search_quantity(self):
        self.assertEqual(TrainerChatbotService.extract_search_quantity("Dame 3 llamadas"), 3)
        self.assertEqual(TrainerChatbotService.extract_search_quantity("Encuentra una conversación mala"), 1)
        self.assertEqual(TrainerChatbotService.extract_search_quantity("Busca cinco llamadas"), 5)
        self.assertEqual(TrainerChatbotService.extract_search_quantity("Muéstrame 2 ejemplos"), 2)
        # Default fallback is 2
        self.assertEqual(TrainerChatbotService.extract_search_quantity("Dame conversaciones donde fallé"), 2)

    def test_extract_search_period(self):
        ref = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
        # User specified period (e.g. últimos 10 días)
        s, e, lbl = TrainerChatbotService.extract_search_period("Dame 2 llamadas de los últimos 10 días", ref)
        self.assertIn("10 días", lbl.lower())

        # Default fallback when period is not specified: last 30 days
        s_def, e_def, lbl_def = TrainerChatbotService.extract_search_period("Dame 2 llamadas con buena empatía", ref)
        self.assertEqual(lbl_def, "los últimos 30 días")
        self.assertEqual((e_def - s_def).days, 30)

    async def test_parse_criteria_query_polarities_and_catalog(self):
        mock_catalog = [
            {"key": "empatia", "label": "Empatía", "type": "score"},
            {"key": "claridad_explicacion_economica", "label": "Explicación precio consulta", "type": "score"},
            {"key": "gestion_objeciones", "label": "Gestión de objeciones", "type": "score"},
            {"key": "saludo_adecuado", "label": "Saludo inicial correcto", "type": "boolean"},
        ]
        with patch("app.utils.item_score_filters.get_evaluation_item_filter_options", new_callable=AsyncMock) as mock_get_opts:
            mock_get_opts.return_value = mock_catalog
            async with self.session_maker() as db:
                filters = await TrainerChatbotService.parse_criteria_query(
                    "Dame dos conversaciones donde la empatía haya sido buena pero la gestión del precio mala",
                    db=db,
                    context=self.agent_context,
                    target_agent_id="agent_100",
                )
                self.assertEqual(len(filters), 2)
                f_emp = next(f for f in filters if f["key"] == "empatia")
                f_price = next(f for f in filters if f["key"] == "claridad_explicacion_economica")

                self.assertEqual(f_emp["polarity"], "positive")
                self.assertEqual(f_emp["min"], 7.0)
                self.assertEqual(f_emp["max"], 10.0)

                self.assertEqual(f_price["polarity"], "negative")
                self.assertEqual(f_price["min"], 0.0)
                self.assertEqual(f_price["max"], 4.0)

    # -------------------------------------------------------------
    # 3. Flow B: Single Call Detail
    # -------------------------------------------------------------
    async def test_single_call_detail_success(self):
        async with self.session_maker() as db:
            with patch("app.services.trainer_chatbot_service.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = "En la llamada call-001-good demostraste una excelente empatía de 9/10."

                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Analízame la llamada call-001-good",
                )

                # Verify LLM was called with call detail in system prompt
                call_args = mock_llm.call_args[1]["messages"]
                sys_prompt = next(m["content"] for m in call_args if m["role"] == "system")
                self.assertIn("call-001-good", sys_prompt)
                self.assertIn("INSTRUCCIÓN PARA ANÁLISIS DE LLAMADA CONCRETA", sys_prompt)
                self.assertIn("8.5/10", sys_prompt)

                # Verify sanitized response preserves call_id and returns
                self.assertIn("call-001-good", res["response"])
                self.assertEqual(res["agent_id"], "agent_100")

    async def test_single_call_detail_not_found(self):
        async with self.session_maker() as db:
            res = await TrainerChatbotService.process_chat(
                db=db,
                current_user=self.agent_user,
                context=self.agent_context,
                message="Analízame la llamada llamada-inexistente-123",
            )
            # Should return clear pedagogical message without calling LLM or failing
            self.assertIn("No he localizado la llamada llamada-inexistente-123", res["response"])

    async def test_single_call_cross_tenant_isolation(self):
        async with self.session_maker() as db:
            # Query call belonging to Company 2
            res = await TrainerChatbotService.process_chat(
                db=db,
                current_user=self.agent_user,
                context=self.agent_context,
                message="Analízame la llamada call-999-other-company",
            )
            # Must NOT find it, keeping strict isolation
            self.assertIn("No he localizado la llamada call-999-other-company", res["response"])
            self.assertNotIn("otra empresa", res["response"])

    # -------------------------------------------------------------
    # 4. Flow A: Compare Two Calls
    # -------------------------------------------------------------
    async def test_compare_two_calls_success(self):
        async with self.session_maker() as db:
            with patch("app.services.trainer_chatbot_service.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = (
                    "Comparando ambas interacciones: en la llamada call-001-good obtuviste 8.5/10, "
                    "mientras que en la llamada call-002-bad-price bajó a 5.2/10 debido al manejo del precio."
                )

                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Compara la llamada call-001-good con la llamada call-002-bad-price",
                )

                call_args = mock_llm.call_args[1]["messages"]
                sys_prompt = next(m["content"] for m in call_args if m["role"] == "system")
                self.assertIn("COMPARATIVA DE LLAMADAS", sys_prompt)
                self.assertIn("call-001-good", sys_prompt)
                self.assertIn("call-002-bad-price", sys_prompt)

                self.assertIn("call-001-good", res["response"])
                self.assertIn("call-002-bad-price", res["response"])

    async def test_compare_two_calls_one_missing(self):
        async with self.session_maker() as db:
            res = await TrainerChatbotService.process_chat(
                db=db,
                current_user=self.agent_user,
                context=self.agent_context,
                message="Compara la llamada call-001-good con la llamada inexistente-999",
            )
            self.assertIn("He localizado la llamada call-001-good, pero no encuentro la llamada inexistente-999", res["response"])

    # -------------------------------------------------------------
    # 5. Flow C: Criteria Examples Search
    # -------------------------------------------------------------
    async def test_criteria_search_calls_found(self):
        mock_catalog = [
            {"key": "empatia", "label": "Empatía", "type": "score"},
            {"key": "claridad_explicacion_economica", "label": "Explicación precio consulta", "type": "score"},
        ]
        async with self.session_maker() as db:
            with patch("app.utils.item_score_filters.get_evaluation_item_filter_options", new_callable=AsyncMock) as mock_get_opts, \
                 patch("app.services.trainer_chatbot_service.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
                mock_get_opts.return_value = mock_catalog
                mock_llm.return_value = (
                    "He localizado la llamada call-001-good donde mostraste gran empatía (9/10). "
                    "Te servirá de ejemplo para mantener la calma y escucha activa."
                )

                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Dame una conversación donde la empatía haya sido buena",
                )

                call_args = mock_llm.call_args[1]["messages"]
                sys_prompt = next(m["content"] for m in call_args if m["role"] == "system")
                self.assertIn("INSTRUCCIÓN PARA BÚSQUEDA DE EJEMPLOS POR CRITERIOS", sys_prompt)
                self.assertIn("call-001-good", sys_prompt)
                self.assertIn("call-001-good", res["response"])

    async def test_regression_routing_criteria_query_not_routed_to_single_call(self):
        """
        End-to-end routing regression test:
        'Dame dos conversaciones donde la empatía haya sido buena'
        must NEVER route to single call detail or call fetch_scoped_call with 'es'.
        Must route to criterion search intent, query criteria catalog, and use item_filters.
        """
        mock_catalog = [
            {"key": "empatia", "label": "Empatía", "type": "score"},
            {"key": "claridad_explicacion_economica", "label": "Explicación precio consulta", "type": "score"},
        ]
        query = "Dame dos conversaciones donde la empatía haya sido buena"

        # 1. Verify detect_call_ids returns []
        self.assertEqual(TrainerChatbotService.detect_call_ids(query), [])

        # 2. Verify detect_criterion_search_intent returns True
        self.assertTrue(TrainerChatbotService.detect_criterion_search_intent(query))

        async with self.session_maker() as db:
            with patch("app.utils.item_score_filters.get_evaluation_item_filter_options", new_callable=AsyncMock) as mock_get_opts, \
                 patch.object(TrainerChatbotService, "fetch_scoped_call", new_callable=AsyncMock) as mock_fetch_call, \
                 patch("app.services.trainer_chatbot_service.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:

                mock_get_opts.return_value = mock_catalog
                mock_llm.return_value = (
                    "Encontré la llamada call-001-good como ejemplo de alta empatía (9/10)."
                )

                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message=query,
                )

                # MUST NOT enter single call flow or fetch_scoped_call
                mock_fetch_call.assert_not_called()

                # MUST enter criteria flow: catalog was consulted
                mock_get_opts.assert_called_once()

                # MUST NOT contain error message about nonexistent call 'es'
                self.assertNotIn("No he localizado la llamada es", res["response"])
                self.assertNotIn("llamada es", res["response"])

                # LLM received the criteria prompt instruction and call example
                call_args = mock_llm.call_args[1]["messages"]
                sys_prompt = next(m["content"] for m in call_args if m["role"] == "system")
                self.assertIn("INSTRUCCIÓN PARA BÚSQUEDA DE EJEMPLOS POR CRITERIOS", sys_prompt)
                self.assertIn("call-001-good", sys_prompt)

    # -------------------------------------------------------------
    # 6. Sanitization of Call IDs vs Internal DB IDs
    # -------------------------------------------------------------
    def test_call_id_sanitization_preserves_public_id_and_strips_internal_ids(self):
        text = (
            "En la Llamada 123456 (ID 123456) se observa un tono muy constructivo con 8.5 de nota. "
            "Sin embargo, en el registro técnico con ID 573 y agent_id='agent_100' de la tabla bm_users hubo fallos."
        )
        allowed = {"123456"}

        sanitized = TrainerChatbotService.sanitize_response_text(
            text=text,
            is_admin=False,
            query_text="Analízame la llamada 123456",
            allowed_call_ids=allowed,
        )

        # Public call id must be preserved
        self.assertIn("123456", sanitized)
        # Internal DB IDs and table names must be removed/masked
        self.assertNotIn("ID 573", sanitized)
        self.assertNotIn("573", sanitized)
        self.assertNotIn("agent_id='agent_100'", sanitized)
        self.assertNotIn("bm_users", sanitized)
        self.assertIn("el registro de formación", sanitized)


if __name__ == "__main__":
    unittest.main()
