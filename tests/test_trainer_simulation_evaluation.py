import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_eval.db"

from sqlalchemy import BigInteger, delete, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.models.companies import Company
from app.models.criteria import PromptCriterion
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingCallEvaluation,
    TrainingCallSession,
    TrainingCompletionStatus,
    TrainingEvaluationPrompt,
    TrainingSimulationPrompt,
)
from app.models.prompts import Prompt
from app.models.services import Service
from app.services.personalized_training_service import (
    PersonalizedTrainingService,
    _build_personalized_training_evaluation_prompt,
    evaluate_training_session_task,
)


class TestTrainerSimulationEvaluation(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            await db.execute(delete(TrainingCallEvaluation))
            await db.execute(delete(TrainingCallSession))
            await db.execute(delete(TrainingCompletionStatus))
            await db.execute(delete(TrainingSimulationPrompt))
            await db.execute(delete(TrainingEvaluationPrompt))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(PromptCriterion))
            await db.execute(delete(Prompt))
            await db.execute(delete(Service))
            await db.execute(delete(Company))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(TrainingCallEvaluation))
            await db.execute(delete(TrainingCallSession))
            await db.execute(delete(TrainingCompletionStatus))
            await db.execute(delete(TrainingSimulationPrompt))
            await db.execute(delete(TrainingEvaluationPrompt))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(PromptCriterion))
            await db.execute(delete(Prompt))
            await db.execute(delete(Service))
            await db.execute(delete(Company))
            await db.commit()

    def test_prompt_builder_includes_all_context(self):
        """Verifica que _build_personalized_training_evaluation_prompt incluya todos los campos de contexto."""
        sim = TrainingSimulationPrompt(
            simulation_prompt_id=10,
            training_report_id=20,
            hubspot_owner_id="demo_owner_01",
            prompt_number=1,
            title="Objeción de Precio y Financiación",
            scenario_type="Ventas / Retención",
            prompt_text="El cliente considera que la cuota mensual es excesiva.",
            objective_focus_json=["Argumentar valor diferencial", "Ofrecer flexibilidad de pago"],
        )
        report = TrainingAgentReport(
            training_report_id=20,
            general_objectives_json=["Mejorar empatía", "Reducir tiempo de silencio"],
            specific_objectives_json=["Dominar matriz de objeciones de coste"],
        )
        crit1 = PromptCriterion(
            criterion_id=1,
            criterion_name="Escucha Activa",
            criterion_description="No interrumpe al cliente y confirma sus dudas.",
        )
        crit2 = PromptCriterion(
            criterion_id=2,
            criterion_name="Propuesta de Valor",
            criterion_description="Explica los beneficios tangibles.",
        )

        prompt = _build_personalized_training_evaluation_prompt(
            sim_prompt=sim,
            agent_report=report,
            criteria_items=[crit1, crit2],
            company_name="Empresa Demo B2B",
        )

        self.assertIn("Empresa Demo B2B", prompt)
        self.assertIn("Objeción de Precio y Financiación", prompt)
        self.assertIn("Ventas / Retención", prompt)
        self.assertIn("El cliente considera que la cuota mensual es excesiva", prompt)
        self.assertIn("Argumentar valor diferencial", prompt)
        self.assertIn("Ofrecer flexibilidad de pago", prompt)
        self.assertIn("Mejorar empatía", prompt)
        self.assertIn("Dominar matriz de objeciones de coste", prompt)
        self.assertIn("Escucha Activa", prompt)
        self.assertIn("Propuesta de Valor", prompt)
        self.assertIn("is_valid_roleplay", prompt)
        self.assertIn('"Escucha Activa": true', prompt)
        self.assertIn('"Propuesta de Valor": true', prompt)

    def test_prompt_builder_fallback_criteria(self):
        """Verifica que se usen criterios estándar de calidad cuando no hay criterios en BD."""
        prompt = _build_personalized_training_evaluation_prompt(
            sim_prompt=None,
            agent_report=None,
            criteria_items=[],
            company_name="Empresa Demo",
        )
        self.assertIn("Saludo profesional e identificación", prompt)
        self.assertIn("Escucha activa y empatía", prompt)
        self.assertIn("Manejo de dudas y objeciones", prompt)
        self.assertIn("is_valid_roleplay", prompt)

    def test_map_completion_to_dict_turn_mapping_and_backward_compatibility(self):
        """Verifica que _map_completion_to_dict maneje turnos Cliente/Usuario y estructuras modernas."""
        # Evaluación con transcripción usando Cliente:
        mock_eval = type("MockEval", (), {
            "score": Decimal("8.5"),
            "feedback": "Excelente manejo de la objeción.",
            "transcription": "Agente: Buenos días, le atiende Laura.\nCliente: Hola Laura, tengo una duda.\nAgente: Dígame.",
            "result_json": {
                "is_valid_roleplay": True,
                "score": 8.5,
                "objectives_met": ["Saludo cordial", "Escucha activa"],
                "areas_for_improvement": ["Cierre formal"],
                "result_json": {
                    "Saludo profesional": True,
                    "Escucha activa": True,
                    "Cierre formal": False,
                }
            }
        })()

        mock_completion = type("MockComp", (), {
            "completion_id": 1,
            "training_report_id": 10,
            "simulation_prompt_id": 100,
            "hubspot_owner_id": "demo_01",
            "status": "completed",
            "completed_at": datetime.now(timezone.utc),
            "evaluation_id": 50,
            "call_session_id": 60,
            "training_call_id": None,
            "training_phone_number": None,
            "notes": None,
            "prompt": type("MockPrompt", (), {"prompt_number": 1, "title": "Sim 1"})(),
            "evaluation": mock_eval,
            "created_at": datetime.now(timezone.utc),
        })()

        slot = PersonalizedTrainingService._map_completion_to_dict(mock_completion)

        self.assertEqual(slot["score"], 8.5)
        self.assertEqual(slot["feedback"], "Excelente manejo de la objeción.")
        self.assertEqual(slot["criteria"], {
            "Saludo profesional": True,
            "Escucha activa": True,
            "Cierre formal": False,
        })
        self.assertIn("Saludo cordial", slot["strengths"])
        self.assertIn("Cierre formal", slot["weaknesses"])

        # Verificar que Cliente se mapeó a paciente y Agente a agente
        turns = slot["transcription_turns"]
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0]["role"], "agente")
        self.assertEqual(turns[1]["role"], "paciente")
        self.assertEqual(turns[2]["role"], "agente")

    def test_map_completion_to_dict_legacy_flat_result_json(self):
        """Verifica compatibilidad con evaluaciones históricas que tenían result_json plano."""
        legacy_eval = type("MockEval", (), {
            "score": Decimal("7.0"),
            "feedback": "Buen intento.",
            "transcription": None,
            "result_json": {
                "Empatía": True,
                "Claridad": True,
                "Cierre": False,
                "score": 7.0,
            }
        })()

        mock_comp = type("MockComp", (), {
            "completion_id": 2,
            "training_report_id": 10,
            "simulation_prompt_id": 101,
            "hubspot_owner_id": "demo_01",
            "status": "completed",
            "completed_at": datetime.now(timezone.utc),
            "evaluation_id": 51,
            "call_session_id": 61,
            "training_call_id": None,
            "training_phone_number": None,
            "notes": None,
            "prompt": None,
            "evaluation": legacy_eval,
            "created_at": datetime.now(timezone.utc),
        })()

        slot = PersonalizedTrainingService._map_completion_to_dict(mock_comp)
        # Debe filtrar booleanos
        self.assertEqual(slot["criteria"], {
            "Empatía": True,
            "Claridad": True,
            "Cierre": False,
        })
        self.assertIn("Empatía", slot["strengths"])
        self.assertIn("Cierre", slot["weaknesses"])

    @patch("app.services.openai_service.analyze_audio_bytes")
    @patch("app.services.twilio_service.TwilioService.download_audio")
    async def test_evaluate_training_session_task_success(self, mock_download, mock_analyze):
        """Verifica el flujo completo de evaluación exitosa con criterios y objetivos guardados."""
        mock_download.return_value = b"fake_audio_bytes"
        mock_analyze.return_value = json.dumps({
            "is_valid_roleplay": True,
            "score": 8.0,
            "feedback": "El agente demostró gran solvencia en el tratamiento de la objeción.",
            "objectives_met": ["Identificación clara", "Sondeo de necesidad"],
            "areas_for_improvement": ["Cierre con acuerdos"],
            "result_json": {
                "Identificación y saludo": True,
                "Sondeo de necesidad": True,
                "Resolución y claridad": True,
                "Cierre de llamada": False,
            },
            "transcription": "Agente: Buenos días.\nCliente: Hola, llamaba por un cargo.",
        })

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
            service = Service(service_id=1, company_id=7, service_name="Front Atención", service_key="front_atencion")
            prompt_def = Prompt(prompt_id=1, service_id=1, company_id=7, prompt_name="Prompt Front", prompt_type="audio")
            crit = PromptCriterion(prompt_id=1, criterion_name="Identificación y saludo", is_active=True)
            db.add_all([company, service, prompt_def, crit])
            await db.flush()

            now = datetime.now(timezone.utc)
            report = TrainingAgentReport(
                training_report_id=201,
                company_id=7,
                service_id=1,
                hubspot_owner_id="demo_owner_01",
                agent_name="Agente Demo 01",
                agent_initials="AD",
                period_start=now,
                period_end=now,
                status="in_progress",
                is_current=True,
            )
            db.add(report)
            await db.flush()

            sim = TrainingSimulationPrompt(
                simulation_prompt_id=101,
                training_report_id=201,
                hubspot_owner_id="demo_owner_01",
                prompt_number=1,
                title="Duda de Facturación",
                scenario_type="Atención",
                prompt_text="Cliente molesto con factura.",
                objective_focus_json=["Calmar al cliente", "Explicar desglose"],
            )
            db.add(sim)
            await db.flush()

            comp = TrainingCompletionStatus(
                completion_id=301,
                training_report_id=201,
                simulation_prompt_id=101,
                hubspot_owner_id="demo_owner_01",
                status="pending",
            )
            session = TrainingCallSession(
                session_id=401,
                call_sid="CA_fake_401",
                cycle_id=201,
                conversation_id=101,
                agent_id="demo_owner_01",
                status="completed",
                recording_url="https://api.twilio.com/fake/recording.mp3",
            )
            db.add_all([comp, session])
            await db.commit()

        # Ejecutar evaluación
        await evaluate_training_session_task(401)

        # Verificar resultados en BD
        async with AsyncSession(self.engine) as db:
            res_sess = await db.execute(select(TrainingCallSession).where(TrainingCallSession.session_id == 401))
            sess = res_sess.scalars().first()
            self.assertEqual(sess.status, "evaluated")

            res_comp = await db.execute(select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 301))
            comp = res_comp.scalars().first()
            self.assertEqual(comp.status, "completed")
            self.assertIsNotNone(comp.completed_at)
            self.assertIsNotNone(comp.evaluation_id)

            res_ev = await db.execute(select(TrainingCallEvaluation).where(TrainingCallEvaluation.session_id == 401))
            ev = res_ev.scalars().first()
            self.assertIsNotNone(ev)
            self.assertEqual(float(ev.score), 8.0)
            self.assertEqual(ev.feedback, "El agente demostró gran solvencia en el tratamiento de la objeción.")

            result = ev.result_json
            self.assertTrue(result["is_valid_roleplay"])
            self.assertIn("objectives_met", result)
            self.assertIn("areas_for_improvement", result)
            self.assertIn("Identificación y saludo", result["result_json"])
            self.assertTrue(result["result_json"]["Identificación y saludo"])
            self.assertFalse(result["result_json"]["Cierre de llamada"])

    @patch("app.services.openai_service.analyze_audio_bytes")
    @patch("app.services.twilio_service.TwilioService.download_audio")
    async def test_evaluate_training_session_task_invalid_roleplay(self, mock_download, mock_analyze):
        """Verifica que si is_valid_roleplay es False la llamada se marque como failed y comp siga pending."""
        mock_download.return_value = b"fake_audio_bytes"
        mock_analyze.return_value = json.dumps({
            "is_valid_roleplay": False,
            "score": 2.0,
            "feedback": "La llamada se cortó inmediatamente tras la presentación.",
            "result_json": {
                "Identificación y saludo": True,
            },
            "transcription": "Agente: Buenos días.\n",
        })

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
            service = Service(service_id=1, company_id=7, service_name="Front Atención", service_key="front_atencion")
            db.add_all([company, service])
            await db.flush()

            now = datetime.now(timezone.utc)
            report = TrainingAgentReport(
                training_report_id=202,
                company_id=7,
                service_id=1,
                hubspot_owner_id="demo_owner_01",
                agent_name="Agente Demo 01",
                agent_initials="AD",
                period_start=now,
                period_end=now,
                status="in_progress",
            )
            db.add(report)
            await db.flush()

            sim = TrainingSimulationPrompt(simulation_prompt_id=102, training_report_id=202, hubspot_owner_id="demo_owner_01", prompt_number=2, title="Sim 2", scenario_type="test", prompt_text="test")
            comp = TrainingCompletionStatus(completion_id=302, training_report_id=202, simulation_prompt_id=102, hubspot_owner_id="demo_owner_01", status="pending")
            session = TrainingCallSession(
                session_id=402,
                call_sid="CA_fake_402",
                cycle_id=202,
                conversation_id=102,
                agent_id="demo_owner_01",
                status="completed",
                recording_url="https://api.twilio.com/fake/recording.mp3",
            )
            db.add_all([sim, comp, session])
            await db.commit()

        await evaluate_training_session_task(402)

        async with AsyncSession(self.engine) as db:
            res_sess = await db.execute(select(TrainingCallSession).where(TrainingCallSession.session_id == 402))
            sess = res_sess.scalars().first()
            self.assertEqual(sess.status, "failed")
            self.assertIn("juego de rol no fue válido", sess.error_message)

            res_comp = await db.execute(select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 302))
            comp = res_comp.scalars().first()
            self.assertEqual(comp.status, "pending")
            self.assertIsNone(comp.completed_at)
            self.assertIsNone(comp.evaluation_id)

    async def test_get_evaluation_detail_endpoint_returns_rich_data(self):
        """Verifica que el endpoint /admin/evaluations/{id} devuelva criterios, turnos y fortalezas/debilidades."""
        from app.routers.personalized_training import get_evaluation_detail
        from app.core.tenant_context import TenantContext

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
            service = Service(service_id=1, company_id=7, service_name="Front Atención", service_key="front_atencion")
            eval_p = TrainingEvaluationPrompt(id=1, service_id=1, prompt_text="prompt", version=1)
            db.add_all([company, service, eval_p])
            await db.flush()

            ev = TrainingCallEvaluation(
                evaluation_id=501,
                session_id=601,
                cycle_id=201,
                conversation_id=101,
                agent_id="demo_owner_01",
                prompt_version_id=1,
                score=Decimal("8.5"),
                feedback="Buen manejo general.",
                transcription="Agente: Buenos días.\nCliente: Hola, tengo una consulta.\nAgente: Claro, dígame.",
                result_json={
                    "is_valid_roleplay": True,
                    "score": 8.5,
                    "objectives_met": ["Saludo cordial", "Sondeo"],
                    "areas_for_improvement": ["Cierre"],
                    "result_json": {
                        "Identificación y saludo": True,
                        "Sondeo de necesidad": True,
                        "Cierre de llamada": False,
                    }
                }
            )
            db.add(ev)
            await db.commit()

            from app.core.roles import InternalRole
            context = TenantContext(
                user_id=1,
                raw_role="super_admin",
                normalized_role=InternalRole.SUPER_ADMIN,
                company_id=None,
                allowed_company_ids=[7],
                is_super_admin=True,
            )

            res = await get_evaluation_detail(evaluation_id=501, context=context, db=db)

            self.assertEqual(res["evaluation_id"], 501)
            self.assertEqual(res["score"], 8.5)
            self.assertEqual(res["feedback"], "Buen manejo general.")
            self.assertEqual(res["criteria"], {
                "Identificación y saludo": True,
                "Sondeo de necesidad": True,
                "Cierre de llamada": False,
            })
            self.assertIn("Saludo cordial", res["strengths"])
            self.assertIn("Cierre", res["weaknesses"])
            self.assertIn("Saludo cordial", res["objectives_met"])
            self.assertIn("Cierre", res["areas_for_improvement"])

            # Comprobar turnos
            turns = res["transcription_turns"]
            self.assertEqual(len(turns), 3)
            self.assertEqual(turns[0]["role"], "agent")
            self.assertEqual(turns[1]["role"], "patient")
            self.assertEqual(turns[2]["role"], "agent")


if __name__ == "__main__":
    unittest.main()

