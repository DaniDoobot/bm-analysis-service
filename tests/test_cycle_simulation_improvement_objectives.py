"""
Tests for cycle simulation improvement objectives resolution, individual feedback,
status mapping (Superado / No superado / No evaluable), evidence quotes,
and strict role-based visibility (exposed for Admin/Coordinator/Manager, hidden for Agent).
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_cycle_objs.db"
os.environ["GEMINI_API_KEY"] = "mock_key"

from sqlalchemy import text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from httpx import AsyncClient, ASGITransport
from app.db import get_engine, Base, AsyncSessionLocal
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCallSession,
    TrainingCallEvaluation,
    TrainingCompletionStatus,
    TrainingAgentSetting,
)
from app.services.personalized_training_service import PersonalizedTrainingService
from app.routers.personalized_training import sanitize_report_for_agent
from app.dependencies import get_current_user
from app.main import app


class TestCycleSimulationImprovementObjectives(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("test_cycle_objs.db"):
            try:
                os.remove("test_cycle_objs.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        now = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as db:
            c1 = Company(company_id=1, company_name="Empresa Test", company_key="empresa_test", is_active=True)
            db.add(c1)
            await db.commit()

            s1 = Service(service_id=10, company_id=1, service_name="Atención al Cliente", service_key="atencion_cliente", is_active=True)
            db.add(s1)
            await db.commit()

            # Users
            self.user_superadmin = User(user_id=1, username="super", company_id=1, email="super@test.com", password_hash="hash", role="superadmin", is_active=True)
            self.user_admin = User(user_id=2, username="admin", company_id=1, email="admin@test.com", password_hash="hash", role="company_admin", is_active=True)
            self.user_coord = User(user_id=3, username="coord", company_id=1, email="coord@test.com", password_hash="hash", role="team_coordinator", is_active=True)
            self.user_agent = User(user_id=4, username="agent", company_id=1, email="agent@test.com", password_hash="hash", role="agent", hubspot_owner_id="agt_100", is_active=True)

            db.add_all([self.user_superadmin, self.user_admin, self.user_coord, self.user_agent])
            await db.commit()

            # Team setup
            t1 = Team(team_id=100, company_id=1, service_id=10, team_name="Equipo Alfa", is_active=True)
            db.add(t1)
            await db.commit()

            db.add(UserTeamAssociation(user_id=self.user_coord.user_id, team_id=100))
            db.add(AgentTeamAssociation(user_id=self.user_agent.user_id, team_id=100))
            await db.commit()

            # Agent setting
            db.add(TrainingAgentSetting(
                company_id=1,
                hubspot_owner_id="agt_100",
                agent_name="Laura Gómez",
                agent_initials="LG",
                is_enabled=True,
            ))
            await db.commit()

            # Report with General and Specific objectives
            rep = TrainingAgentReport(
                training_report_id=50,
                company_id=1,
                service_id=10,
                hubspot_owner_id="agt_100",
                agent_name="Laura Gómez",
                agent_initials="LG",
                period_start=now - timedelta(days=7),
                period_end=now,
                status="in_progress",
                is_current=True,
                evaluations_count=2,
                calls_count=2,
                general_objectives_json=[
                    {
                        "title": "Escucha Activa y Diagnóstico de Necesidades",
                        "description": "Aplicar escucha activa sin interrumpir.",
                        "expected_behavior": "Esperar 30 segundos y parafrasear.",
                        "success_indicators": ["Cero interrupciones."]
                    },
                    {
                        "title": "Claridad y Estructura de la Comunicación",
                        "description": "Explicar los pasos en orden lógico.",
                        "expected_behavior": "Estructurar la respuesta en partes claras.",
                        "success_indicators": ["Puntuación mínima de 8."]
                    }
                ],
                specific_objectives_json=[
                    {
                        "title": "Manejo de Objeciones a la Solución Propuesta",
                        "description": "Responder con argumentos sólidos y empáticos.",
                        "related_criteria": ["manejo_objeciones", "argumentacion"],
                        "specific_behavior_to_improve": "Validar la objeción antes de rebatirla.",
                        "success_indicators": ["Resolución en primera instancia."]
                    },
                    {
                        "title": "Cierre Efectivo y Confirmación de Siguientes Pasos",
                        "description": "Confirmar qué sucede a continuación.",
                        "related_criteria": ["cierre_satisfaccion", "siguientes_pasos"],
                        "specific_behavior_to_improve": "Preguntar si quedan dudas antes de despedirse.",
                        "success_indicators": ["Resumen de cierre presente."]
                    }
                ]
            )
            db.add(rep)
            await db.commit()

            # Simulation Prompt 1: Has linked objectives
            p1 = TrainingSimulationPrompt(
                simulation_prompt_id=201,
                training_report_id=50,
                hubspot_owner_id="agt_100",
                prompt_number=1,
                title="Simulación 1: Consulta Compleja con Cliente Insatisfecho",
                scenario_type="roleplay",
                objective_focus_json={
                    "focus": ["Escucha Activa", "Manejo de Objeciones a la Solución Propuesta"],
                    "linked_general_objectives": ["Escucha Activa y Diagnóstico de Necesidades"],
                    "linked_specific_objectives": ["Manejo de Objeciones a la Solución Propuesta"],
                    "objective_summary": "Atender con escucha activa y gestionar objeciones complejas.",
                    "expected_behavior": "Validar la objeción y aplicar desescalada.",
                },
                prompt_text="Secret agent prompt instructions",
            )

            # Simulation Prompt 2: Pending simulation with focus
            p2 = TrainingSimulationPrompt(
                simulation_prompt_id=202,
                training_report_id=50,
                hubspot_owner_id="agt_100",
                prompt_number=2,
                title="Simulación 2: Cierre y Confirmación",
                scenario_type="roleplay",
                objective_focus_json={
                    "focus": ["Cierre Efectivo y Confirmación de Siguientes Pasos"],
                    "linked_general_objectives": ["Claridad y Estructura de la Comunicación"],
                    "linked_specific_objectives": ["Cierre Efectivo y Confirmación de Siguientes Pasos"],
                    "objective_summary": "Estructurar la llamada y cerrar con claridad.",
                    "expected_behavior": "Confirmar datos y no dejar cabos sueltos.",
                },
                prompt_text="Secret prompt 2 instructions",
            )

            # Simulation Prompt 3: Historical simulation without objectives (empty objective_focus_json)
            p3 = TrainingSimulationPrompt(
                simulation_prompt_id=203,
                training_report_id=50,
                hubspot_owner_id="agt_100",
                prompt_number=3,
                title="Simulación Histórica 3",
                scenario_type="roleplay",
                objective_focus_json=None,
                prompt_text="Old prompt without focus",
            )

            db.add_all([p1, p2, p3])
            await db.commit()

            # Call Session for Sim 1
            sess1 = TrainingCallSession(
                session_id=501,
                cycle_id=50,
                conversation_id=201,
                agent_id="agt_100",
                call_sid="CA_SIM_1",
                recording_url="https://audio.provider.com/call1.mp3",
                status="completed",
            )
            db.add(sess1)
            await db.commit()

            # Evaluation for Sim 1 with detailed criteria_evaluations:
            # - Escucha Activa passed with 8.5
            # - Manejo de Objeciones failed with 5.0
            ev1 = TrainingCallEvaluation(
                evaluation_id=601,
                session_id=501,
                cycle_id=50,
                conversation_id=201,
                agent_id="agt_100",
                prompt_version_id=1,
                transcription="Agente: Buenos días.\nCliente: Tengo una queja.\nAgente: Entiendo perfectamente su malestar, cuénteme.",
                score=Decimal("6.75"),
                feedback="La escucha activa fue muy buena, pero falló en el manejo de objeciones.",
                result_json={
                    "score": 6.75,
                    "feedback": "La escucha activa fue muy buena, pero falló en el manejo de objeciones.",
                    "criteria_evaluations": [
                        {
                            "criterion_key": "escucha_activa",
                            "criterion_name": "Escucha Activa y Diagnóstico de Necesidades",
                            "passed": True,
                            "score": 8.5,
                            "reasoning": "El agente esperó a que el cliente terminara de exponer su situación sin interrumpir.",
                            "evidence_quote": "Entiendo perfectamente su malestar, cuénteme.",
                            "improvement_tip": "Mantener esta empatía inicial.",
                            "expected_behavior": "Esperar 30 segundos y parafrasear.",
                            "observed_behavior": "No interrumpió y validó de inmediato."
                        },
                        {
                            "criterion_key": "manejo_objeciones",
                            "criterion_name": "Manejo de Objeciones a la Solución Propuesta",
                            "passed": False,
                            "score": 5.0,
                            "reasoning": "El agente ofreció un descuento anticipado en lugar de rebatir con argumentos de valor.",
                            "evidence_quote": "Le aplico un 20% de descuento directo.",
                            "improvement_tip": "Defender el valor de la propuesta antes de recurrir al descuento.",
                            "expected_behavior": "Validar la objeción antes de rebatirla.",
                            "observed_behavior": "Cedió inmediatamente sin argumentar."
                        }
                    ],
                    "result_json": {
                        "escucha_activa": True,
                        "manejo_objeciones": False,
                    },
                    "objectives_met": ["Escucha activa sin interrupciones"],
                    "areas_for_improvement": ["Manejo de objeciones de precio"]
                }
            )
            db.add(ev1)
            await db.commit()

            # Completion status for Sim 1
            comp1 = TrainingCompletionStatus(
                completion_id=701,
                training_report_id=50,
                simulation_prompt_id=201,
                hubspot_owner_id="agt_100",
                status="completed",
                evaluation_id=601,
                call_session_id=501,
            )
            db.add(comp1)
            await db.commit()

    async def asyncTearDown(self):
        engine = get_engine()
        await engine.dispose()
        if os.path.exists("test_cycle_objs.db"):
            try:
                os.remove("test_cycle_objs.db")
            except Exception:
                pass

    async def test_resolve_simulation_improvement_objectives_with_criteria_evaluations(self):
        """Verify detailed evaluation matching against criteria_evaluations (passed, failed, quotes, feedback)."""
        async with AsyncSessionLocal() as db:
            rep = await db.get(TrainingAgentReport, 50)
            p1 = await db.get(TrainingSimulationPrompt, 201)
            ev1 = await db.get(TrainingCallEvaluation, 601)

            objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
                prompt=p1,
                report=rep,
                evaluation=ev1,
            )

            self.assertIsInstance(objectives, list)
            self.assertEqual(len(objectives), 2)

            # Check Escucha Activa objective
            escucha_obj = next((o for o in objectives if "escucha" in o["name"].lower()), None)
            self.assertIsNotNone(escucha_obj)
            self.assertEqual(escucha_obj["status"], "passed")
            self.assertEqual(escucha_obj["status_label"], "Superado")
            self.assertEqual(escucha_obj["score"], 8.5)
            self.assertIn("sin interrumpir", escucha_obj["feedback"].lower())
            self.assertEqual(escucha_obj["evidence"], "Entiendo perfectamente su malestar, cuénteme.")
            self.assertEqual(escucha_obj["expected_behavior"], "Esperar 30 segundos y parafrasear.")
            self.assertEqual(escucha_obj["observed_behavior"], "No interrumpió y validó de inmediato.")

            # Check Manejo de Objeciones objective
            objeciones_obj = next((o for o in objectives if "objeciones" in o["name"].lower()), None)
            self.assertIsNotNone(objeciones_obj)
            self.assertEqual(objeciones_obj["status"], "failed")
            self.assertEqual(objeciones_obj["status_label"], "No superado")
            self.assertEqual(objeciones_obj["score"], 5.0)
            self.assertIn("descuento", objeciones_obj["feedback"].lower())
            self.assertEqual(objeciones_obj["evidence"], "Le aplico un 20% de descuento directo.")
            self.assertEqual(objeciones_obj["expected_behavior"], "Validar la objeción antes de rebatirla.")
            self.assertEqual(objeciones_obj["observed_behavior"], "Cedió inmediatamente sin argumentar.")

    async def test_resolve_simulation_improvement_objectives_boolean_fallback(self):
        """Verify fallback when only boolean result_json is present (no criteria_evaluations)."""
        async with AsyncSessionLocal() as db:
            rep = await db.get(TrainingAgentReport, 50)
            p1 = await db.get(TrainingSimulationPrompt, 201)

            # Evaluation with only boolean result_json
            simple_eval = {
                "score": 7.5,
                "feedback": "Llamada correcta general.",
                "result_json": {
                    "escucha_activa": True,
                    "manejo_objeciones": False,
                }
            }

            objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
                prompt=p1,
                report=rep,
                evaluation=simple_eval,
            )

            self.assertEqual(len(objectives), 2)
            escucha_obj = next((o for o in objectives if "escucha" in o["name"].lower()), None)
            self.assertIsNotNone(escucha_obj)
            self.assertEqual(escucha_obj["status"], "passed")
            self.assertEqual(escucha_obj["status_label"], "Superado")
            self.assertEqual(escucha_obj["score"], 7.5)

            objeciones_obj = next((o for o in objectives if "objeciones" in o["name"].lower()), None)
            self.assertIsNotNone(objeciones_obj)
            self.assertEqual(objeciones_obj["status"], "failed")
            self.assertEqual(objeciones_obj["status_label"], "No superado")
            self.assertEqual(objeciones_obj["score"], 6.0)  # Capped at 6.0 for failed

    async def test_resolve_simulation_improvement_objectives_not_evaluable(self):
        """Verify objectives with no evidence/matching criteria are marked not_evaluable."""
        async with AsyncSessionLocal() as db:
            rep = await db.get(TrainingAgentReport, 50)
            p1 = await db.get(TrainingSimulationPrompt, 201)

            # Evaluation with unrelated criteria
            unrelated_eval = {
                "score": 7.0,
                "feedback": "Llamada sobre otros temas.",
                "result_json": {
                    "criteria_evaluations": [
                        {
                            "criterion_key": "protocolo_saludo",
                            "criterion_name": "Protocolo de Bienvenida",
                            "passed": True,
                            "score": 8.0,
                            "reasoning": "Buen saludo inicial."
                        }
                    ]
                }
            }

            objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
                prompt=p1,
                report=rep,
                evaluation=unrelated_eval,
            )

            for obj in objectives:
                self.assertEqual(obj["status"], "not_evaluable")
                self.assertEqual(obj["status_label"], "No evaluable")
                self.assertIsNone(obj["score"])
                self.assertIn("No se observó suficiente evidencia", obj["feedback"])

    async def test_pending_simulation_objectives(self):
        """Verify pending simulation slots show objectives with 'not_evaluable' and clear pending feedback."""
        async with AsyncSessionLocal() as db:
            rep = await db.get(TrainingAgentReport, 50)
            p2 = await db.get(TrainingSimulationPrompt, 202)

            objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
                prompt=p2,
                report=rep,
                evaluation=None,
            )

            self.assertTrue(len(objectives) >= 1)
            for obj in objectives:
                self.assertEqual(obj["status"], "not_evaluable")
                self.assertEqual(obj["status_label"], "No evaluable")
                self.assertIsNone(obj["score"])
                self.assertEqual(obj["feedback"], "Simulación pendiente de realización.")
                self.assertIsNotNone(obj["expected_behavior"])

    async def test_historical_simulation_without_objectives_returns_empty_list(self):
        """Verify historical simulation without objective metadata returns empty list (no hallucinated objectives)."""
        async with AsyncSessionLocal() as db:
            rep = await db.get(TrainingAgentReport, 50)
            p3 = await db.get(TrainingSimulationPrompt, 203)

            objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
                prompt=p3,
                report=rep,
                evaluation=None,
            )
            self.assertEqual(objectives, [])

    async def test_agent_view_sanitization_removes_improvement_objectives(self):
        """Verify sanitize_report_for_agent sets improvement_objectives to None on all simulations and completions."""
        report_dict = {
            "training_report_id": 50,
            "prompts": [{"simulation_prompt_id": 201, "prompt_text": "Sensitive"}],
            "simulations": [
                {
                    "simulation_prompt_id": 201,
                    "prompt_text": "Sensitive instructions",
                    "improvement_objectives": [
                        {"name": "Escucha Activa", "status": "passed"}
                    ]
                }
            ],
            "completion_statuses": [
                {
                    "completion_id": 701,
                    "improvement_objectives": [
                        {"name": "Escucha Activa", "status": "passed"}
                    ]
                }
            ]
        }

        sanitized = sanitize_report_for_agent(report_dict)

        # Sensitive prompt text stripped
        self.assertEqual(sanitized["prompts"][0]["prompt_text"], "")
        self.assertEqual(sanitized["simulations"][0]["prompt_text"], "")

        # Improvement objectives strictly hidden (None)
        self.assertIsNone(sanitized["simulations"][0]["improvement_objectives"])
        self.assertIsNone(sanitized["completion_statuses"][0]["improvement_objectives"])

    async def test_admin_and_agent_endpoints_role_visibility(self):
        """
        Verify API endpoint behavior:
        - Admin accessing /bm/training/admin/agents/{id} sees improvement_objectives populated.
        - Admin accessing /bm/training/evaluations/{id} sees improvement_objectives populated.
        - Agent accessing /bm/training/me/current has improvement_objectives as None.
        - Agent accessing /bm/training/evaluations/{id} has improvement_objectives as None.
        """
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Admin accessing agent detail
            app.dependency_overrides[get_current_user] = lambda: self.user_admin
            r_admin = await ac.get("/bm/training/admin/agents/agt_100")
            self.assertEqual(r_admin.status_code, 200, r_admin.text)
            admin_data = r_admin.json()
            curr_rep = admin_data["current_report"]
            self.assertIsNotNone(curr_rep)
            sims = curr_rep["simulations"]
            self.assertTrue(len(sims) >= 1)
            # Sim 1 is completed and should have populated objectives
            sim1 = next((s for s in sims if s["simulation_prompt_id"] == 201), None)
            self.assertIsNotNone(sim1)
            self.assertIsNotNone(sim1["improvement_objectives"])
            self.assertEqual(len(sim1["improvement_objectives"]), 2)
            passed_sim_obj = next((o for o in sim1["improvement_objectives"] if o["status"] == "passed"), None)
            self.assertIsNotNone(passed_sim_obj)
            self.assertEqual(passed_sim_obj["status_label"], "Superado")
            failed_sim_obj = next((o for o in sim1["improvement_objectives"] if o["status"] == "failed"), None)
            self.assertIsNotNone(failed_sim_obj)
            self.assertEqual(failed_sim_obj["status_label"], "No superado")

            # 2. Admin accessing evaluation detail
            r_ev_admin = await ac.get("/bm/training/evaluations/601")
            self.assertEqual(r_ev_admin.status_code, 200, r_ev_admin.text)
            ev_admin_data = r_ev_admin.json()
            self.assertIsNotNone(ev_admin_data.get("improvement_objectives"))
            self.assertEqual(len(ev_admin_data["improvement_objectives"]), 2)
            passed_ev_obj = next((o for o in ev_admin_data["improvement_objectives"] if o["status"] == "passed"), None)
            self.assertIsNotNone(passed_ev_obj)
            self.assertEqual(passed_ev_obj["status_label"], "Superado")

            # 3. Agent accessing /me/current -> improvement_objectives must be None
            app.dependency_overrides[get_current_user] = lambda: self.user_agent
            r_agent = await ac.get("/bm/training/me/current")
            self.assertEqual(r_agent.status_code, 200, r_agent.text)
            agent_data = r_agent.json()
            for s in agent_data.get("simulations", []):
                self.assertIsNone(s.get("improvement_objectives"))
            for c in agent_data.get("completion_statuses", []):
                self.assertIsNone(c.get("improvement_objectives"))

            # 4. Agent accessing /evaluations/601 -> improvement_objectives must be None
            r_ev_agent = await ac.get("/bm/training/evaluations/601")
            self.assertEqual(r_ev_agent.status_code, 200, r_ev_agent.text)
            ev_agent_data = r_ev_agent.json()
            self.assertIsNone(ev_agent_data.get("improvement_objectives"))

            app.dependency_overrides.clear()

    async def test_calibracion_protocolario_trato_de_usted_mapping(self):
        """
        Section 12: Reproduce exact conceptual case:
        Objective: 'Calibración del registro protocolario de usted', related_criteria: ['Trato de Usted']
        Criterion: criterion_key: 'trato_de_usted', criterion_name: 'Trato de Usted', passed: false, score: 4.0
        Expected: status = 'failed', status_label = 'No superado', score = 4.0. NEVER not_evaluable.
        """
        prompt = {
            "title": "Simulación de acogida",
            "objective_focus_json": [
                {
                    "title": "Calibración del registro protocolario de usted",
                    "description": "Adoptar el tratamiento de usted con el paciente.",
                    "related_criteria": ["Trato de Usted"],
                    "expected_behavior": "Tratar de usted de forma consistente."
                }
            ]
        }
        evaluation = {
            "score": 4.0,
            "feedback": "El agente no aplicó el trato protocolario de usted.",
            "result_json": {
                "criteria_evaluations": [
                    {
                        "criterion_key": "trato_de_usted",
                        "criterion_name": "Trato de Usted",
                        "passed": False,
                        "score": 4.0,
                        "reasoning": "El agente tuteó al paciente en varios turnos.",
                        "improvement_tip": "Emplear 'usted' desde el primer saludo.",
                        "evidence_quote": "Encantado, Carlos, dime en qué te ayudo."
                    }
                ]
            }
        }

        objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
            prompt=prompt,
            report=None,
            evaluation=evaluation,
        )

        self.assertEqual(len(objectives), 1)
        obj = objectives[0]
        self.assertEqual(obj["name"], "Calibración del registro protocolario de usted")
        self.assertEqual(obj["status"], "failed")
        self.assertEqual(obj["status_label"], "No superado")
        self.assertEqual(obj["score"], 4.0)
        self.assertIn("usted", obj["feedback"].lower())
        self.assertEqual(obj["evidence"], "Encantado, Carlos, dime en qué te ayudo.")
        self.assertNotEqual(obj["status"], "not_evaluable")

    async def test_trato_de_usted_passed_true(self):
        """Verify objective is 'passed' and 'Superado' when criterion passes."""
        prompt = {
            "title": "Simulación formal",
            "objective_focus_json": [
                {
                    "name": "Calibración del registro protocolario de usted",
                    "related_criteria": ["Trato de Usted"]
                }
            ]
        }
        evaluation = {
            "score": 9.0,
            "result_json": {
                "criteria_evaluations": [
                    {
                        "criterion_key": "trato_de_usted",
                        "criterion_name": "Trato de usted",
                        "passed": True,
                        "score": 9.0,
                        "reasoning": "Excelente uso del tratamiento formal de usted.",
                        "evidence_quote": "Buenas tardes, señor Carlos, ¿en qué podemos ayudarle?"
                    }
                ]
            }
        }

        objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
            prompt=prompt,
            report=None,
            evaluation=evaluation,
        )

        self.assertEqual(len(objectives), 1)
        obj = objectives[0]
        self.assertEqual(obj["status"], "passed")
        self.assertEqual(obj["status_label"], "Superado")
        self.assertEqual(obj["score"], 9.0)
        self.assertIn("excelente", obj["feedback"].lower())

    async def test_multiple_related_criteria(self):
        """Verify multiple related criteria calculate average score and combined status."""
        prompt = {
            "title": "Simulación combinada",
            "objective_focus_json": [
                {
                    "title": "Registro protocolario y empatía inicial",
                    "related_criteria": ["Trato de Usted", "Empatía"]
                }
            ]
        }
        evaluation = {
            "score": 6.0,
            "result_json": {
                "criteria_evaluations": [
                    {
                        "criterion_key": "trato_de_usted",
                        "criterion_name": "Trato de Usted",
                        "passed": False,
                        "score": 4.0,
                        "reasoning": "El agente tuteó.",
                        "improvement_tip": "Adoptar usted."
                    },
                    {
                        "criterion_key": "empatia",
                        "criterion_name": "Empatía",
                        "passed": True,
                        "score": 8.0,
                        "reasoning": "Buena validación emocional del paciente."
                    }
                ]
            }
        }

        objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
            prompt=prompt,
            report=None,
            evaluation=evaluation,
        )

        self.assertEqual(len(objectives), 1)
        obj = objectives[0]
        self.assertEqual(obj["score"], 6.0)
        self.assertEqual(obj["status"], "failed")
        self.assertEqual(obj["status_label"], "No superado")

    async def test_related_criterion_null_score_no_evidence(self):
        """Verify that when matching criteria have null score and null passed, objective is not_evaluable."""
        prompt = {
            "title": "Simulación",
            "objective_focus_json": [
                {
                    "title": "Calibración del registro protocolario de usted",
                    "related_criteria": ["Trato de Usted"]
                }
            ]
        }
        evaluation = {
            "score": 7.0,
            "result_json": {
                "criteria_evaluations": [
                    {
                        "criterion_key": "trato_de_usted",
                        "criterion_name": "Trato de Usted",
                        "passed": None,
                        "score": None,
                        "reasoning": None
                    }
                ]
            }
        }

        objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
            prompt=prompt,
            report=None,
            evaluation=evaluation,
        )

        self.assertEqual(len(objectives), 1)
        obj = objectives[0]
        self.assertEqual(obj["status"], "not_evaluable")
        self.assertEqual(obj["status_label"], "No evaluable")
        self.assertIsNone(obj["score"])
        self.assertIn("No se observó suficiente evidencia", obj["feedback"])

    async def test_objective_without_matching_criteria_is_not_evaluable(self):
        """Verify objective with zero matching criteria in evaluation is not_evaluable."""
        prompt = {
            "title": "Simulación",
            "objective_focus_json": [
                {
                    "title": "Gestión de cobros bancarios complejos",
                    "related_criteria": ["cobros_bancarios_iban"]
                }
            ]
        }
        evaluation = {
            "score": 8.0,
            "result_json": {
                "criteria_evaluations": [
                    {
                        "criterion_key": "trato_de_usted",
                        "criterion_name": "Trato de Usted",
                        "passed": True,
                        "score": 10.0
                    }
                ]
            }
        }

        objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
            prompt=prompt,
            report=None,
            evaluation=evaluation,
        )

        self.assertEqual(len(objectives), 1)
        obj = objectives[0]
        self.assertEqual(obj["status"], "not_evaluable")
        self.assertEqual(obj["status_label"], "No evaluable")
        self.assertIsNone(obj["score"])

    async def test_distinct_objectives_maintain_own_descriptions_and_behaviors(self):
        """
        Section 13: Verify that distinct objectives preserve their individual name,
        description, and expected_behavior and DO NOT inherit a shared aggregate summary.
        """
        prompt = {
            "title": "Simulación de prueba",
            "objective_summary": "Resumen global compartido de toda la simulación.",
            "expected_behavior": "Conducta global compartida de toda la simulación.",
            "objective_focus_json": [
                {
                    "title": "Calibración del registro protocolario de usted",
                    "description": "Descripción específica del trato formal.",
                    "related_criteria": ["Trato de Usted"],
                    "expected_behavior": "Conducta específica del trato formal."
                },
                {
                    "title": "Acogida empática inmediata ante vulnerabilidad",
                    "description": "Descripción específica de la empatía humana.",
                    "related_criteria": ["Empatía"],
                    "expected_behavior": "Conducta específica de la empatía humana."
                },
                {
                    "title": "Cierre estructurado con puntualidad y prestigio médico",
                    "description": "Descripción específica del cierre clínico.",
                    "related_criteria": ["Puntualidad importancia"],
                    "expected_behavior": "Conducta específica del cierre clínico."
                }
            ]
        }
        evaluation = {
            "score": 7.0,
            "result_json": {
                "criteria_evaluations": [
                    {"criterion_key": "trato_de_usted", "criterion_name": "Trato de Usted", "passed": False, "score": 3.0},
                    {"criterion_key": "empatia", "criterion_name": "Empatía", "passed": True, "score": 8.0},
                    {"criterion_key": "puntualidad_importancia", "criterion_name": "Puntualidad importancia", "passed": False, "score": 4.0}
                ]
            }
        }

        objectives = PersonalizedTrainingService.resolve_simulation_improvement_objectives(
            prompt=prompt,
            report=None,
            evaluation=evaluation,
        )

        self.assertEqual(len(objectives), 3)

        # Objective 1
        o1 = next(o for o in objectives if "protocolario" in o["name"])
        self.assertEqual(o1["name"], "Calibración del registro protocolario de usted")
        self.assertEqual(o1["description"], "Descripción específica del trato formal.")
        self.assertEqual(o1["expected_behavior"], "Conducta específica del trato formal.")
        self.assertEqual(o1["status"], "failed")
        self.assertEqual(o1["score"], 3.0)

        # Objective 2
        o2 = next(o for o in objectives if "vulnerabilidad" in o["name"])
        self.assertEqual(o2["name"], "Acogida empática inmediata ante vulnerabilidad")
        self.assertEqual(o2["description"], "Descripción específica de la empatía humana.")
        self.assertEqual(o2["expected_behavior"], "Conducta específica de la empatía humana.")
        self.assertEqual(o2["status"], "passed")
        self.assertEqual(o2["score"], 8.0)

        # Objective 3
        o3 = next(o for o in objectives if "prestigio" in o["name"])
        self.assertEqual(o3["name"], "Cierre estructurado con puntualidad y prestigio médico")
        self.assertEqual(o3["description"], "Descripción específica del cierre clínico.")
        self.assertEqual(o3["expected_behavior"], "Conducta específica del cierre clínico.")
        self.assertEqual(o3["status"], "failed")
        self.assertEqual(o3["score"], 4.0)

        # Verify none of them inherited the aggregate summary
        for o in [o1, o2, o3]:
            self.assertNotEqual(o["description"], "Resumen global compartido de toda la simulación.")
            self.assertNotEqual(o["expected_behavior"], "Conducta global compartida de toda la simulación.")


if __name__ == "__main__":
    unittest.main()

