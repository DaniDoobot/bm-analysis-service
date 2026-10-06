"""
Test Suite: Trainer Chatbot 30-Day Global Limitation
====================================================
Validates all requirements for the 30-day temporal limit in AI Tutor:
1. "Cuéntame mi evolución general" (no explicit period) -> get_agent_evolution called with period="30d" (never "all").
2. Historical profile (build_agent_historical_profile) -> real calls bounded to last 30 days.
3. Training questions / sessions -> TrainerSession bounded to last 30 days.
4. Training cycles -> TrainingAgentReport bounded to last 30 days.
5. Qualitative criteria search ("Dame dos llamadas con empatía baja") -> defaults to 30-day window.
6. "Analiza mis últimos 60 días" -> deterministic rejection asking to restrict to max 30 days (no LLM, no heavy queries).
7. "¿Cómo he evolucionado los últimos 3 meses?" -> deterministic rejection asking to restrict to max 30 days.
8. "Todo mi histórico" -> deterministic rejection asking to restrict to max 30 days.
9. "Cuéntame mi evolución de los últimos 30 días" -> allowed as global summary (calls LLM).
10. "Analízame en detalle los últimos 30 días" -> blocked by the 15-day detailed analysis limit.
11. Single call_id inquiry older than 30 days -> allowed (not constrained by 30-day limit).
12. Comparison between two call_ids older than 30 days -> allowed (not constrained by 30-day limit).
"""
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import BigInteger, delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.trainer import TrainerSession, TrainerEvaluation, TrainerSimulation
from app.models.personalized_training import TrainingAgentReport
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationJob, MassEvaluationRun
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.services.trainer_chatbot_service import (
    TrainerChatbotService,
    AGENT_BLOCKING_MSG,
    AGENT_GLOBAL_PERIOD_BLOCKING_MSG,
    ADMIN_GLOBAL_PERIOD_BLOCKING_MSG,
)


