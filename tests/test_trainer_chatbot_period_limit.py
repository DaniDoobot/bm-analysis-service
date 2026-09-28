"""
Test Suite: Trainer Chatbot Period Limitation (Max 15 Natural Days)
===================================================================
Covers all requirements from Incidence #8:
1. "últimos 7 días" → permitido (<= 15 días).
2. "últimos 15 días" → permitido (<= 15 días).
3. "últimos 16 días" → bloqueado/acotar (> 15 días).
4. "últimos 30 días" → bloqueado (> 15 días).
5. "del 1 al 15 de junio" → permitido (15 días).
6. "del 1 al 16 de junio" → bloqueado (16 días).
7. "todo junio" → bloqueado (> 15 días).
8. "primera quincena de junio" → permitido (15 días).
9. pregunta general "¿cómo he evolucionado?" → NO bloqueada (histórico completo).
10. pregunta general "¿en qué suelo fallar?" → NO bloqueada (histórico completo).
11. modo admin con periodo válido → permitido.
12. modo admin con periodo >15 días → bloqueado con tono para supervisión/admin.
13. aislamiento/tenancy sigue intacto (company_id y agent scoping respetados).
14. histórico completo sintetizado sigue disponible en preguntas generales.
15. el contexto detallado no crece linealmente con el número de evaluaciones (agregación previa).
"""
import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import BigInteger, delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from sqlalchemy.pool import StaticPool
from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.trainer import TrainerSession, TrainerEvaluation
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationJob, MassEvaluationRun
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.services.trainer_chatbot_service import (
    TrainerChatbotService,
    DetailedPeriodResult,
    AGENT_BLOCKING_MSG,
    ADMIN_BLOCKING_MSG,
)


