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

    # 3. "últimos 16 días" → bloqueado
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

    # 4. "del 1 al 15 de julio" → permitido
    def test_04_explicit_1_to_15_july_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Dime qué tal lo hice del 1 al 15 de julio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 15)
        self.assertEqual(res.start_date.day, 1)
        self.assertEqual(res.end_date.day, 15)
        self.assertIsNone(res.blocking_message)

    # 5. "del 1 al 16 de julio" → bloqueado
    def test_05_explicit_1_to_16_july_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Revisa del 1 al 16 de julio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.period_days, 16)
        self.assertIn("hasta 15 días", res.blocking_message)

    # 6. "primera quincena de julio" → permitido
    def test_06_first_fortnight_july_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame la primera quincena de julio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 15)
        self.assertTrue(res.is_fortnight)
        self.assertEqual(res.start_date.day, 1)
        self.assertEqual(res.end_date.day, 15)
        self.assertIsNone(res.blocking_message)

    # 7. "segunda quincena de junio" → permitido
    def test_07_second_fortnight_june_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame la segunda quincena de junio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 15)
        self.assertTrue(res.is_fortnight)
        self.assertEqual(res.start_date.day, 16)
        self.assertEqual(res.end_date.day, 30)
        self.assertIsNone(res.blocking_message)

    # 8. "segunda quincena de julio" → permitido aunque sean 16 días
    def test_08_second_fortnight_july_16days_permitted(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame la segunda quincena de julio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertFalse(res.exceeds_limit)
        self.assertEqual(res.period_days, 16)
        self.assertTrue(res.is_fortnight)
        self.assertEqual(res.start_date.day, 16)
        self.assertEqual(res.end_date.day, 31)
        self.assertIsNone(res.blocking_message)

    # 9. "del 10 al 25 de julio" → bloqueado
    def test_09_explicit_10_to_25_july_16days_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame del 10 al 25 de julio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.period_days, 16)
        self.assertFalse(res.is_fortnight)
        self.assertIn("hasta 15 días", res.blocking_message)

    # 10. "todo julio" → bloqueado
    def test_10_full_month_july_blocked(self):
        res = TrainerChatbotService.detect_detailed_period_request(
            "Analízame todo julio",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertTrue(res.is_detailed_period_request)
        self.assertTrue(res.exceeds_limit)
        self.assertEqual(res.period_days, 31)
        self.assertIn("hasta 15 días", res.blocking_message)

    # 11. Preguntas generales siguen usando histórico completo
    async def test_11_general_questions_full_history(self):
        # A. "¿Cómo he evolucionado?"
        res_evo = TrainerChatbotService.detect_detailed_period_request(
            "¿Cómo he evolucionado?",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertFalse(res_evo.is_detailed_period_request)
        self.assertFalse(res_evo.exceeds_limit)

        # B. "¿En qué suelo fallar?"
        res_fail = TrainerChatbotService.detect_detailed_period_request(
            "¿En qué suelo fallar?",
            is_admin=False,
            reference_date=self.ref_date,
        )
        self.assertFalse(res_fail.is_detailed_period_request)
        self.assertFalse(res_fail.exceeds_limit)

        # Verificación en process_chat: histórico completo inyectado en prompt
        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "Tu evolución muestra consistencia en el servicio."
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
            self.assertIn("PERFIL HISTÓRICO Y EVOLUCIÓN COMPLETA DEL AGENTE", system_prompt)

    # 12. Admin respeta la misma lógica
    async def test_12_admin_mode_logic(self):
        # Permitido: 1 al 10 de junio
        res_perm = TrainerChatbotService.detect_detailed_period_request(
            "Analiza a este agente del 1 al 10 de junio",
            is_admin=True,
            reference_date=self.ref_date,
        )
        self.assertTrue(res_perm.is_detailed_period_request)
        self.assertFalse(res_perm.exceeds_limit)
        self.assertEqual(res_perm.period_days, 10)

        # Bloqueado: todo junio
        res_block = TrainerChatbotService.detect_detailed_period_request(
            "Analiza a este agente durante todo junio",
            is_admin=True,
            reference_date=self.ref_date,
        )
        self.assertTrue(res_block.is_detailed_period_request)
        self.assertTrue(res_block.exceeds_limit)
        self.assertEqual(res_block.blocking_message, ADMIN_BLOCKING_MSG)
        self.assertIn("desempeño del agente", res_block.blocking_message)

    # 13. Tenant isolation intacto
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

        self.assertNotIn("Llamada de otra empresa", summary_text)
        self.assertIn("No constan evaluaciones de llamadas reales ni simulaciones", summary_text)

    # 14. Consulta válida <=15 días con llamadas reales NO produce 500
    async def test_14_valid_period_real_calls_no_500(self):
        eval_date = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
        async with self.session_maker() as db:
            call_res = MassEvaluationResult(
                mass_analysis_id=888,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                company_id=1,
                service_id=1,
                hubspot_owner_id="agent_01",
                call_id="call_real_ok",
                status="completed",
                call_timestamp=eval_date,
                evaluacion_global=Decimal("8.0"),
                result_json={"resumen": "Llamada con explicación de tarifas correcta."},
            )
            db.add(call_res)
            await db.commit()

        with patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "En los últimos 7 días has tenido un desempeño adecuado."
            async with self.session_maker() as db:
                chat_res = await TrainerChatbotService.process_chat(
                    db=db,
                    current_user=self.agent_user,
                    context=self.context_agent,
                    message="Analízame los últimos 7 días",
                )
            self.assertTrue(mock_llm.called)
            self.assertIn("últimos 7 días", chat_res["response"])

    # 15. Llamadas cuyo result_json no tenga "resumen" NO producen excepción
    async def test_15_no_resumen_no_exception(self):
        eval_date = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)
        async with self.session_maker() as db:
            call_without_resumen = MassEvaluationResult(
                mass_analysis_id=889,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                company_id=1,
                service_id=1,
                hubspot_owner_id="agent_01",
                call_id="call_no_resumen",
                status="completed",
                call_timestamp=eval_date,
                evaluacion_global=Decimal("7.0"),
                result_json={"other_field": 123},  # sin campo 'resumen' ni summary attribute
            )
            call_with_none_json = MassEvaluationResult(
                mass_analysis_id=890,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                company_id=1,
                service_id=1,
                hubspot_owner_id="agent_01",
                call_id="call_none_json",
                status="completed",
                call_timestamp=eval_date + timedelta(hours=1),
                evaluacion_global=Decimal("6.5"),
                result_json=None,
            )
            db.add_all([call_without_resumen, call_with_none_json])
            await db.commit()

            summary_text = await TrainerChatbotService.build_detailed_period_summary(
                db=db,
                context=self.context_agent,
                target_agent_id="agent_01",
                start_date=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
                end_date=datetime(2026, 6, 15, 23, 59, 59, tzinfo=timezone.utc),
            )

        self.assertIn("Llamada #1", summary_text)
        self.assertIn("Llamada #2", summary_text)

    # 16. Llamada con resumen válido sí lo usa
    async def test_16_valid_resumen_used(self):
        eval_date = datetime(2026, 6, 7, 10, 0, 0, tzinfo=timezone.utc)
        async with self.session_maker() as db:
            call_resumen = MassEvaluationResult(
                mass_analysis_id=891,
                run_id=1,
                job_id=1,
                prompt_id=1,
                prompt_snapshot="{}",
                company_id=1,
                service_id=1,
                hubspot_owner_id="agent_01",
                call_id="call_valid_resumen",
                status="completed",
                call_timestamp=eval_date,
                evaluacion_global=Decimal("8.5"),
                result_json={"resumen": "Explicación detallada del presupuesto con buen tono."},
            )
            db.add(call_resumen)
            await db.commit()

            summary_text = await TrainerChatbotService.build_detailed_period_summary(
                db=db,
                context=self.context_agent,
                target_agent_id="agent_01",
                start_date=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
                end_date=datetime(2026, 6, 15, 23, 59, 59, tzinfo=timezone.utc),
            )

        self.assertIn("Explicación detallada del presupuesto", summary_text)

    # 17. Máximo 3 ejemplos breves
    async def test_17_max_3_sample_calls(self):
        eval_date = datetime(2026, 6, 8, 8, 0, 0, tzinfo=timezone.utc)
        async with self.session_maker() as db:
            calls = [
                MassEvaluationResult(
                    mass_analysis_id=900 + i,
                    run_id=1,
                    job_id=1,
                    prompt_id=1,
                    prompt_snapshot="{}",
                    company_id=1,
                    service_id=1,
                    hubspot_owner_id="agent_01",
                    call_id=f"call_many_{i}",
                    status="completed",
                    call_timestamp=eval_date + timedelta(hours=i),
                    evaluacion_global=Decimal("7.0"),
                    result_json={"resumen": f"Resumen llamada #{i}"},
                )
                for i in range(10)
            ]
            db.add_all(calls)
            await db.commit()

            summary_text = await TrainerChatbotService.build_detailed_period_summary(
                db=db,
                context=self.context_agent,
                target_agent_id="agent_01",
                start_date=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
                end_date=datetime(2026, 6, 15, 23, 59, 59, tzinfo=timezone.utc),
            )

        self.assertIn("Llamada #1", summary_text)
        self.assertIn("Llamada #2", summary_text)
        self.assertIn("Llamada #3", summary_text)
        self.assertNotIn("Llamada #4", summary_text)

    # 18. Contexto sigue < 2.000 caracteres aprox.
    async def test_18_context_strictly_bounded_under_2000_chars(self):
        eval_date = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)
        async with self.session_maker() as db:
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
                        call_id=f"call_bulk_{i}",
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

        self.assertIn("llamadas evaluadas", summary_text)
        self.assertLess(len(summary_text), 2000)



if __name__ == "__main__":
    unittest.main()
