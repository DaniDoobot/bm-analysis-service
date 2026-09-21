import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_evidence.db"

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


class TestTrainingEvaluationEvidence(unittest.IsolatedAsyncioTestCase):

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

    def test_evaluation_prompt_requires_structured_criteria_evidence(self):
        """1. Verifica que el prompt exija explicitamente criteria_evaluations con todos sus campos."""
        crit = PromptCriterion(
            criterion_id=1,
            criterion_name="Empatía y Escucha Activa",
            criterion_key="empatia",
            criterion_description="Valida emociones del paciente y no interrumpe.",
        )
        prompt = _build_personalized_training_evaluation_prompt(
            sim_prompt=None,
            agent_report=None,
            criteria_items=[crit],
            company_name="Clínica Dental Demo",
        )

        self.assertIn("criteria_evaluations", prompt)
        self.assertIn("criterion_key", prompt)
        self.assertIn("criterion_name", prompt)
        self.assertIn("score", prompt)
        self.assertIn("passed", prompt)
        self.assertIn("evidence_quote", prompt)
        self.assertIn("relevant_turns", prompt)
        self.assertIn("observed_behavior", prompt)
        self.assertIn("expected_behavior", prompt)
        self.assertIn("reasoning", prompt)
        self.assertIn("improvement_tip", prompt)
        self.assertIn("AGENTE HUMANO", prompt)
        self.assertIn("[Turno 1]", prompt)

    @patch("app.services.openai_service.analyze_audio_bytes")
    @patch("app.services.twilio_service.TwilioService.download_audio")
    async def test_structured_criteria_evaluations_are_persisted(self, mock_download, mock_analyze):
        """2. Simular respuesta con criteria_evaluations y verificar persistencia en result_json."""
        mock_download.return_value = b"dummy_audio"
        mock_analyze.return_value = json.dumps({
            "is_valid_roleplay": True,
            "score": 7.5,
            "feedback": "Buen manejo de la situación en general.",
            "objectives_met": ["Identificación correcta"],
            "areas_for_improvement": ["Mayor empatía en objeciones"],
            "result_json": {
                "Empatía": True,
            },
            "criteria_evaluations": [
                {
                    "criterion_key": "empatia",
                    "criterion_name": "Empatía",
                    "score": 7.0,
                    "passed": True,
                    "expected_behavior": "Validar la molestia del paciente antes de responder.",
                    "observed_behavior": "El agente tardó en validar la emoción del paciente pero resolvió su duda.",
                    "evidence_quote": "Entiendo su preocupación con los tiempos de espera.",
                    "relevant_turns": [3, 4],
                    "reasoning": "Respondió educadamente pero faltó calidez inicial.",
                    "improvement_tip": "Incorporar frases empáticas al inicio de la llamada.",
                }
            ],
            "transcription": "[Turno 1] Paciente: Llevo días esperando.\n[Turno 2] Agente: Entiendo su preocupación con los tiempos de espera.",
        })

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=1, company_name="Demo", company_key="demo", is_demo=True)
            service = Service(service_id=1, company_id=1, service_name="Front", service_key="front")
            prompt_def = Prompt(prompt_id=1, service_id=1, company_id=1, prompt_name="P1", prompt_type="audio")
            db.add_all([company, service, prompt_def])
            await db.flush()

            now = datetime.now(timezone.utc)
            report = TrainingAgentReport(
                training_report_id=10,
                company_id=1,
                service_id=1,
                hubspot_owner_id="agent_01",
                agent_name="Agente Uno",
                agent_initials="AO",
                period_start=now,
                period_end=now,
                status="in_progress",
            )
            sim = TrainingSimulationPrompt(
                simulation_prompt_id=20,
                training_report_id=10,
                hubspot_owner_id="agent_01",
                prompt_number=1,
                title="Simulación 1",
                scenario_type="Atención",
                prompt_text="Rol",
            )
            comp = TrainingCompletionStatus(
                completion_id=30,
                training_report_id=10,
                simulation_prompt_id=20,
                hubspot_owner_id="agent_01",
                status="pending",
            )
            session = TrainingCallSession(
                session_id=40,
                call_sid="CA_evidence_1",
                cycle_id=10,
                conversation_id=20,
                agent_id="agent_01",
                status="completed",
                recording_url="https://api.twilio.com/fake/rec.mp3",
            )
            db.add_all([report, sim, comp, session])
            await db.commit()

        await evaluate_training_session_task(40)

        async with AsyncSession(self.engine) as db:
            res_ev = await db.execute(select(TrainingCallEvaluation).where(TrainingCallEvaluation.session_id == 40))
            ev = res_ev.scalars().first()
            self.assertIsNotNone(ev)
            self.assertEqual(float(ev.score), 7.5)

            # Verificar que criteria_evaluations está presente y bien estructurado
            crit_evals = ev.result_json.get("criteria_evaluations")
            self.assertIsInstance(crit_evals, list)
            self.assertEqual(len(crit_evals), 1)

            entry = crit_evals[0]
            self.assertEqual(entry["criterion_key"], "empatia")
            self.assertEqual(entry["criterion_name"], "Empatía")
            self.assertEqual(entry["score"], 7.0)
            self.assertTrue(entry["passed"])
            self.assertEqual(entry["expected_behavior"], "Validar la molestia del paciente antes de responder.")
            self.assertEqual(entry["observed_behavior"], "El agente tardó en validar la emoción del paciente pero resolvió su duda.")
            self.assertEqual(entry["evidence_quote"], "Entiendo su preocupación con los tiempos de espera.")
            self.assertEqual(entry["relevant_turns"], [3, 4])
            self.assertEqual(entry["reasoning"], "Respondió educadamente pero faltó calidez inicial.")
            self.assertEqual(entry["improvement_tip"], "Incorporar frases empáticas al inicio de la llamada.")

    @patch("app.services.openai_service.analyze_audio_bytes")
    @patch("app.services.twilio_service.TwilioService.download_audio")
    async def test_legacy_result_json_is_preserved(self, mock_download, mock_analyze):
        """3. Verificar que el formato de booleanos legacy se conserve en result_json."""
        mock_download.return_value = b"dummy_audio"
        mock_analyze.return_value = json.dumps({
            "is_valid_roleplay": True,
            "score": 8.0,
            "feedback": "Buena atención.",
            "result_json": {
                "Identificación": True,
                "Cierre formal": False,
            },
            "criteria_evaluations": [
                {
                    "criterion_key": "identificacion",
                    "criterion_name": "Identificación",
                    "score": 9.0,
                    "passed": True,
                },
                {
                    "criterion_key": "cierre_formal",
                    "criterion_name": "Cierre formal",
                    "score": 5.0,
                    "passed": False,
                }
            ],
            "transcription": "Agente: Hola.\nPaciente: Adiós.",
        })

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=2, company_name="Demo2", company_key="demo2", is_demo=True)
            service = Service(service_id=2, company_id=2, service_name="Front2", service_key="front2")
            report = TrainingAgentReport(
                training_report_id=11, company_id=2, service_id=2, hubspot_owner_id="agent_02",
                agent_name="Agente Dos", agent_initials="AD",
                period_start=datetime.now(timezone.utc), period_end=datetime.now(timezone.utc),
                status="in_progress",
            )
            sim = TrainingSimulationPrompt(
                simulation_prompt_id=21, training_report_id=11, hubspot_owner_id="agent_02",
                prompt_number=1, title="Simulación 2", scenario_type="Atención", prompt_text="Rol",
            )
            comp = TrainingCompletionStatus(
                completion_id=31, training_report_id=11, simulation_prompt_id=21, hubspot_owner_id="agent_02",
                status="pending",
            )
            session = TrainingCallSession(
                session_id=41, call_sid="CA_evidence_2", cycle_id=11, conversation_id=21,
                agent_id="agent_02", status="completed", recording_url="https://api.twilio.com/fake/rec2.mp3",
            )
            db.add_all([company, service, report, sim, comp, session])
            await db.commit()

        await evaluate_training_session_task(41)

        async with AsyncSession(self.engine) as db:
            res_ev = await db.execute(select(TrainingCallEvaluation).where(TrainingCallEvaluation.session_id == 41))
            ev = res_ev.scalars().first()
            self.assertIsNotNone(ev)

            # Verificar que el checklist booleano legacy sigue existiendo y es fiel
            inner_legacy = ev.result_json.get("result_json")
            self.assertIsInstance(inner_legacy, dict)
            self.assertTrue(inner_legacy.get("Identificación"))
            self.assertFalse(inner_legacy.get("Cierre formal"))

    @patch("app.services.openai_service.analyze_audio_bytes")
    @patch("app.services.twilio_service.TwilioService.download_audio")
    async def test_multiple_criteria_are_persisted(self, mock_download, mock_analyze):
        """4. Simular al menos tres criterios y verificar que todos se persisten individualmente."""
        mock_download.return_value = b"dummy_audio"
        mock_analyze.return_value = json.dumps({
            "is_valid_roleplay": True,
            "score": 8.3,
            "feedback": "Evaluación completa multivariable.",
            "result_json": {
                "Saludo e Identificación": True,
                "Escucha Activa": True,
                "Gestión de Objeciones": False,
            },
            "criteria_evaluations": [
                {
                    "criterion_key": "saludo_identificacion",
                    "criterion_name": "Saludo e Identificación",
                    "score": 9.0,
                    "passed": True,
                    "evidence_quote": "Buenos días, le habla Carlos.",
                    "relevant_turns": [1],
                },
                {
                    "criterion_key": "escucha_activa",
                    "criterion_name": "Escucha Activa",
                    "score": 8.5,
                    "passed": True,
                    "evidence_quote": "Comprendo perfectamente su situación.",
                    "relevant_turns": [3],
                },
                {
                    "criterion_key": "gestion_objeciones",
                    "criterion_name": "Gestión de Objeciones",
                    "score": 5.5,
                    "passed": False,
                    "evidence_quote": "No puedo hacer nada con ese precio.",
                    "relevant_turns": [5, 6],
                },
            ],
            "transcription": "[Turno 1] Agente: Buenos días, le habla Carlos.\n[Turno 2] Paciente: El presupuesto es caro.\n[Turno 3] Agente: Comprendo perfectamente su situación.\n[Turno 5] Agente: No puedo hacer nada con ese precio.",
        })

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=3, company_name="Demo3", company_key="demo3", is_demo=True)
            service = Service(service_id=3, company_id=3, service_name="Front3", service_key="front3")
            report = TrainingAgentReport(
                training_report_id=12, company_id=3, service_id=3, hubspot_owner_id="agent_03",
                agent_name="Agente Tres", agent_initials="AT",
                period_start=datetime.now(timezone.utc), period_end=datetime.now(timezone.utc),
                status="in_progress",
            )
            sim = TrainingSimulationPrompt(
                simulation_prompt_id=22, training_report_id=12, hubspot_owner_id="agent_03",
                prompt_number=1, title="Simulación 3", scenario_type="Ventas", prompt_text="Rol",
            )
            comp = TrainingCompletionStatus(
                completion_id=32, training_report_id=12, simulation_prompt_id=22, hubspot_owner_id="agent_03",
                status="pending",
            )
            session = TrainingCallSession(
                session_id=42, call_sid="CA_evidence_3", cycle_id=12, conversation_id=22,
                agent_id="agent_03", status="completed", recording_url="https://api.twilio.com/fake/rec3.mp3",
            )
            db.add_all([company, service, report, sim, comp, session])
            await db.commit()

        await evaluate_training_session_task(42)

        async with AsyncSession(self.engine) as db:
            res_ev = await db.execute(select(TrainingCallEvaluation).where(TrainingCallEvaluation.session_id == 42))
            ev = res_ev.scalars().first()
            crit_evals = ev.result_json.get("criteria_evaluations")
            self.assertEqual(len(crit_evals), 3)
            keys = [c["criterion_key"] for c in crit_evals]
            self.assertIn("saludo_identificacion", keys)
            self.assertIn("escucha_activa", keys)
            self.assertIn("gestion_objeciones", keys)

    @patch("app.services.openai_service.analyze_audio_bytes")
    @patch("app.services.twilio_service.TwilioService.download_audio")
    async def test_invalid_or_incomplete_criterion_does_not_break_evaluation(self, mock_download, mock_analyze):
        """5. Criterio con score no numérico, turns corruptos o estructura incompleta no rompe la evaluación."""
        mock_download.return_value = b"dummy_audio"
        mock_analyze.return_value = json.dumps({
            "is_valid_roleplay": True,
            "score": 6.0,
            "feedback": "Feedback básico.",
            "criteria_evaluations": [
                {
                    # Criterio malformado: score es string inválido, turns son texto, sin passed
                    "criterion_name": "Criterio Problemático",
                    "score": "invalido_no_numero",
                    "relevant_turns": ["no_entero", 4, None],
                },
                "esto no es un dict sino un string",
            ],
            "result_json": {
                "Criterio Problemático": False,
            },
            "transcription": "Agente: Hola.",
        })

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=4, company_name="Demo4", company_key="demo4", is_demo=True)
            service = Service(service_id=4, company_id=4, service_name="Front4", service_key="front4")
            report = TrainingAgentReport(
                training_report_id=13, company_id=4, service_id=4, hubspot_owner_id="agent_04",
                agent_name="Agente Cuatro", agent_initials="AC",
                period_start=datetime.now(timezone.utc), period_end=datetime.now(timezone.utc),
                status="in_progress",
            )
            sim = TrainingSimulationPrompt(
                simulation_prompt_id=23, training_report_id=13, hubspot_owner_id="agent_04",
                prompt_number=1, title="Simulación 4", scenario_type="Atención", prompt_text="Rol",
            )
            comp = TrainingCompletionStatus(
                completion_id=33, training_report_id=13, simulation_prompt_id=23, hubspot_owner_id="agent_04",
                status="pending",
            )
            session = TrainingCallSession(
                session_id=43, call_sid="CA_evidence_4", cycle_id=13, conversation_id=23,
                agent_id="agent_04", status="completed", recording_url="https://api.twilio.com/fake/rec4.mp3",
            )
            db.add_all([company, service, report, sim, comp, session])
            await db.commit()

        # Debe completar sin lanzar excepción
        await evaluate_training_session_task(43)

        async with AsyncSession(self.engine) as db:
            res_ev = await db.execute(select(TrainingCallEvaluation).where(TrainingCallEvaluation.session_id == 43))
            ev = res_ev.scalars().first()
            self.assertIsNotNone(ev)
            crit_evals = ev.result_json.get("criteria_evaluations")
            self.assertIsInstance(crit_evals, list)
            # El item corrupto se normalizó defensivamente y el string no-dict se ignoró sin romper
            self.assertEqual(len(crit_evals), 1)
            item = crit_evals[0]
            self.assertEqual(item["criterion_name"], "Criterio Problemático")
            self.assertIsNone(item["score"])
            self.assertIsInstance(item["passed"], bool)
            self.assertEqual(item["relevant_turns"], [4])

    def test_historical_evaluation_without_criteria_evaluations_remains_compatible(self):
        """6. Evaluaciones históricas sin criteria_evaluations son leídas limpiamente por _map_completion_to_dict."""
        historical_eval = type("MockHistoricalEval", (), {
            "score": Decimal("7.2"),
            "feedback": "Evaluación histórica de hace meses.",
            "transcription": "Agente: Hola.\nCliente: Buenas.",
            "result_json": {
                "is_valid_roleplay": True,
                "score": 7.2,
                "result_json": {
                    "Saludo inicial": True,
                    "Empatía": False,
                }
            }
        })()

        mock_comp = type("MockComp", (), {
            "completion_id": 99,
            "training_report_id": 999,
            "simulation_prompt_id": 990,
            "hubspot_owner_id": "agent_hist",
            "status": "completed",
            "completed_at": datetime.now(timezone.utc),
            "evaluation_id": 88,
            "call_session_id": 77,
            "training_call_id": None,
            "training_phone_number": None,
            "notes": None,
            "prompt": None,
            "evaluation": historical_eval,
            "created_at": datetime.now(timezone.utc),
        })()

        slot = PersonalizedTrainingService._map_completion_to_dict(mock_comp)
        self.assertIsNotNone(slot)
        self.assertEqual(slot["score"], 7.2)
        self.assertEqual(slot["criteria"], {"Saludo inicial": True, "Empatía": False})
        self.assertIsNone(slot.get("criteria_evaluations"))
        self.assertEqual(len(slot["transcription_turns"]), 2)

    @patch("app.services.openai_service.analyze_audio_bytes")
    @patch("app.services.twilio_service.TwilioService.download_audio")
    async def test_no_second_llm_call_for_evidence(self, mock_download, mock_analyze):
        """7. Verifica que el flujo realice UNA SOLA llamada LLM para evaluar y extraer evidencias."""
        mock_download.return_value = b"dummy_audio"
        mock_analyze.return_value = json.dumps({
            "is_valid_roleplay": True,
            "score": 8.0,
            "feedback": "Bien.",
            "result_json": {"Criterio 1": True},
            "criteria_evaluations": [{
                "criterion_key": "criterio_1",
                "criterion_name": "Criterio 1",
                "score": 8.0,
                "passed": True,
                "evidence_quote": "Cita",
                "relevant_turns": [1],
                "reasoning": "Razón",
                "improvement_tip": "Tip",
            }],
            "transcription": "Agente: Hola.",
        })

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=5, company_name="Demo5", company_key="demo5", is_demo=True)
            service = Service(service_id=5, company_id=5, service_name="Front5", service_key="front5")
            report = TrainingAgentReport(
                training_report_id=15, company_id=5, service_id=5, hubspot_owner_id="agent_05",
                agent_name="Agente Cinco", agent_initials="AC",
                period_start=datetime.now(timezone.utc), period_end=datetime.now(timezone.utc),
                status="in_progress",
            )
            sim = TrainingSimulationPrompt(
                simulation_prompt_id=25, training_report_id=15, hubspot_owner_id="agent_05",
                prompt_number=1, title="Simulación 5", scenario_type="Atención", prompt_text="Rol",
            )
            comp = TrainingCompletionStatus(
                completion_id=35, training_report_id=15, simulation_prompt_id=25, hubspot_owner_id="agent_05",
                status="pending",
            )
            session = TrainingCallSession(
                session_id=45, call_sid="CA_evidence_5", cycle_id=15, conversation_id=25,
                agent_id="agent_05", status="completed", recording_url="https://api.twilio.com/fake/rec5.mp3",
            )
            db.add_all([company, service, report, sim, comp, session])
            await db.commit()

        with patch("app.services.openai_service.complete_text") as mock_complete_text:
            await evaluate_training_session_task(45)
            # Solo debe haberse llamado analyze_audio_bytes (exactamente 1 vez)
            self.assertEqual(mock_analyze.call_count, 1)
            # No debe haberse llamado complete_text para una segunda evaluación/justificación
            mock_complete_text.assert_not_called()

    @patch("app.services.openai_service.analyze_audio_bytes")
    @patch("app.services.twilio_service.TwilioService.download_audio")
    async def test_missing_criteria_evaluations_does_not_fabricate_evidence(self, mock_download, mock_analyze):
        """8. Respuesta sin criteria_evaluations NO fabrica evidencia, citas, turnos ni razonamientos inventados."""
        mock_download.return_value = b"dummy_audio"
        # Gemini devuelve solo campos estándar y NO devuelve criteria_evaluations
        mock_analyze.return_value = json.dumps({
            "is_valid_roleplay": True,
            "score": 8.5,
            "feedback": "Llamada fluida con buen ritmo.",
            "objectives_met": ["Saludo cordial", "Escucha activa"],
            "areas_for_improvement": ["Cierre"],
            "result_json": {
                "Saludo inicial": True,
                "Empatía": True,
                "Cierre formal": False,
            },
            "transcription": "[Turno 1] Paciente: Hola.\n[Turno 2] Agente: Hola, buenos días.",
        })

        async with AsyncSession(self.engine) as db:
            company = Company(company_id=6, company_name="Demo6", company_key="demo6", is_demo=True)
            service = Service(service_id=6, company_id=6, service_name="Front6", service_key="front6")
            report = TrainingAgentReport(
                training_report_id=16, company_id=6, service_id=6, hubspot_owner_id="agent_06",
                agent_name="Agente Seis", agent_initials="AS",
                period_start=datetime.now(timezone.utc), period_end=datetime.now(timezone.utc),
                status="in_progress",
            )
            sim = TrainingSimulationPrompt(
                simulation_prompt_id=26, training_report_id=16, hubspot_owner_id="agent_06",
                prompt_number=1, title="Simulación 6", scenario_type="Atención", prompt_text="Rol",
            )
            comp = TrainingCompletionStatus(
                completion_id=36, training_report_id=16, simulation_prompt_id=26, hubspot_owner_id="agent_06",
                status="pending",
            )
            session = TrainingCallSession(
                session_id=46, call_sid="CA_evidence_6", cycle_id=16, conversation_id=26,
                agent_id="agent_06", status="completed", recording_url="https://api.twilio.com/fake/rec6.mp3",
            )
            db.add_all([company, service, report, sim, comp, session])
            await db.commit()

        # Ejecutar evaluación
        await evaluate_training_session_task(46)

        async with AsyncSession(self.engine) as db:
            res_ev = await db.execute(select(TrainingCallEvaluation).where(TrainingCallEvaluation.session_id == 46))
            ev = res_ev.scalars().first()
            self.assertIsNotNone(ev)
            self.assertEqual(float(ev.score), 8.5)

            # 1. Conserva los booleanos legacy
            inner_legacy = ev.result_json.get("result_json")
            self.assertIsInstance(inner_legacy, dict)
            self.assertTrue(inner_legacy.get("Saludo inicial"))
            self.assertTrue(inner_legacy.get("Empatía"))
            self.assertFalse(inner_legacy.get("Cierre formal"))

            # 2. criteria_evaluations NO contiene elementos fabricados
            crit_evals = ev.result_json.get("criteria_evaluations")
            self.assertEqual(crit_evals, [])

            # 3. No aparecen citas, razonamientos ni turnos inventados
            for item in crit_evals:
                self.fail("No debe existir ningún elemento fabricado en criteria_evaluations")

            # 4. Verificar que la sesión se evaluó con éxito sin fallar
            res_sess = await db.execute(select(TrainingCallSession).where(TrainingCallSession.session_id == 46))
            sess = res_sess.scalars().first()
            self.assertEqual(sess.status, "evaluated")


if __name__ == "__main__":
    unittest.main()