class TestTrainerChatbotPeriodLimit(unittest.IsolatedAsyncioTestCase):

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
            c1 = Company(company_id=1, company_name="Empresa Demo", company_key="empresa-demo", is_demo=False)
            c2 = Company(company_id=2, company_name="Otra Empresa", company_key="otra-empresa", is_demo=False)
            s1 = Service(service_id=1, company_id=1, service_name="Atención", service_key="atencion")
            db.add_all([c1, c2, s1])

            # Agente normal
            self.agent_user = User(
                user_id=10,
                username="agente_demo",
                email="agente@demo.com",
                role="agent",
                company_id=1,
                hubspot_owner_id="agent_01",
                password_hash="test_pw_hash_agent",
                is_active=True,
            )
            # Supervisor / Admin
            self.admin_user = User(
                user_id=20,
                username="admin_demo",
                email="admin@demo.com",
                role="company_admin",
                company_id=1,
                password_hash="test_pw_hash_admin",
                is_active=True,
            )
            db.add_all([self.agent_user, self.admin_user])

            # Jobs y runs para MassEvaluationResult
            job = MassEvaluationJob(job_id=1, company_id=1, service_id=1, prompt_id=1, job_name="Job 1")
            run = MassEvaluationRun(run_id=1, job_id=1, trigger_type="manual", status="completed")
            db.add_all([job, run])
            await db.commit()

        self.context_agent = TenantContext(
            user_id=10,
            user_email="agente@demo.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=["agent_01"],
            is_super_admin=False,
            is_company_admin=False,
        )
        self.context_admin = TenantContext(
            user_id=20,
            user_email="admin@demo.com",
            raw_role="company_admin",
            normalized_role=InternalRole.COMPANY_ADMIN,
            company_id=1,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=["agent_01"],
            is_super_admin=False,
            is_company_admin=True,
        )
        self.ref_date = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)

    # 1. "últimos 7 días" → permitido
    def test_01_last_7_days_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analiza mis evaluaciones de los últimos 7 días",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 7)
        self.assertIsNone(res.blocking_message)

    # 2. "últimos 15 días" → permitido
    def test_02_last_15_days_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "¿Cómo lo hice en los últimos 15 días?",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 15)
        self.assertIsNone(res.blocking_message)

    # 3. "últimos 16 días" → bloqueado/acotar
    def test_03_last_16_days_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Revisa mis llamadas de los últimos 16 días",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.period_days, 16)
        self.assertIn("hasta 15 días", res.blocking_message)

    # 4. "últimos 30 días" → bloqueado
    def test_04_last_30_days_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Haz un análisis detallado de los últimos 30 días",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.period_days, 30)
        self.assertIn("hasta 15 días", res.blocking_message)

    # 5. "del 1 al 15 de junio" → permitido
    def test_05_explicit_1_to_15_june_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Dime qué tal lo hice del 1 al 15 de junio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 15)
        self.assertIsNone(res.blocking_message)

    # 6. "del 1 al 16 de junio" → bloqueado
    def test_06_explicit_1_to_16_june_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Revisa del 1 al 16 de junio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.period_days, 16)
        self.assertIn("hasta 15 días", res.blocking_message)

    # 7. "todo junio" → bloqueado
    def test_07_full_month_june_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame todo junio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.period_days, 30)
        self.assertIn("hasta 15 días", res.blocking_message)

    # 8. "primera quincena de junio" → permitido
    def test_08_first_fortnight_june_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame la primera quincena de junio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 15)
        self.assertIsNone(res.blocking_message)

    # 8b. "segunda quincena de junio" → 15 días → permitido
    def test_08b_second_fortnight_june_30days_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame la segunda quincena de junio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 15)
        self.assertEqual(res.start_date.day, 16)
        self.assertEqual(res.end_date.day, 30)
        self.assertIsNone(res.blocking_message)

    # 8c. "segunda quincena de julio" → 16 días → bloqueado
    def test_08c_second_fortnight_july_31days_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame la segunda quincena de julio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.period_days, 16)
        self.assertEqual(res.start_date.day, 16)
        self.assertEqual(res.end_date.day, 31)
        self.assertIn("hasta 15 días", res.blocking_message)

    # 8d. Confirmar que no se altera "primera quincena de julio" → 15 días → permitido
    def test_08d_first_fortnight_july_31days_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame la primera quincena de julio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 15)
        self.assertEqual(res.start_date.day, 1)
        self.assertEqual(res.end_date.day, 15)
        self.assertIsNone(res.blocking_message)

    # 9. Pregunta general "¿cómo he evolucionado?" → NO bloqueada
    def test_09_general_question_evolution_not_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "¿Cómo he evolucionado?",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertFalse(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)

    # 10. Pregunta general "¿en qué suelo fallar?" → NO bloqueada
    def test_10_general_question_fail_areas_not_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "¿En qué suelo fallar habitualmente?",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertFalse(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)

    # 11. Modo admin con periodo válido → permitido
    async def test_11_admin_mode_valid_period_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analiza a este agente del 1 al 10 de junio",
            is_admin=True,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 10)

        # Simular process_chat para admin con periodo válido: debe invocar LLM
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "En el periodo del 1 al 10 de junio, el agente mantuvo un buen desempeño."
            async with self.session_maker() as db:
                chat_res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.admin_user,
                    context=self.context_admin,
                    message="Analiza a este agente del 1 al 10 de junio",
                    agent_id="agent_01",
                )
            self.assertTrue(mock_llm.called)
            self.assertIn("buen desempeño", chat_res["response"])

    # 12. Modo admin con periodo >15 días → bloqueado con tono admin
    async def test_12_admin_mode_over_15_days_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analiza a este agente durante junio",
            is_admin=True,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.blocking_message, ADMIN_BLOCKING_MSG)
        self.assertIn("desempeño del agente", res.blocking_message)

        # En process_chat NO debe llamar al LLM
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            async with self.session_maker() as db:
                chat_res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.admin_user,
                    context=self.context_admin,
                    message="Analiza a este agente durante junio",
                    agent_id="agent_01",
                )
            mock_llm.assert_not_called()
            self.assertEqual(chat_res["response"], ADMIN_BLOCKING_MSG)

    # 13. Aislamiento / tenancy sigue intacto
    async def test_13_tenant_isolation_intact(self):
        start_dt = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        async with self.session_maker() as db:
            res_c2 = MassEvaluationResult(
                mass_analysis_id=999,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                company_id=2,  # Otra empresa
                service_id=1,
                hubspot_owner_id="agent_01",
                call_id="call_c2_leaked",
                status="completed",
                call_timestamp=start_dt,
                evaluacion_global=Decimal("9.9"),
                result_json={"resumen": "Llamada de otra empresa que no debe verse"},
            )
            db.add(res_c2)
            await db.commit()

            summary_text = await TrainerChatbotService.build_detailed_period_summary(
                db=db,
                context=self.context_agent,
                target_agent_id="agent_01",
                start_date=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
                end_date=datetime(2026, 6, 15, 23, 59, 59, tzinfo=timezone.utc),
            )

        # No debe figurar la llamada de la compañía 2
        self.assertNotIn("Llamada de otra empresa", summary_text)
        self.assertIn("No constan evaluaciones de llamadas reales ni simulaciones", summary_text)

    # 14. Histórico completo sintetizado sigue disponible en preguntas generales
    async def test_14_general_query_uses_full_historical_profile(self):
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "Has mostrado una evolución constante en tu desempeño general."
            async with self.session_maker() as db:
                await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.context_agent,
                    message="¿Cómo he evolucionado?",
                )
            self.assertTrue(mock_llm.called)
            called_messages = mock_llm.call_args[1]["messages"]
            system_prompt = next(m["content"] for m in called_messages if m["role"] == "system")
            # El perfil histórico completo debe estar presente en el prompt
            self.assertIn("PERFIL HISTÓRICO Y EVOLUCIÓN COMPLETA DEL AGENTE", system_prompt)

    # 15. El contexto detallado no crece linealmente con el número de evaluaciones
    async def test_15_context_bounded_non_linear_growth(self):
        eval_date = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
        async with self.session_maker() as db:
            # Insertar 50 evaluaciones dentro del tramo
            bulk_evals = []
            for i in range(50):
                bulk_evals.append(
                    MassEvaluationResult(
                        mass_analysis_id=100 + i,
                        run_id=1,
                        job_id=1,
                        prompt_id=1,
                        prompt_snapshot="{}",
                        company_id=1,
                        service_id=1,
                        hubspot_owner_id="agent_01",
                        call_id=f"call_{i}",
                        status="completed",
                        call_timestamp=eval_date + timedelta(hours=i),
                        evaluacion_global=Decimal("7.5"),
                        result_json={"resumen": f"Resumen de la llamada #{i} con texto largo para verificar acotación."},
                    )
                )
            db.add_all(bulk_evals)
            await db.commit()

            summary_text = await TrainerChatbotService.build_detailed_period_summary(
                db=db,
                context=self.context_agent,
                target_agent_id="agent_01",
                start_date=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
                end_date=datetime(2026, 6, 15, 23, 59, 59, tzinfo=timezone.utc),
            )

        # Debe contener la métrica agregada
        self.assertIn("50 llamadas evaluadas", summary_text)
        # Debe contener como máximo 3 llamadas de ejemplo, NO las 50
        self.assertIn("Llamada #1", summary_text)
        self.assertIn("Llamada #2", summary_text)
        self.assertIn("Llamada #3", summary_text)
        self.assertNotIn("Llamada #4", summary_text)
        self.assertNotIn("Llamada #10", summary_text)
        # El tamaño del bloque sintetizado debe ser estrictamente acotado (< 2000 caracteres)
        self.assertLess(len(summary_text), 2000)


if __name__ == "__main__":
    unittest.main()