class TestTrainerChatbot30dLimit(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session_maker() as db:
            await db.execute(delete(TrainerEvaluation))
            await db.execute(delete(TrainerSession))
            await db.execute(delete(TrainerSimulation))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(MassEvaluationResult))
            await db.execute(delete(MassEvaluationRun))
            await db.execute(delete(MassEvaluationJob))
            await db.execute(delete(User))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            c = Company(company_id=1, company_name="Boston Medical", company_key="bm", is_demo=False)
            s = Service(service_id=1, company_id=1, service_name="Atención", service_key="atencion")
            u_agent = User(
                user_id=10,
                username="ana_garcia",
                email="ana@speech.com",
                role="agent",
                hubspot_owner_id="agent_01",
                password_hash="pw_agent",
                is_active=True,
                company_id=1,
            )
            u_admin = User(
                user_id=2,
                username="admin_speech",
                email="admin@speech.com",
                role="company_admin",
                hubspot_owner_id=None,
                password_hash="pw_admin",
                is_active=True,
                company_id=1,
            )
            job = MassEvaluationJob(job_id=1, company_id=1, service_id=1, prompt_id=1, job_name="Job 1")
            run = MassEvaluationRun(run_id=1, job_id=1, trigger_type="manual", status="completed")

            db.add_all([c, s, u_agent, u_admin, job, run])
            await db.commit()

        self.agent_user = u_agent
        self.admin_user = u_admin
        self.agent_context = TenantContext(
            user_id=10,
            user_email="ana@speech.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )
        self.admin_context = TenantContext(
            user_id=2,
            user_email="admin@speech.com",
            raw_role="admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            is_super_admin=False,
            company_id=1,
            allowed_company_ids=[1],
            allowed_agent_ids=["agent_01"],
        )

    # 1. "Cuéntame mi evolución general" llama a get_agent_evolution con period="30d" (nunca "all")
    async def test_01_general_evolution_uses_30d_never_all(self):
        with patch("app.services.trainer_chatbot_service.get_agent_evolution", new_callable=AsyncMock) as mock_evo, \
             patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            mock_evo.return_value = {
                "summary": {"total_analyses": 5, "avg_evaluacion_global": 7.5},
                "trend": {"evaluacion_global_direction": "stable"},
                "strengths": [],
                "weaknesses": [],
            }
            mock_llm.return_value = "Tu evolución en los últimos 30 días es positiva."

            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Cuéntame mi evolución general",
                )

            self.assertIn("response", res)
            self.assertTrue(mock_evo.called)
            # Confirm period="30d" was used, never "all"
            for call_args in mock_evo.call_args_list:
                kwargs = call_args[1]
                self.assertNotEqual(kwargs.get("period"), "all")
                if "period" in kwargs:
                    self.assertEqual(kwargs.get("period"), "30d")

    # 2. Perfil histórico (build_agent_historical_profile) acota llamadas a 30 días
    async def test_02_historical_profile_bounded_to_30_days(self):
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            # Llamada reciente (hace 10 días)
            c_recent = MassEvaluationResult(
                mass_analysis_id=1,
                run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call-recent-10d",
                company_id=1, service_id=1, hubspot_owner_id="agent_01",
                agent_name="Ana García",
                call_timestamp=now - timedelta(days=10),
                analysis_timestamp=now - timedelta(days=10),
                status="completed", evaluacion_global=Decimal("8.00"),
                result_json={"evaluacion_global": 8.0, "empatia": 8.5},
            )
            # Llamada antigua (hace 60 días)
            c_old = MassEvaluationResult(
                mass_analysis_id=2,
                run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call-old-60d",
                company_id=1, service_id=1, hubspot_owner_id="agent_01",
                agent_name="Ana García",
                call_timestamp=now - timedelta(days=60),
                analysis_timestamp=now - timedelta(days=60),
                status="completed", evaluacion_global=Decimal("4.00"),
                result_json={"evaluacion_global": 4.0, "empatia": 4.0},
            )
            db.add_all([c_recent, c_old])
            await db.commit()

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=self.agent_context,
                target_agent_id="agent_01",
            )

            self.assertIn("ÚLTIMOS 30 DÍAS", profile)
            # Total llamadas en los últimos 30 días debe ser 1 (la de hace 10 días)
            self.assertIn("1 llamadas", profile)
            self.assertNotIn("call-old-60d", profile)

    # 3. Preguntas de formación -> TrainerSession acotadas a 30 días
    async def test_03_training_sessions_bounded_to_30_days(self):
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            sim = TrainerSimulation(
                simulation_id=1, company_id=1, service_id=1,
                name="Simulación Objeciones", code="SIM-01",
                roleplay_prompt="Roleplay", status="published",
            )
            db.add(sim)
            await db.flush()

            # Sesión dentro de 30 días (hace 15 días)
            sess_recent = TrainerSession(
                session_id=10, simulation_id=1, agent_id="agent_01", agent_code="AG-01",
                company_id=1, service_id=1, call_id="sim-recent-15d",
                status="completed", evaluation_status="completed",
                created_at=now - timedelta(days=15),
            )
            ev_recent = TrainerEvaluation(
                evaluation_id=10, session_id=10, prompt_snapshot="{}",
                result_json={"passed": True},
                score=Decimal("8.50"), summary="Excelente manejo reciente.",
                strengths=["Empatía reciente"], improvement_points=["Cierre"],
                created_at=now - timedelta(days=15),
            )
            # Sesión fuera de 30 días (hace 45 días)
            sess_old = TrainerSession(
                session_id=11, simulation_id=1, agent_id="agent_01", agent_code="AG-01",
                company_id=1, service_id=1, call_id="sim-old-45d",
                status="completed", evaluation_status="completed",
                created_at=now - timedelta(days=45),
            )
            ev_old = TrainerEvaluation(
                evaluation_id=11, session_id=11, prompt_snapshot="{}",
                result_json={"passed": True},
                score=Decimal("5.00"), summary="Evaluación antigua descartada.",
                strengths=["Fortaleza arcaica"], improvement_points=["Foco arcaico"],
                created_at=now - timedelta(days=45),
            )
            db.add_all([sess_recent, ev_recent, sess_old, ev_old])
            await db.commit()

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=self.agent_context,
                target_agent_id="agent_01",
            )

            # Debe incluir la sesión reciente de hace 15 días
            self.assertIn("Excelente manejo reciente", profile)
            # Debe excluir la sesión antigua de hace 45 días
            self.assertNotIn("Evaluación antigua descartada", profile)
            self.assertNotIn("Fortaleza arcaica", profile)

    # 4. Preguntas de ciclos -> TrainingAgentReport acotadas a 30 días
    async def test_04_training_reports_cycles_bounded_to_30_days(self):
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            # Reporte de ciclo dentro de 30 días (period_end hace 10 días)
            rep_recent = TrainingAgentReport(
                training_report_id=20, company_id=1, service_id=1,
                hubspot_owner_id="agent_01", agent_name="Ana García", agent_initials="AG",
                period_start=now - timedelta(days=25), period_end=now - timedelta(days=10),
                status="completed", avg_evaluacion_global=Decimal("7.90"),
                evolution_summary="Evolución reciente.",
                final_report_json={
                    "objectives_status": [
                        {"title": "Objetivo Reciente En 30d", "status": "SUPERADO", "score": 8.0, "base_score": 6.0}
                    ],
                    "recommendations": ["Recomendación de ciclo reciente."],
                },
                created_at=now - timedelta(days=10),
            )
            # Reporte de ciclo fuera de 30 días (period_end hace 45 días)
            rep_old = TrainingAgentReport(
                training_report_id=21, company_id=1, service_id=1,
                hubspot_owner_id="agent_01", agent_name="Ana García", agent_initials="AG",
                period_start=now - timedelta(days=60), period_end=now - timedelta(days=45),
                status="completed", avg_evaluacion_global=Decimal("6.00"),
                evolution_summary="Evolución antigua.",
                final_report_json={
                    "objectives_status": [
                        {"title": "Objetivo Antiguo Fuera De 30d", "status": "SUPERADO", "score": 7.0, "base_score": 5.0}
                    ],
                    "recommendations": ["Recomendación antigua excluida."],
                },
                created_at=now - timedelta(days=45),
            )
            db.add_all([rep_recent, rep_old])
            await db.commit()

            profile = await TrainerChatbotService.build_agent_historical_profile(
                db=db,
                context=self.agent_context,
                target_agent_id="agent_01",
            )

            self.assertIn("Objetivo Reciente En 30d", profile)
            self.assertIn("Recomendación de ciclo reciente", profile)
            self.assertNotIn("Objetivo Antiguo Fuera De 30d", profile)
            self.assertNotIn("Recomendación antigua excluida", profile)

    # 5. Búsqueda por criterios ("Dame dos llamadas con empatía baja") usa ventana de 30 días
    async def test_05_criteria_search_bounded_to_30_days(self):
        ref_dt = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        start_dt, end_dt, label = TrainerChatbotService.extract_search_period(
            "Dame dos llamadas con empatía baja",
            ref_dt=ref_dt,
        )
        self.assertEqual(label, "los últimos 30 días")
        expected_start = ref_dt - timedelta(days=30)
        self.assertEqual(start_dt, expected_start)
        self.assertEqual(end_dt, ref_dt)

    # 6. "Analiza mis últimos 60 días" -> respuesta determinista pidiendo acotar a máx 30 días (sin LLM)
    async def test_06_60_days_deterministic_rejection_no_llm(self):
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Analiza mis últimos 60 días",
                )

            mock_llm.assert_not_called()
            self.assertEqual(res["response"], AGENT_GLOBAL_PERIOD_BLOCKING_MSG)

    # 7. "¿Cómo he evolucionado los últimos 3 meses?" -> respuesta determinista pidiendo acotar a máx 30 días
    async def test_07_three_months_deterministic_rejection(self):
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="¿Cómo he evolucionado los últimos 3 meses?",
                )

            mock_llm.assert_not_called()
            self.assertEqual(res["response"], AGENT_GLOBAL_PERIOD_BLOCKING_MSG)

    # 8. "Todo mi histórico" -> respuesta determinista pidiendo acotar a máx 30 días
    async def test_08_todo_mi_historico_deterministic_rejection(self):
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Todo mi histórico",
                )

            mock_llm.assert_not_called()
            self.assertEqual(res["response"], AGENT_GLOBAL_PERIOD_BLOCKING_MSG)

    # 9. "Cuéntame mi evolución de los últimos 30 días" -> permitido como resumen global (llama al LLM)
    async def test_09_evolution_last_30_days_allowed_calls_llm(self):
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "En los últimos 30 días has demostrado una evolución positiva constante."

            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Cuéntame mi evolución de los últimos 30 días",
                )

            self.assertTrue(mock_llm.called)
            self.assertEqual(res["response"], "En los últimos 30 días has demostrado una evolución positiva constante.")

    # 10. "Analízame en detalle los últimos 30 días" -> bloqueado por el límite de 15 días
    async def test_10_detailed_analysis_last_30_days_blocked_by_15d_limit(self):
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Analízame en detalle los últimos 30 días",
                )

            mock_llm.assert_not_called()
            self.assertEqual(res["response"], AGENT_BLOCKING_MSG)

    # 11. Consulta de llamada concreta con call_id de hace más de 30 días -> permitido (NO bloqueado)
    async def test_11_single_call_id_older_than_30_days_allowed(self):
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            call_old = MassEvaluationResult(
                mass_analysis_id=99,
                run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call-old-75d",
                company_id=1, service_id=1, hubspot_owner_id="agent_01",
                agent_name="Ana García",
                call_timestamp=now - timedelta(days=75),
                analysis_timestamp=now - timedelta(days=75),
                status="completed", evaluacion_global=Decimal("7.50"),
                result_json={
                    "evaluacion_global": 7.5,
                    "resumen": "Llamada de hace 75 días sobre dudas de servicio.",
                    "puntos_fuertes": ["Claridad"],
                    "puntos_debiles": ["Cierre"],
                },
            )
            db.add(call_old)
            await db.commit()

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "En la llamada call-old-75d mostraste claridad pero faltó cierre."

            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="¿Qué tal estuvo la llamada call-old-75d?",
                )

            self.assertTrue(mock_llm.called)
            called_messages = mock_llm.call_args[1]["messages"]
            system_prompt = next(m["content"] for m in called_messages if m["role"] == "system")
            self.assertIn("DETALLE DE LA LLAMADA: call-old-75d", system_prompt)
            self.assertIn("call-old-75d", res["response"])

    # 12. Comparación de dos call_id de hace más de 30 días -> permitido (NO bloqueado)
    async def test_12_two_call_ids_comparison_older_than_30_days_allowed(self):
        now = datetime.now(timezone.utc)
        async with self.session_maker() as db:
            call_old_1 = MassEvaluationResult(
                mass_analysis_id=101,
                run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call-old-80d",
                company_id=1, service_id=1, hubspot_owner_id="agent_01",
                agent_name="Ana García",
                call_timestamp=now - timedelta(days=80),
                analysis_timestamp=now - timedelta(days=80),
                status="completed", evaluacion_global=Decimal("6.50"),
                result_json={"evaluacion_global": 6.5, "resumen": "Llamada antigua 80d"},
            )
            call_old_2 = MassEvaluationResult(
                mass_analysis_id=102,
                run_id=1, job_id=1, prompt_id=1, prompt_snapshot="{}",
                call_id="call-old-90d",
                company_id=1, service_id=1, hubspot_owner_id="agent_01",
                agent_name="Ana García",
                call_timestamp=now - timedelta(days=90),
                analysis_timestamp=now - timedelta(days=90),
                status="completed", evaluacion_global=Decimal("7.80"),
                result_json={"evaluacion_global": 7.8, "resumen": "Llamada antigua 90d"},
            )
            db.add_all([call_old_1, call_old_2])
            await db.commit()

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "Comparando ambas llamadas: en la llamada call-old-90d tuviste mejor desempeño que en la llamada call-old-80d."

            async with self.session_maker() as db:
                res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.agent_context,
                    message="Compara la llamada call-old-80d con la llamada call-old-90d",
                )

            self.assertTrue(mock_llm.called)
            called_messages = mock_llm.call_args[1]["messages"]
            system_prompt = next(m["content"] for m in called_messages if m["role"] == "system")
            self.assertIn("COMPARATIVA DE LLAMADAS", system_prompt)
            self.assertIn("call-old-80d", system_prompt)
            self.assertIn("call-old-90d", system_prompt)


if __name__ == "__main__":
    unittest.main()
