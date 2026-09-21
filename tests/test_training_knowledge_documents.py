"""Tests for Trainer Knowledge Layer Phase 2: Document persistence, generation, and idempotency."""
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_knowledge_docs.db"

from sqlalchemy import BigInteger, delete, select, and_, func
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
from app.models.services import Service
from app.models.teams import Team, AgentTeamAssociation
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingCallEvaluation,
    TrainingCallSession,
    TrainingCompletionStatus,
    TrainingEvaluationPrompt,
    TrainingSimulationPrompt,
    TrainingKnowledgeDocument,
)
from app.services.training_knowledge_service import TrainingKnowledgeService


class TestTrainingKnowledgeDocuments(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await db.execute(delete(TrainingKnowledgeDocument))
            await db.execute(delete(TrainingCallEvaluation))
            await db.execute(delete(TrainingCallSession))
            await db.execute(delete(TrainingCompletionStatus))
            await db.execute(delete(TrainingSimulationPrompt))
            await db.execute(delete(TrainingEvaluationPrompt))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(AgentTeamAssociation))
            await db.execute(delete(User))
            await db.execute(delete(Team))
            await db.execute(delete(Service))
            await db.execute(delete(Company))
            await db.commit()

            # Seed base company and service
            self.company = Company(company_id=10, company_name="Hospital Central", company_key="HOSP_CENTRAL")
            self.company2 = Company(company_id=20, company_name="Clínica Norte", company_key="CLIN_NORTE")
            self.service = Service(service_id=100, company_id=10, service_key="atencion_paciente", service_name="Atención al Paciente")
            db.add_all([self.company, self.company2, self.service])
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            await db.execute(delete(TrainingKnowledgeDocument))
            await db.execute(delete(TrainingCallEvaluation))
            await db.execute(delete(TrainingCallSession))
            await db.execute(delete(TrainingCompletionStatus))
            await db.execute(delete(TrainingSimulationPrompt))
            await db.execute(delete(TrainingEvaluationPrompt))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(AgentTeamAssociation))
            await db.execute(delete(User))
            await db.execute(delete(Team))
            await db.execute(delete(Service))
            await db.execute(delete(Company))
            await db.commit()

    async def _create_test_cycle(
        self,
        db: AsyncSession,
        hubspot_owner_id: str = "agent_01",
        agent_name: str = "Carlos Agente",
        agent_initials: str = "CA",
        company_id: int = 10,
        service_id: int = 100,
        num_simulations: int = 1,
        status: str = "in_progress",
    ) -> tuple[TrainingAgentReport, list[TrainingSimulationPrompt], list[TrainingCallEvaluation]]:
        report = TrainingAgentReport(
            company_id=company_id,
            service_id=service_id,
            hubspot_owner_id=hubspot_owner_id,
            agent_name=agent_name,
            agent_initials=agent_initials,
            period_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 9, 15, tzinfo=timezone.utc),
            status=status,
            general_objectives_json=[
                {"title": "Escucha Activa", "description": "Escuchar sin interrumpir", "base_score": 6.0}
            ],
            specific_objectives_json=[
                {
                    "title": "Manejo de Objeciones",
                    "description": "Resolver dudas sobre tarifas",
                    "base_score": 5.5,
                    "related_criteria": ["manejo_objeciones"],
                }
            ],
            final_report_json={
                "summary_final": "El agente ha mostrado excelente evolución técnica.",
                "strengths": ["Claridad al explicar", "Tono profesional", "Resolución ágil"],
                "weaknesses": ["Validación de identidad", "Preguntas abiertas", "Despedida"],
                "recommendations": "Profundizar en técnicas de cierre suave en el próximo ciclo.",
                "objectives_status": [
                    {
                        "title": "Escucha Activa",
                        "type": "general",
                        "description": "Escuchar sin interrumpir",
                        "base_score": 6.0,
                        "score": 8.0,
                        "improvement_delta": 2.0,
                        "status": "superado",
                        "justification": "Mantuvo silencios adecuados y parafraseó.",
                    },
                    {
                        "title": "Manejo de Objeciones",
                        "type": "especifico",
                        "description": "Resolver dudas sobre tarifas",
                        "base_score": 5.5,
                        "score": 7.5,
                        "improvement_delta": 2.0,
                        "status": "superado",
                        "related_criteria": ["manejo_objeciones"],
                        "justification": "Argumentó con valor añadido sin confrontar.",
                    },
                ],
            } if status == "completed" else None,
            avg_evaluacion_global=Decimal("7.75") if status == "completed" else None,
        )
        db.add(report)
        await db.flush()

        prompts = []
        evaluations = []

        eval_prompt = TrainingEvaluationPrompt(
            id=1,
            service_id=service_id,
            company_id=company_id,
            prompt_text="Prompt de evaluación general",
            version=1,
            is_active=True,
        )
        db.add(eval_prompt)
        await db.flush()

        for i in range(1, num_simulations + 1):
            prompt = TrainingSimulationPrompt(
                training_report_id=report.training_report_id,
                hubspot_owner_id=hubspot_owner_id,
                prompt_number=i,
                title=f"Simulación de Consulta {i}",
                scenario_type="consulta_general",
                objective_focus_json={"foco": f"Evaluar gestión de llamada {i}"},
                prompt_text=f"El paciente llama con dudas sobre su cita médica #{i}.",
            )
            db.add(prompt)
            await db.flush()
            prompts.append(prompt)

            session = TrainingCallSession(
                call_sid=f"CA_test_{report.training_report_id}_{i}",
                agent_id=hubspot_owner_id,
                cycle_id=report.training_report_id,
                conversation_id=prompt.simulation_prompt_id,
                status="evaluated",
            )
            db.add(session)
            await db.flush()

            evaluation = TrainingCallEvaluation(
                session_id=session.session_id,
                cycle_id=report.training_report_id,
                conversation_id=prompt.simulation_prompt_id,
                agent_id=hubspot_owner_id,
                prompt_version_id=eval_prompt.id,
                transcription="[Turno 1] Agente: Buenos días, ¿en qué puedo ayudarle?\n[Turno 2] Paciente: Quería consultar una cita.",
                result_json={
                    "is_valid_roleplay": True,
                    "score": 7.5,
                    "feedback": f"Llamada {i} bien gestionada con buen ritmo.",
                    "result_json": {"empatia": True, "claridad": True},
                    "criteria_evaluations": [
                        {
                            "criterion_key": "empatia",
                            "criterion_name": "Empatía y Calidez",
                            "score": 8.0,
                            "passed": True,
                            "expected_behavior": "Mostrar comprensión genuina.",
                            "observed_behavior": "Validó la preocupación del paciente oportunamente.",
                            "evidence_quote": "Buenos días, ¿en qué puedo ayudarle?",
                            "relevant_turns": [1],
                            "reasoning": "El saludo inicial fue cálido y receptivo.",
                            "improvement_tip": "Mantener este nivel durante el cierre.",
                        }
                    ],
                },
                score=Decimal("7.50"),
                feedback=f"Llamada {i} bien gestionada con buen ritmo.",
            )
            db.add(evaluation)
            await db.flush()
            evaluations.append(evaluation)

            comp = TrainingCompletionStatus(
                training_report_id=report.training_report_id,
                simulation_prompt_id=prompt.simulation_prompt_id,
                hubspot_owner_id=hubspot_owner_id,
                status="completed",
                call_session_id=session.session_id,
                evaluation_id=evaluation.evaluation_id,
            )
            db.add(comp)
            await db.flush()

        await db.commit()
        return report, prompts, evaluations

    # 1. test_simulation_document_generation
    async def test_simulation_document_generation(self):
        """Creates a cycle with a simulation and verifies that the simulation document is correctly generated."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(db, num_simulations=1)
            eval_id = evals[0].evaluation_id

            doc = await TrainingKnowledgeService.generate_simulation_knowledge_document(db, eval_id)

            self.assertIsNotNone(doc)
            self.assertEqual(doc.document_type, "simulation")
            self.assertEqual(doc.cycle_id, report.training_report_id)
            self.assertEqual(doc.simulation_id, prompts[0].simulation_prompt_id)
            self.assertEqual(doc.evaluation_id, eval_id)
            self.assertEqual(doc.hubspot_owner_id, "agent_01")
            self.assertEqual(doc.company_id, 10)
            self.assertIn("Simulación 1: Simulación de Consulta 1", doc.title)
            self.assertIn("DOCUMENTO DE CONOCIMIENTO: SIMULACIÓN", doc.content)
            self.assertIn("Hospital Central", doc.content)
            self.assertIn("Carlos Agente", doc.content)

    # 2. test_simulation_document_contains_structured_evidence
    async def test_simulation_document_contains_structured_evidence(self):
        """Verifies that criterion, score, evidence quote, reasoning, turns, and tip appear in the document."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(db, num_simulations=1)
            eval_id = evals[0].evaluation_id

            doc = await TrainingKnowledgeService.generate_simulation_knowledge_document(db, eval_id)

            self.assertIn("Empatía y Calidez", doc.content)
            self.assertIn("8.0 / 10.0", doc.content)
            self.assertIn("Superado", doc.content)
            self.assertIn("Mostrar comprensión genuina.", doc.content)
            self.assertIn("Validó la preocupación del paciente oportunamente.", doc.content)
            self.assertIn('"Buenos días, ¿en qué puedo ayudarle?"', doc.content)
            self.assertIn("**Turnos Relevantes:** 1", doc.content)
            self.assertIn("El saludo inicial fue cálido y receptivo.", doc.content)
            self.assertIn("Mantener este nivel durante el cierre.", doc.content)

    # 3. test_cycle_document_generation
    async def test_cycle_document_generation(self):
        """Verifies that upon completing the cycle, the cycle summary document is generated."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(
                db, num_simulations=2, status="completed"
            )

            docs = await TrainingKnowledgeService.generate_cycle_knowledge_documents(
                db, report.training_report_id
            )

            # Returns 2 simulation documents + 1 cycle document
            self.assertEqual(len(docs), 3)

            cycle_docs = [d for d in docs if d.document_type == "cycle"]
            self.assertEqual(len(cycle_docs), 1)
            cycle_doc = cycle_docs[0]

            self.assertEqual(cycle_doc.cycle_id, report.training_report_id)
            self.assertIsNone(cycle_doc.simulation_id)
            self.assertIn("DOCUMENTO DE CONOCIMIENTO: RESUMEN DE CICLO", cycle_doc.content)
            self.assertIn("7.75 / 10.0", cycle_doc.content)
            self.assertIn("Escucha Activa", cycle_doc.content)
            self.assertIn("Manejo de Objeciones", cycle_doc.content)
            self.assertIn("El agente ha mostrado excelente evolución técnica.", cycle_doc.content)

    # 4. test_cycle_document_waits_until_cycle_is_complete
    async def test_cycle_document_waits_until_cycle_is_complete(self):
        """Verifies that an incomplete cycle with pending simulations does not generate a cycle document."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(
                db, num_simulations=2, status="in_progress"
            )

            # Mark 2nd simulation as pending
            stmt_comp = select(TrainingCompletionStatus).where(
                TrainingCompletionStatus.simulation_prompt_id == prompts[1].simulation_prompt_id
            )
            res = await db.execute(stmt_comp)
            comp2 = res.scalars().first()
            comp2.status = "pending"
            comp2.evaluation_id = None
            await db.commit()

            # Attempt cycle document generation
            docs = await TrainingKnowledgeService.generate_cycle_knowledge_documents(
                db, report.training_report_id
            )

            self.assertEqual(docs, [])

            # Verify no cycle document exists in DB
            stmt_docs = select(TrainingKnowledgeDocument).where(
                and_(
                    TrainingKnowledgeDocument.cycle_id == report.training_report_id,
                    TrainingKnowledgeDocument.document_type == "cycle",
                )
            )
            r = await db.execute(stmt_docs)
            self.assertIsNone(r.scalars().first())

    # 5. test_document_generation_is_idempotent
    async def test_document_generation_is_idempotent(self):
        """Verifies that calling generation multiple times updates in place without creating duplicate rows."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(
                db, num_simulations=2, status="completed"
            )

            # First run
            docs_run1 = await TrainingKnowledgeService.generate_cycle_knowledge_documents(
                db, report.training_report_id
            )
            self.assertEqual(len(docs_run1), 3)

            stmt_count = select(func.count(TrainingKnowledgeDocument.id)).where(
                TrainingKnowledgeDocument.cycle_id == report.training_report_id
            )
            count1 = (await db.execute(stmt_count)).scalar()
            self.assertEqual(count1, 3)

            # Second run
            docs_run2 = await TrainingKnowledgeService.generate_cycle_knowledge_documents(
                db, report.training_report_id
            )
            count2 = (await db.execute(stmt_count)).scalar()
            self.assertEqual(count2, 3)

            # Check that primary key IDs are the same
            ids_run1 = sorted([d.id for d in docs_run1])
            ids_run2 = sorted([d.id for d in docs_run2])
            self.assertEqual(ids_run1, ids_run2)

    # 6. test_historical_evaluation_without_evidence_remains_valid
    async def test_historical_evaluation_without_evidence_remains_valid(self):
        """Verifies that legacy evaluations without criteria_evaluations generate clean docs with zero fabrication."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(db, num_simulations=1)
            evaluation = evals[0]

            # Set legacy result_json with boolean dictionary only
            evaluation.result_json = {
                "is_valid_roleplay": True,
                "score": 6.5,
                "feedback": "Evaluación histórica sin desglose multimodal.",
                "result_json": {
                    "saludo_cordial": True,
                    "identificacion_paciente": False,
                },
            }
            await db.commit()

            doc = await TrainingKnowledgeService.generate_simulation_knowledge_document(
                db, evaluation.evaluation_id
            )

            self.assertIsNotNone(doc)
            self.assertIn("Saludo cordial:** Cumplido", doc.content)
            self.assertIn("Identificacion paciente:** No cumplido", doc.content)

            # Ensure NO artificial evidence is fabricated
            self.assertNotIn("Evidencia Textual:", doc.content)
            self.assertNotIn("Comportamiento Esperado:", doc.content)
            self.assertNotIn("Turnos Relevantes:", doc.content)

    # 7. test_document_multitenant_isolation
    async def test_document_multitenant_isolation(self):
        """Verifies that documents are isolated by company_id and hubspot_owner_id."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(
                db, company_id=10, hubspot_owner_id="agent_tenant_1", num_simulations=1
            )
            doc = await TrainingKnowledgeService.generate_simulation_knowledge_document(
                db, evals[0].evaluation_id
            )

            # 1. Query for Company 20 (other company) -> 0 results
            docs_comp20 = await TrainingKnowledgeService.list_documents(db, company_id=20)
            self.assertEqual(len(docs_comp20), 0)

            # 2. Query for other agent -> 0 results
            docs_other_agent = await TrainingKnowledgeService.list_documents(
                db, company_id=10, hubspot_owner_id="agent_tenant_other"
            )
            self.assertEqual(len(docs_other_agent), 0)

            # 3. Query for target company and agent -> 1 result
            docs_target = await TrainingKnowledgeService.list_documents(
                db, company_id=10, hubspot_owner_id="agent_tenant_1"
            )
            self.assertEqual(len(docs_target), 1)
            self.assertEqual(docs_target[0].id, doc.id)

    # 8. test_document_metadata
    async def test_document_metadata(self):
        """Checks that metadata_json contains all expected filterable identifiers."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            # Assign agent to a team
            team = Team(team_name="Equipo Alpha", company_id=10, service_id=100)
            db.add(team)
            await db.flush()

            user = User(
                username="carlos_test",
                email="carlos@hospital.es",
                hubspot_owner_id="agent_meta_01",
                role="agent",
                company_id=10,
                primary_service_id=100,
                password_hash="dummy_hash",
            )
            db.add(user)
            await db.flush()

            db.add(AgentTeamAssociation(user_id=user.user_id, team_id=team.team_id))
            await db.commit()

            report, prompts, evals = await self._create_test_cycle(
                db, hubspot_owner_id="agent_meta_01", num_simulations=1, status="completed"
            )
            docs = await TrainingKnowledgeService.generate_cycle_knowledge_documents(
                db, report.training_report_id
            )

            sim_doc = [d for d in docs if d.document_type == "simulation"][0]
            cycle_doc = [d for d in docs if d.document_type == "cycle"][0]

            # Verify simulation metadata
            s_meta = sim_doc.metadata_json
            self.assertEqual(s_meta["company_id"], 10)
            self.assertEqual(s_meta["company_name"], "Hospital Central")
            self.assertEqual(s_meta["hubspot_owner_id"], "agent_meta_01")
            self.assertEqual(s_meta["service_id"], 100)
            self.assertEqual(s_meta["team_id"], team.team_id)
            self.assertEqual(s_meta["team_name"], "Equipo Alpha")
            self.assertEqual(s_meta["cycle_id"], report.training_report_id)
            self.assertEqual(s_meta["simulation_id"], prompts[0].simulation_prompt_id)
            self.assertEqual(s_meta["simulation_number"], 1)
            self.assertEqual(s_meta["evaluation_id"], evals[0].evaluation_id)
            self.assertEqual(s_meta["document_type"], "simulation")
            self.assertEqual(s_meta["score"], 7.5)

            # Verify cycle metadata
            c_meta = cycle_doc.metadata_json
            self.assertEqual(c_meta["company_id"], 10)
            self.assertEqual(c_meta["hubspot_owner_id"], "agent_meta_01")
            self.assertEqual(c_meta["cycle_id"], report.training_report_id)
            self.assertEqual(c_meta["document_type"], "cycle")
            self.assertIsNone(c_meta["simulation_id"])
            self.assertEqual(c_meta["simulations_count"], 1)

    # 9. test_document_does_not_call_llm
    async def test_document_does_not_call_llm(self):
        """Verifies that knowledge document generation is 100% deterministic and never invokes Gemini/OpenAI."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(
                db, num_simulations=2, status="completed"
            )

            with patch("google.genai.Client") as mock_gemini, \
                 patch("app.services.personalized_training_service.complete_text", new_callable=AsyncMock) as mock_complete_text:

                docs = await TrainingKnowledgeService.generate_cycle_knowledge_documents(
                    db, report.training_report_id
                )

                self.assertEqual(len(docs), 3)
                mock_gemini.assert_not_called()
                mock_complete_text.assert_not_called()

    # 10. test_cycle_document_contains_all_simulations
    async def test_cycle_document_contains_all_simulations(self):
        """Verifies that the cycle document references all completed simulations."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            report, prompts, evals = await self._create_test_cycle(
                db, num_simulations=3, status="completed"
            )

            docs = await TrainingKnowledgeService.generate_cycle_knowledge_documents(
                db, report.training_report_id
            )

            cycle_doc = [d for d in docs if d.document_type == "cycle"][0]

            self.assertIn("Simulación 1: Simulación de Consulta 1", cycle_doc.content)
            self.assertIn("Simulación 2: Simulación de Consulta 2", cycle_doc.content)
            self.assertIn("Simulación 3: Simulación de Consulta 3", cycle_doc.content)


if __name__ == "__main__":
    unittest.main()
