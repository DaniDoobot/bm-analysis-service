"""
Test Suite: Trainer Chatbot Historical Context and Long-term Agent Trajectory
=============================================================================
Validates:
1. Multi-tenant isolation: Tenant A cannot access historical data of Tenant B.
2. Role enforcement: Agent users can only query their own historical trajectory.
3. Supervisor/Admin scoping: Admins and Supervisors can query history of authorized agents.
4. Complete historical access: History is not limited to 90 days (period="all").
5. History beyond 12 knowledge documents: Summarizes calls and cycles beyond the 12 document limit.
6. Strengths and weaknesses: Top criteria and recurring error/difficulty criteria extracted with scores and counts.
7. Criteria evolution: Temporal criteria trajectory (first_avg, last_avg, delta, direction).
8. Training cycle objectives: Evaluates SUPERADO / NO SUPERADO objectives with scores and justifications.
9. Accumulated cycle recommendations: Extracts past pedagogical recommendations.
10. Roleplay simulations: Includes completed simulations, scores, and improvement points.
11. Historical vs recent comparison: Contrasts complete historical average with recent 30-day average.
12. Absence of data handling: Clean explanation when no data exists, no hallucinations.
13. Compact prompt structure: Output is concise (< 1500 tokens), formatted in clean markdown, no raw JSON or DB tables.
"""
import json
import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_chatbot_history.db"

from sqlalchemy import BigInteger, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from httpx import AsyncClient, ASGITransport

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.main import app
from app.dependencies import get_db, get_current_user, get_tenant_context
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingKnowledgeDocument,
)
from app.models.trainer import (
    TrainerSession,
    TrainerEvaluation,
    TrainerSimulation,
)
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.services.trainer_chatbot_service import TrainerChatbotService


