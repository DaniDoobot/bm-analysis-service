"""
Test Suite: Trainer Chatbot Simulation Distinction and Accurate Counting
==========================================================================
Validates:
1. 4 sesiones totales / 3 completadas / 0 evaluadas (caso real de Eugenia Carreño).
2. Sesión started aparece en total registradas pero no en completadas.
3. Sesión incompleta no afecta cálculo de medias ni evolución.
4. Sesión incompleta no afecta fortalezas ni debilidades cualitativas.
5. Grounding prompt instruye correctamente para responder "¿cuántas simulaciones tengo?" con el total.
6. Grounding prompt instruye correctamente para responder "¿cuántas he completado?" con completadas.
7. Grounding prompt instruye correctamente para responder "¿cuántas tienen evaluación?" con evaluadas.
8. Histórico existente con simulaciones completadas y evaluadas sigue funcionando íntegro.
9. Tenant isolation sigue 100% intacto (sesiones de otra compañía no se cuentan).
"""
import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_chatbot_counts.db"

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
from app.models.trainer import (
    TrainerSession,
    TrainerEvaluation,
    TrainerSimulation,
)
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.services.trainer_chatbot_service import TrainerChatbotService


class TestTrainerChatbotSimulationCounts(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session_maker() as db:
            await db.execute(delete(TrainerEvaluation))
            await db.execute(delete(TrainerSession))
            await db.execute(delete(TrainerSimulation))
            await db.execute(delete(User))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_demo=False)
            c2 = Company(company_id=2, company_name="Otra Empresa", company_key="otra-empresa", is_demo=False)
            s1 = Service(service_id=1, company_id=1, service_name="Atención", service_key="atencion")
            s2 = Service(service_id=2, company_id=2, service_name="Servicio B", service_key="servicio-b")
            db.add_all([c1, c2, s1, s2])

            sim = TrainerSimulation(
                simulation_id=10,
                company_id=1,
                service_id=1,
                name="Prueba2",
                code="121314",
                roleplay_prompt="Escenario de práctica.",
                status="published",
            )
            db.add(sim)
            await db.commit()

        self.context_c1 = TenantContext(
            user_id=157,
            user_email="ecarreno@boston.es",
            raw_role="agente",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["1375831791"],
        )

    async def test_1_eugenia_scenario_4_total_3_completed_0_evaluated(self):
        """Caso Eugenia Carreño: 4 registradas, 3 completadas, 0 evaluadas, 1 no finalizada."""
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            # 1 sesión started (35s)
            s1 = TrainerSession(
                session_id=12,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-12",
                status="started",
                evaluation_status="started",
                created_at=now - timedelta(days=3),
            )
            # 3 sesiones completed sin evaluación
            s2 = TrainerSession(
                session_id=14,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-14",
                status="completed",
                evaluation_status="evaluation_pending",
                created_at=now - timedelta(days=2),
            )
            s3 = TrainerSession(
                session_id=15,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-15",
                status="completed",
                evaluation_status="completed_waiting_recording",
                created_at=now - timedelta(days=1),
            )
            s4 = TrainerSession(
                session_id=17,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-17",
                status="completed",
                evaluation_status="completed_waiting_recording",
                created_at=now,
            )
            db.add_all([s1, s2, s3, s4])
            await db.commit()

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=self.context_c1,
                target_agent_id="1375831791",
            )

        self.assertIn("Simulaciones registradas/iniciadas: 4", profile)
        self.assertIn("Simulaciones completadas: 3", profile)
        self.assertIn("Simulaciones evaluadas con nota: 0", profile)
        self.assertIn("Simulaciones interrumpidas/no finalizadas: 1", profile)
        self.assertIn("Histórico de Simulaciones de Roleplay (3 completadas)", profile)
        # Sin evaluaciones con nota, no debe haber puntuación media
        self.assertNotIn("Puntuación media en simulaciones", profile)

    async def test_2_started_session_appears_in_total_not_in_completed(self):
        """Una sesión en estado started figura en total registradas pero 0 completadas."""
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            s1 = TrainerSession(
                session_id=20,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-started-only",
                status="started",
                created_at=now,
            )
            db.add(s1)
            await db.commit()

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=self.context_c1,
                target_agent_id="1375831791",
            )

        self.assertIn("Simulaciones registradas/iniciadas: 1", profile)
        self.assertIn("Simulaciones completadas: 0", profile)
        self.assertIn("Simulaciones evaluadas con nota: 0", profile)
        self.assertIn("Simulaciones interrumpidas/no finalizadas: 1", profile)

    async def test_3_incomplete_session_does_not_affect_averages(self):
        """Una sesión incompleta no debe alterar la media de puntuación."""
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            # 2 sesiones completadas con scores 6.0 y 8.0 -> media = 7.0
            s_comp1 = TrainerSession(
                session_id=31,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-c1",
                status="completed",
                created_at=now - timedelta(days=2),
            )
            s_comp2 = TrainerSession(
                session_id=32,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-c2",
                status="completed",
                created_at=now - timedelta(days=1),
            )
            ev1 = TrainerEvaluation(
                evaluation_id=31,
                session_id=31,
                prompt_snapshot="Snapshot",
                result_json={},
                score=Decimal("6.0"),
                created_at=now - timedelta(days=2),
            )
            ev2 = TrainerEvaluation(
                evaluation_id=32,
                session_id=32,
                prompt_snapshot="Snapshot",
                result_json={},
                score=Decimal("8.0"),
                created_at=now - timedelta(days=1),
            )
            # 1 sesión incompleta con una evaluación anómala de 10.0
            s_inc = TrainerSession(
                session_id=33,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-inc",
                status="started",
                created_at=now,
            )
            ev3 = TrainerEvaluation(
                evaluation_id=33,
                session_id=33,
                prompt_snapshot="Snapshot",
                result_json={},
                score=Decimal("10.0"),
                created_at=now,
            )
            db.add_all([s_comp1, s_comp2, s_inc, ev1, ev2, ev3])
            await db.commit()

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=self.context_c1,
                target_agent_id="1375831791",
            )

        # Media esperada: (6.0 + 8.0) / 2 = 7.0 (NO (6+8+10)/3 = 8.0)
        self.assertIn("Puntuación media en simulaciones: 7.0/10 (basada en 2 simulaciones evaluadas)", profile)
        self.assertIn("Simulaciones registradas/iniciadas: 3", profile)
        self.assertIn("Simulaciones completadas: 2", profile)
        self.assertIn("Simulaciones evaluadas con nota: 2", profile)
        self.assertIn("Simulaciones interrumpidas/no finalizadas: 1", profile)

    async def test_4_incomplete_session_does_not_affect_strengths_and_weaknesses(self):
        """Fortalezas o debilidades de una sesión incompleta no deben incluirse en el perfil."""
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            s_comp = TrainerSession(
                session_id=41,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-c",
                status="completed",
                created_at=now - timedelta(days=1),
            )
            ev_comp = TrainerEvaluation(
                evaluation_id=41,
                session_id=41,
                prompt_snapshot="Snapshot",
                result_json={},
                score=Decimal("7.5"),
                strengths=["Fortaleza Valida Completada"],
                improvement_points=["Mejora Valida Completada"],
                summary="Feedback de sesión completada.",
                created_at=now - timedelta(days=1),
            )
            s_inc = TrainerSession(
                session_id=42,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=1,
                service_id=1,
                call_id="call-i",
                status="started",
                created_at=now,
            )
            ev_inc = TrainerEvaluation(
                evaluation_id=42,
                session_id=42,
                prompt_snapshot="Snapshot",
                result_json={},
                score=Decimal("4.0"),
                strengths=["Fortaleza Fantasma Incompleta"],
                improvement_points=["Mejora Fantasma Incompleta"],
                summary="Feedback de sesión interrumpida.",
                created_at=now,
            )
            db.add_all([s_comp, ev_comp, s_inc, ev_inc])
            await db.commit()

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=self.context_c1,
                target_agent_id="1375831791",
            )

        self.assertIn("Fortaleza Valida Completada", profile)
        self.assertIn("Mejora Valida Completada", profile)
        self.assertIn("Feedback de sesión completada", profile)
        self.assertNotIn("Fortaleza Fantasma Incompleta", profile)
        self.assertNotIn("Mejora Fantasma Incompleta", profile)
        self.assertNotIn("Feedback de sesión interrumpida", profile)

    async def test_5_grounding_prompt_includes_rule_9_distinction(self):
        """Verifica que el prompt de grounding incluye las directrices de conteo para el LLM."""
        prompt = TrainerChatbotService.build_grounding_prompt(
            documents=[],
            target_agent_id="1375831791",
            historical_profile="Simulaciones: 4 registradas, 3 completadas, 0 evaluadas, 1 no finalizada.",
            is_admin=False,
        )
        self.assertIn("9. DISTINCIÓN Y CONTEO DE SIMULACIONES:", prompt)
        self.assertIn("¿cuántas simulaciones tengo?", prompt)
        self.assertIn("¿cuántas he completado?", prompt)
        self.assertIn("¿cuántas tienen evaluación?", prompt)

    async def test_6_tenant_isolation_intact(self):
        """Sesiones pertenecientes a otra compañía no deben ser contabilizadas."""
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            s_c2 = TrainerSession(
                session_id=51,
                simulation_id=10,
                agent_id="1375831791",
                agent_code="EC88",
                company_id=2,  # Otra empresa
                service_id=2,
                call_id="call-other-company",
                status="completed",
                created_at=now,
            )
            db.add(s_c2)
            await db.commit()

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=self.context_c1,
                target_agent_id="1375831791",
            )

        # Para el tenant company_id=1, no debe figurar la sesión de company_id=2
        self.assertNotIn("Simulaciones registradas/iniciadas: 1", profile)
        self.assertIn("No existen evaluaciones históricas de llamadas reales, ciclos formativos ni simulaciones registradas", profile)
