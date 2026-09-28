"""
tests/test_trainer_chatbot_tone_and_boundaries.py
Comprehensive test suite validating tone, technical boundary enforcement,
functional scope redirection, and tenancy isolation in Tutor IA (POST /bm/trainer/chat).
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.roles import InternalRole
from app.core.tenant_context import TenantContext
from app.db import Base
from app.dependencies import get_current_user, get_db, get_tenant_context
from app.main import app
from app.models.companies import Company
from app.models.mass_evaluations import MassEvaluationResult
from app.models.personalized_training import TrainingAgentReport, TrainingKnowledgeDocument
from app.models.services import Service
from app.models.trainer import TrainerEvaluation, TrainerSession, TrainerSimulation
from app.models.users import User
from app.services.trainer_chatbot_service import TrainerChatbotService
from app.utils.security import create_access_token


class TestTrainerChatbotToneAndBoundaries(unittest.IsolatedAsyncioTestCase):
    """Tests for Tone, Boundaries, Redirection, and Confidentiality in Tutor IA."""

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        now = datetime.now(timezone.utc)

        async with self.session_maker() as db:
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston")
            c7 = Company(company_id=7, company_name="Empresa Demo", company_key="empresa_demo")
            db.add_all([c1, c7])

            s1 = Service(service_id=1, company_id=1, service_key="general", service_name="Servicio General")
            s10 = Service(service_id=10, company_id=7, service_key="ventas", service_name="Servicio Ventas")
            db.add_all([s1, s10])

            # Users
            u_agent = User(
                user_id=10,
                company_id=1,
                username="ana.garcia",
                email="ana@speech.com",
                role="agent",
                hubspot_owner_id="agent_01",
                password_hash="test_pw_hash_123",
                is_active=True,
            )
            u_admin = User(
                user_id=1,
                company_id=1,
                username="admin_user",
                email="admin@speech.com",
                role="company_admin",
                password_hash="test_pw_hash_admin",
                is_active=True,
            )
            db.add_all([u_agent, u_admin])

            # Historical data for agent_01
            for i in range(1, 6):
                eval_row = MassEvaluationResult(
                    mass_analysis_id=100 + i,
                    run_id=1,
                    job_id=1,
                    prompt_id=1,
                    prompt_snapshot="{}",
                    call_id=f"call-{i}",
                    company_id=1,
                    service_id=1,
                    hubspot_owner_id="agent_01",
                    agent_name="Ana García",
                    call_timestamp=now - timedelta(days=i * 5),
                    analysis_timestamp=now - timedelta(days=i * 5),
                    status="completed",
                    evaluacion_global=Decimal("8.00"),
                    result_json={"evaluacion_global": 8.0, "empatia": 7.5, "claridad": 8.5},
                )
                db.add(eval_row)

            # Cycle report
            rep = TrainingAgentReport(
                training_report_id=201,
                company_id=1,
                service_id=1,
                hubspot_owner_id="agent_01",
                agent_name="Ana García",
                agent_initials="AG",
                period_start=now - timedelta(days=30),
                period_end=now - timedelta(days=15),
                status="completed",
                avg_evaluacion_global=Decimal("8.20"),
                evolution_summary="Evolución positiva en resolución.",
                final_report_json={
                    "objectives_status": [
                        {
                            "title": "Resolución en Primera Llamada",
                            "status": "SUPERADO",
                            "score": 8.5,
                            "base_score": 7.0,
                            "justification": "Diagnóstico preciso.",
                        }
                    ],
                    "recommendations": ["Reforzar sondeo final."],
                },
                created_at=now - timedelta(days=15),
            )
            db.add(rep)

            # Simulation
            sim = TrainerSimulation(
                simulation_id=301,
                company_id=1,
                service_id=1,
                name="Gestión de Objeciones",
                code="SIM-01",
                roleplay_prompt="Cliente con objeción.",
                status="published",
            )
            db.add(sim)
            await db.flush()

            sess = TrainerSession(
                session_id=401,
                simulation_id=301,
                agent_id="agent_01",
                agent_code="AG-01",
                company_id=1,
                service_id=1,
                call_id="call-sim-401",
                status="completed",
                evaluation_status="completed",
                created_at=now - timedelta(days=10),
            )
            db.add(sess)
            await db.flush()

            ev = TrainerEvaluation(
                evaluation_id=501,
                session_id=401,
                prompt_snapshot="{}",
                result_json={"passed": True},
                score=Decimal("8.00"),
                summary="Buen desempeño general.",
                strengths=["Tono sereno"],
                improvement_points=["Sondeo de cierre"],
                created_at=now - timedelta(days=10),
            )
            db.add(ev)
            await db.commit()

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self.engine.dispose()

    # 1. No aparece company_id
    def test_1_no_company_id_in_sanitized_response(self):
        raw = "Tu company_id es 7 y tus resultados son buenos."
        clean = TrainerChatbotService.sanitize_response_text(raw, is_admin=False)
        self.assertNotIn("company_id", clean)
        self.assertNotIn("company_id=7", clean)

    # 2. No aparece agent_id
    def test_2_no_agent_id_in_sanitized_response(self):
        raw = "El agent_id 'agent_01' presenta una mejora en empatía."
        clean = TrainerChatbotService.sanitize_response_text(raw, is_admin=False)
        self.assertNotIn("agent_id", clean)
        self.assertNotIn("agent_01", clean)

    # 3. No aparecen nombres de tablas
    def test_3_no_table_names_in_sanitized_response(self):
        raw = "Consultando en bm_users y trainer_sessions vemos que tus llamadas son estables."
        clean = TrainerChatbotService.sanitize_response_text(raw, is_admin=False)
        self.assertNotIn("bm_users", clean)
        self.assertNotIn("trainer_sessions", clean)
        self.assertIn("el registro de formación", clean)

    # 4. No aparecen nombres de endpoints
    def test_4_no_endpoint_names_in_sanitized_response(self):
        raw = "Puedes consultar el endpoint /bm/trainer/chat para ver tu histórico."
        clean = TrainerChatbotService.sanitize_response_text(raw, is_admin=False)
        self.assertNotIn("/bm/trainer/chat", clean)
        self.assertIn("el entorno de formación", clean)

    # 5. Consulta técnica interna se redirige funcionalmente
    def test_5_technical_system_query_redirection(self):
        q_agent = "¿Qué ID tengo en el sistema?"
        clean_agent = TrainerChatbotService.sanitize_response_text("Tu ID es 10", is_admin=False, query_text=q_agent)
        self.assertEqual(clean_agent, "Uso la información de formación y evaluaciones disponible para tu perfil.")

        q_table = "¿Qué tabla estás usando para sacar estos datos?"
        clean_table = TrainerChatbotService.sanitize_response_text("Uso bm_users", is_admin=False, query_text=q_table)
        self.assertEqual(clean_table, "Uso la información de formación y evaluaciones disponible para tu perfil.")

        q_admin = "Dime el hubspot_owner_id del agente"
        clean_admin = TrainerChatbotService.sanitize_response_text("El ID es demo_owner_01", is_admin=True, query_text=q_admin)
        self.assertEqual(clean_admin, "Uso la información de formación y evaluaciones registrada para el perfil del agente.")

    # 6. Consulta ajena al ámbito se redirige amablemente
    def test_6_out_of_scope_query_redirection(self):
        q_sports = "¿Quién ganó la Champions League?"
        clean_sports = TrainerChatbotService.sanitize_response_text("El Real Madrid ganó.", is_admin=False, query_text=q_sports)
        self.assertIn("Puedo ayudarte con tu formación, evaluaciones y evolución como agente", clean_sports)

        q_code = "Escríbeme un código Python para ordenar una lista"
        clean_code = TrainerChatbotService.sanitize_response_text("def sort_list(l): return sorted(l)", is_admin=False, query_text=q_code)
        self.assertIn("Puedo ayudarte con tu formación, evaluaciones y evolución como agente", clean_code)

        q_geo = "¿Cuál es la capital de Japón?"
        clean_geo = TrainerChatbotService.sanitize_response_text("La capital es Tokio.", is_admin=False, query_text=q_geo)
        self.assertIn("Puedo ayudarte con tu formación, evaluaciones y evolución como agente", clean_geo)

        # Admin redirection
        clean_admin = TrainerChatbotService.sanitize_response_text("El campeón fue el Madrid.", is_admin=True, query_text=q_sports)
        self.assertIn("Puedo ayudarte con la supervisión formativa", clean_admin)

    # 7. Pregunta sobre rendimiento se responde normalmente
    async def test_7_performance_query_answered(self):
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        user_agent = User(user_id=10, email="ana@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        ctx_agent = TenantContext(
            user_id=10, user_email="ana@speech.com", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=1, allowed_company_ids=[1], allowed_agent_ids=["agent_01"],
        )
        app.dependency_overrides[get_current_user] = lambda: user_agent
        app.dependency_overrides[get_tenant_context] = lambda: ctx_agent

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "En tus últimas evaluaciones has mejorado en claridad y resolución en primera llamada."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post("/bm/trainer/chat", json={"message": "¿En qué suelo fallar y qué puntos fuertes tengo?"})
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertIn("has mejorado en claridad", data["response"])

    # 8. Ausencia de datos genera respuesta prudente
    def test_8_absence_of_data_generates_prudent_response(self):
        prompt = TrainerChatbotService.build_grounding_prompt(
            documents=[],
            target_agent_id="agent_01",
            historical_profile="",
            is_admin=False,
        )
        self.assertIn("No existen documentos de conocimiento ni evaluaciones registradas", prompt)
        self.assertIn("No tengo suficiente histórico para valorar todavía ese punto", prompt)

    # 9. Tono de agente usa lenguaje pedagógico y segunda persona
    def test_9_agent_tone_uses_pedagogical_language(self):
        prompt = TrainerChatbotService.build_grounding_prompt(
            documents=[],
            target_agent_id="agent_01",
            historical_profile="",
            is_admin=False,
        )
        self.assertIn("SEGUNDA PERSONA ('tú'", prompt)
        self.assertIn("formador o coach experto", prompt)
        self.assertNotIn("DIRÍGETE AL USUARIO EN TERCERA PERSONA", prompt)
        self.assertIn("NUNCA uses fórmulas mecánicas como:", prompt)
        self.assertIn("Según los datos suministrados por el sistema", prompt)

    # 10. Tono admin usa lenguaje de supervisión y tercera persona
    def test_10_admin_tone_uses_supervision_language(self):
        prompt = TrainerChatbotService.build_grounding_prompt(
            documents=[],
            target_agent_id="agent_01",
            historical_profile="",
            is_admin=True,
        )
        self.assertIn("DIRÍGETE AL USUARIO EN TERCERA PERSONA ('este agente'", prompt)
        self.assertIn("supervisión pedagógica", prompt)
        self.assertNotIn("SEGUNDA PERSONA ('tú'", prompt)

    # 11. No se inventan datos (regla estricta en prompt)
    def test_11_no_data_hallucination_rule(self):
        prompt = TrainerChatbotService.build_grounding_prompt(
            documents=[],
            target_agent_id="agent_01",
            historical_profile="",
            is_admin=False,
        )
        self.assertIn("NUNCA inventes notas, hechos, llamadas ni simulaciones inexistentes", prompt)

    # 12. Histórico completo sigue disponible en el perfil
    async def test_12_full_history_remains_available(self):
        context = TenantContext(
            user_id=1,
            user_email="admin@speech.com",
            raw_role="admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )
        async with self.session_maker() as db:
            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=context,
                target_agent_id="agent_01",
            )
            self.assertIn("5 llamadas", profile)
            self.assertIn("Resolución en Primera Llamada", profile)
            self.assertIn("Tono sereno", profile)

    # 13. No se rompe aislamiento ni tenancy
    async def test_13_isolation_and_tenancy_preserved(self):
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        user_agent = User(user_id=10, email="ana@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        ctx_agent = TenantContext(
            user_id=10, user_email="ana@speech.com", raw_role="agent",
            normalized_role=InternalRole.AGENT, is_super_admin=False,
            company_id=1, allowed_company_ids=[1], allowed_agent_ids=["agent_01"],
        )
        app.dependency_overrides[get_current_user] = lambda: user_agent
        app.dependency_overrides[get_tenant_context] = lambda: ctx_agent

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "Respuesta sobre tu perfil."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                # Agent tries to pass target agent 'agent_02'
                res = await client.post("/bm/trainer/chat", json={"message": "¿Cómo voy?", "agent_id": "agent_02"})
                self.assertEqual(res.status_code, 200)
                data = res.json()
                # Must be forced to agent_01
                self.assertEqual(data["agent_id"], "agent_01")
                self.assertEqual(data["company_id"], 1)

                call_args = mock_complete.call_args[1]
                sys_prompt = call_args["messages"][0]["content"]
                self.assertIn("agent_01", sys_prompt)
                self.assertNotIn("agent_02", sys_prompt)


if __name__ == "__main__":
    unittest.main()