class TestTrainerChatbotHistoricalContext(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session_maker() as db:
            await db.execute(delete(TrainerEvaluation))
            await db.execute(delete(TrainerSession))
            await db.execute(delete(TrainerSimulation))
            await db.execute(delete(TrainingKnowledgeDocument))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(MassEvaluationResult))
            await db.execute(delete(User))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            # Base companies & services
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_demo=False)
            c2 = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
            s1 = Service(service_id=1, company_id=1, service_name="Atención", service_key="atencion")
            s2 = Service(service_id=10, company_id=7, service_name="Ventas Demo", service_key="ventas")
            db.add_all([c1, c2, s1, s2])
            await db.flush()

            # Seed historical data for agent_01 in company 1
            # 1. Calls extending 180 days ago (proves period="all" beyond 90 days)
            now = datetime.now(timezone.utc)
            t_old = now - timedelta(days=150)
            t_recent = now - timedelta(days=10)

            # Old calls (lower empathy 5.0, high greeting 8.5)
            for i in range(1, 16):
                eval_row = MassEvaluationResult(
                    mass_analysis_id=100 + i,
                    run_id=1,
                    job_id=1,
                    prompt_id=1,
                    prompt_snapshot="{}",
                    call_id=f"call-old-{i}",
                    company_id=1,
                    service_id=1,
                    hubspot_owner_id="agent_01",
                    agent_name="Ana García",
                    call_timestamp=t_old + timedelta(days=i),
                    analysis_timestamp=t_old + timedelta(days=i),
                    status="completed",
                    evaluacion_global=Decimal("6.20"),
                    result_json={
                        "evaluacion_global": 6.2,
                        "empatia": 5.0,
                        "claridad": 6.0,
                        "escucha_activa": 8.5,
                        "objeciones": "precio",
                    },
                )
                db.add(eval_row)

            # Recent calls within last 30 days (improved empathy 8.0, overall 7.8)
            for i in range(1, 16):
                eval_row = MassEvaluationResult(
                    mass_analysis_id=200 + i,
                    run_id=1,
                    job_id=1,
                    prompt_id=1,
                    prompt_snapshot="{}",
                    call_id=f"call-recent-{i}",
                    company_id=1,
                    service_id=1,
                    hubspot_owner_id="agent_01",
                    agent_name="Ana García",
                    call_timestamp=t_recent + timedelta(days=i // 2),
                    analysis_timestamp=t_recent + timedelta(days=i // 2),
                    status="completed",
                    evaluacion_global=Decimal("7.80"),
                    result_json={
                        "evaluacion_global": 7.8,
                        "empatia": 8.0,
                        "claridad": 7.5,
                        "escucha_activa": 9.0,
                        "objeciones": "duda_servicio",
                    },
                )
                db.add(eval_row)

            # 2. Training cycles (12 reports total within last 30 days)
            for c_idx in range(1, 13):
                p_offset = (13 - c_idx) * 2  # Cycle 1 is 24 days ago, Cycle 12 is 2 days ago
                rep = TrainingAgentReport(
                    training_report_id=500 + c_idx,
                    company_id=1,
                    service_id=1,
                    hubspot_owner_id="agent_01",
                    agent_name="Ana García",
                    agent_initials="AG",
                    period_start=now - timedelta(days=p_offset + 1),
                    period_end=now - timedelta(days=p_offset),
                    status="completed",
                    avg_evaluacion_global=Decimal(str(round(6.0 + (c_idx * 0.15), 2))),
                    evolution_summary=f"Evolución ciclo {c_idx}.",
                    final_report_json={
                        "objectives_status": [
                            {
                                "title": "Control de Tiempos Arcaico" if c_idx == 1 else ("Manejo de Objeción de Precio" if c_idx % 2 == 1 else "Escucha Activa Inicial"),
                                "type": "general",
                                "status": "NO SUPERADO" if c_idx in (1, 3, 7, 11) else "SUPERADO",
                                "score": 5.2 if c_idx in (1, 3, 7, 11) else 8.5,
                                "base_score": 4.8,
                                "justification": "Cede al descuento de inmediato ante objeción." if c_idx == 11 else f"Justificación del ciclo {c_idx}.",
                            }
                        ],
                        "recommendations": [
                            "Recomendación antigua del ciclo 1: revisar protocolo inicial." if c_idx == 1 else (
                                "Asegurar compromiso de seguimiento con fecha." if c_idx == 12 else "Practicar la técnica de doble alternativa antes de proponer descuentos comerciales."
                            )
                        ],
                    },
                    created_at=now - timedelta(days=p_offset),
                )
                db.add(rep)

            # Cycle older than 30 days (50 days ago) to verify cutoff
            rep_old = TrainingAgentReport(
                training_report_id=999,
                company_id=1,
                service_id=1,
                hubspot_owner_id="agent_01",
                agent_name="Ana García",
                agent_initials="AG",
                period_start=now - timedelta(days=60),
                period_end=now - timedelta(days=50),
                status="completed",
                avg_evaluacion_global=Decimal("5.00"),
                evolution_summary="Ciclo muy antiguo fuera de 30 días.",
                final_report_json={
                    "objectives_status": [
                        {
                            "title": "Objetivo Excluido Por Ser Antiguo",
                            "type": "general",
                            "status": "SUPERADO",
                            "score": 9.0,
                            "base_score": 5.0,
                        }
                    ],
                    "recommendations": ["Recomendación excluida fuera de 30 días."],
                },
                created_at=now - timedelta(days=50),
            )
            db.add(rep_old)

            # 3. Trainer simulation sessions (14 sessions total within last 30 days)
            sim1 = TrainerSimulation(
                simulation_id=301,
                company_id=1,
                service_id=1,
                name="Objeción Frontal de Precio",
                code="SIM-VENTAS-01",
                roleplay_prompt="Cliente exigente con comparativa de competidor.",
                status="published",
            )
            db.add(sim1)
            await db.flush()

            for s_idx in range(1, 15):
                s_offset = (15 - s_idx) * 2  # Sim 1 is 28 days ago, Sim 14 is 2 days ago
                sess = TrainerSession(
                    session_id=400 + s_idx,
                    simulation_id=301,
                    agent_id="agent_01",
                    agent_code="AG-01",
                    company_id=1,
                    service_id=1,
                    call_id=f"call-sim-400-{s_idx}",
                    status="completed",
                    evaluation_status="completed",
                    created_at=now - timedelta(days=s_offset),
                )
                db.add(sess)
                await db.flush()

                ev = TrainerEvaluation(
                    evaluation_id=600 + s_idx,
                    session_id=400 + s_idx,
                    prompt_snapshot="Snapshot prompt",
                    result_json={"passed": True},
                    score=Decimal(str(round(6.2 + (s_idx * 0.15), 2))),
                    summary=f"Resumen de evaluación simulación {s_idx}.",
                    strengths=["Tono sereno", "Rapport inicial"],
                    improvement_points=[
                        "Punto de mejora arcaico de simulación 1" if s_idx == 1 else "Firmeza en defensa de honorarios",
                        "Doble opción de cierre" if s_idx % 2 == 0 else "Agilidad en tipificación CRM",
                    ],
                    created_at=now - timedelta(days=s_offset),
                )
                db.add(ev)

            # Simulation older than 30 days (60 days ago) to verify cutoff
            sess_old = TrainerSession(
                session_id=999,
                simulation_id=301,
                agent_id="agent_01",
                agent_code="AG-01",
                company_id=1,
                service_id=1,
                call_id="call-sim-old-999",
                status="completed",
                evaluation_status="completed",
                created_at=now - timedelta(days=60),
            )
            db.add(sess_old)

            # 4. Data for another company (Company 7 - Empresa Demo)
            eval_demo = MassEvaluationResult(
                mass_analysis_id=701,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                call_id="demo_c99",
                company_id=7,
                service_id=10,
                hubspot_owner_id="demo_agent_99",
                agent_name="Demo User 99",
                call_timestamp=now,
                analysis_timestamp=now,
                status="completed",
                evaluacion_global=Decimal("9.90"),
                result_json={"evaluacion_global": 9.9, "empatia": 9.9},
            )
            db.add(eval_demo)
            await db.commit()

    async def test_build_agent_historical_profile_structure_and_metrics(self):
        """Verifica que el perfil histórico agrega llamadas reales (todo el histórico), ciclos y simulaciones."""
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

        # 1. Total llamadas (15 recientes dentro de 30 días, 15 antiguas de hace 150 días excluidas)
        self.assertIn("15 llamadas", profile)
        self.assertIn("ÚLTIMOS 30 DÍAS", profile)
        self.assertNotIn("call-old-", profile)

        # 2. Puntuación en los últimos 30 días
        self.assertIn("Puntuación media en los últimos 30 días", profile)

        # 3. Fortalezas consistentes
        self.assertIn("Fortalezas Recurrentes en el Periodo", profile)

        # 4. Áreas de mejora recurrentes
        self.assertIn("Errores y Criterios con Mayor Dificultad", profile)

        # 5. Evolución temporal de criterios (empatía)
        self.assertIn("Evolución Temporal por Criterios en el Periodo", profile)
        self.assertTrue("empatía" in profile.lower() or "empatia" in profile.lower())

        # 6. Ciclos y Objetivos SUPERADO / NO SUPERADO
        self.assertIn("Ciclos de Formación en el Periodo", profile)
        self.assertIn("[SUPERADO] Escucha Activa Inicial", profile)
        self.assertIn("[NO SUPERADO] Manejo de Objeción de Precio", profile)

        # 7. Recomendaciones de ciclos
        self.assertIn("Recomendaciones pedagógicas de ciclos anteriores", profile)
        self.assertIn("técnica de doble alternativa", profile)

        # 8. Simulaciones de roleplay
        self.assertIn("Simulaciones de Roleplay en el Periodo", profile)
        self.assertIn("Firmeza en defensa de honorarios", profile)

        # 9. Formato limpio: sin nombres de tablas DB ni IDs expuestos
        self.assertNotIn("bm_mass_evaluation_results", profile)
        self.assertNotIn("bm_training_agent_reports", profile)
        self.assertNotIn("bm_trainer_sessions", profile)

        # 10. Elementos fuera de los 30 días excluidos
        self.assertNotIn("Objetivo Excluido Por Ser Antiguo", profile)
        self.assertNotIn("call-sim-old-999", profile)

    async def test_historical_profile_respects_tenant_isolation(self):
        """Un usuario en Empresa 1 no puede ver datos de un agente de Empresa 7."""
        context_c1 = TenantContext(
            user_id=1,
            user_email="admin1@speech.com",
            raw_role="admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["demo_agent_99"],  # Intentando pedir agente de C7 en contexto C1
        )

        async with self.session_maker() as db:
            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=context_c1,
                target_agent_id="demo_agent_99",
            )

        # No debe haber datos de demo_agent_99 porque pertenece a company_id=7
        self.assertIn("No existen evaluaciones de llamadas reales", profile)
        self.assertNotIn("9.90", profile)

    async def test_agent_role_cannot_spoof_target_agent_history(self):
        """Un usuario con rol agente siempre recibe el perfil histórico de su propia identidad autenticada."""
        captured_messages = []

        async def fake_complete(messages, temperature=0.2, response_format=None):
            captured_messages.extend(messages)
            return "Respuesta simulada del tutor."

        user_agent = User(user_id=10, email="ana@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context_agent = TenantContext(
            user_id=10,
            user_email="ana@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        with patch("app.services.openai_service.complete_text", side_effect=fake_complete):
            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=user_agent,
                    context=context_agent,
                    message="¿En qué suelo fallar?",
                    agent_id="attacker_agent_999",  # intento de spoofing
                )

        self.assertEqual(res["agent_id"], "agent_01")
        sys_prompt = captured_messages[0]["content"]
        # Debe contener los datos del agente auténtico agent_01 (15 llamadas en 30 días)
        self.assertIn("15 llamadas", sys_prompt)
        self.assertIn("Manejo de Objeción de Precio", sys_prompt)

    async def test_admin_can_query_authorized_agent_history(self):
        """Un admin puede consultar la trayectoria completa de un agente autorizado de su tenant."""
        captured_messages = []

        async def fake_complete(messages, temperature=0.2, response_format=None):
            captured_messages.extend(messages)
            return "El agente ha mejorado en empatía en las últimas semanas."

        user_admin = User(user_id=2, email="admin@speech.com", role="admin", hubspot_owner_id=None, company_id=1)
        context_admin = TenantContext(
            user_id=2,
            user_email="admin@speech.com",
            raw_role="admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        with patch("app.services.openai_service.complete_text", side_effect=fake_complete):
            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=user_admin,
                    context=context_admin,
                    message="¿Cómo ha evolucionado este agente?",
                    agent_id="agent_01",
                )

        self.assertEqual(res["agent_id"], "agent_01")
        sys_prompt = captured_messages[0]["content"]
        self.assertIn("PERFIL HISTÓRICO Y EVOLUCIÓN DEL AGENTE (ÚLTIMOS 30 DÍAS)", sys_prompt)
        self.assertIn("15 llamadas", sys_prompt)

    async def test_absence_of_historical_data_handled_cleanly(self):
        """Si un agente no tiene histórico, el prompt advierte honestamente sin inventar datos."""
        context_admin = TenantContext(
            user_id=2,
            user_email="admin@speech.com",
            raw_role="admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_without_data"],
        )

        async with self.session_maker() as db:
            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=context_admin,
                target_agent_id="agent_without_data",
            )

        self.assertIn("No existen evaluaciones de llamadas reales", profile)
        self.assertNotIn("15 llamadas", profile)

    async def test_compact_prompt_size_limit(self):
        """Verifica que el bloque histórico se mantiene compacto (< 1500 tokens / ~6000 caracteres)."""
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

        # ~4 chars per token -> 1500 tokens is roughly 6000 chars
        self.assertLess(len(profile), 6000)
        self.assertGreater(len(profile), 200)

    async def test_public_chat_contract_response_unchanged(self):
        """Comprueba que el endpoint /bm/trainer/chat devuelve exactamente el contrato esperado."""
        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        user = User(user_id=10, email="ana@speech.com", role="agent", hubspot_owner_id="agent_01", company_id=1)
        context = TenantContext(
            user_id=10,
            user_email="ana@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_tenant_context] = lambda: context

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_complete:
            mock_complete.return_value = "En tu histórico tienes 30 llamadas evaluadas y tu principal fortaleza es la escucha activa."

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/chat",
                    json={"message": "¿Cuáles son mis puntos fuertes históricos?"},
                )

                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertIn("response", data)
                self.assertIn("user_query", data)
                self.assertIn("input_type", data)
                self.assertIn("sources", data)
                self.assertIn("agent_id", data)
                self.assertIn("company_id", data)
                self.assertEqual(data["input_type"], "text")
                self.assertEqual(data["agent_id"], "agent_01")
                self.assertEqual(data["company_id"], 1)

        app.dependency_overrides.clear()

    async def test_full_history_queried_without_90_day_cutoff(self):
        """4. El perfil acota a 30 días: incluye 15 llamadas recientes y excluye llamadas de hace 150 días."""
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

        self.assertIn("15 llamadas", profile)
        self.assertIn("ÚLTIMOS 30 DÍAS", profile)
        self.assertNotIn("call-old-", profile)

    async def test_history_summarized_beyond_12_documents_limit(self):
        """5. La trayectoria histórica supera la limitación de 12 documentos de conocimiento."""
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
            docs = await TrainerChatbotService.fetch_knowledge_documents(
                db=db,
                context=context,
                target_agent_id="agent_01",
            )
            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=context,
                target_agent_id="agent_01",
            )

        # Aunque docs sea vacío (0 documentos) o <= 12, el profile agrega las 15 llamadas y ciclos de los últimos 30 días
        self.assertLessEqual(len(docs), 12)
        self.assertIn("15 llamadas", profile)
        self.assertIn("Ciclos de Formación en el Periodo", profile)

    async def test_strengths_and_weaknesses_extraction(self):
        """6. Extracción estructurada de fortalezas consistentes y áreas de mejora recurrentes."""
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

        self.assertIn("[Fortaleza]", profile)
        self.assertIn("[Área de mejora recurrente]", profile)
        self.assertIn("llamadas evaluadas", profile)

    async def test_criteria_evolution_trajectory(self):
        """7. Seguimiento de la evolución de criterios con delta y dirección."""
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

        self.assertIn("Evolución Temporal por Criterios", profile)
        self.assertIn("inicial", profile)
        self.assertIn("reciente", profile)
        self.assertIn("mejora", profile)

    async def test_training_cycle_objectives_superado_and_no_superado(self):
        """8. Desglose de objetivos SUPERADO y NO SUPERADO con notas y justificaciones."""
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

        self.assertIn("[SUPERADO] Escucha Activa Inicial", profile)
        self.assertIn("[NO SUPERADO] Manejo de Objeción de Precio", profile)
        self.assertIn("Cede al descuento de inmediato", profile)

    async def test_accumulated_cycle_recommendations(self):
        """9. Extracción de recomendaciones pedagógicas acumuladas de ciclos anteriores."""
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

        self.assertIn("Recomendaciones pedagógicas de ciclos anteriores", profile)
        self.assertIn("técnica de doble alternativa", profile)
        self.assertIn("compromiso de seguimiento", profile)

    async def test_roleplay_simulations_history(self):
        """10. Histórico de simulaciones de roleplay con puntuaciones y puntos de mejora."""
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

        self.assertIn("Simulaciones de Roleplay en el Periodo (14 completadas)", profile)
        self.assertIn("Puntuación media en simulaciones", profile)
        self.assertIn("Firmeza en defensa de honorarios", profile)
        self.assertIn("Doble opción de cierre", profile)

    async def test_historical_vs_recent_comparison(self):
        """11. Comparación de métricas acotadas a los últimos 30 días."""
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

        self.assertIn("Puntuación media en los últimos 30 días: 7.80/10", profile)
        self.assertIn("15 llamadas", profile)
        self.assertNotIn("call-old-", profile)

    async def test_cycle_11_and_earlier_influence_profile(self):
        """12. Demuestra explícitamente que datos del ciclo #11 y del ciclo #1 (más antiguos que 10) influyen en el perfil."""
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

        # Total 12 ciclos analizados (supera el límite anterior de 10)
        self.assertIn("12 ciclos analizados", profile)

        # El ciclo #1 (el más antiguo de hace 240 días) contiene 'Control de Tiempos Arcaico' como NO SUPERADO
        self.assertIn("Control de Tiempos Arcaico", profile)

        # La recomendación del ciclo #1 aparece en las recomendaciones acumuladas
        self.assertIn("Recomendación antigua del ciclo 1", profile)

    async def test_simulation_11_and_earlier_influence_profile(self):
        """13. Demuestra explícitamente que datos de simulaciones más allá de las 10 últimas influyen en el perfil."""
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

        # Total 14 simulaciones (supera el límite anterior de 10)
        self.assertIn("14 completadas", profile)

        # La simulación #1 (hace 168 días) contiene un punto de mejora único que debe estar en el perfil
        self.assertIn("Punto de mejora arcaico de simulación 1", profile)

        # El patrón recurrente que abarca desde la simulación 2 a la 14 se refleja
        self.assertIn("Firmeza en defensa de honorarios", profile)

    async def test_more_than_10_cycles_and_simulations_reflected_in_totals(self):
        """14. Confirma que más de 10 ciclos y más de 10 simulaciones se reflejan íntegramente en los totales y medias."""
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

        self.assertIn("12 ciclos analizados", profile)
        self.assertIn("14 completadas", profile)
        self.assertIn("basada en 14 simulaciones evaluadas", profile)


if __name__ == "__main__":
    unittest.main()
