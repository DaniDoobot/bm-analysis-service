import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_fixes.db"

from sqlalchemy import BigInteger, delete, select, and_
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
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingCallEvaluation,
    TrainingCallSession,
    TrainingCompletionStatus,
    TrainingEvaluationPrompt,
    TrainingSimulationPrompt,
)
from app.models.services import Service
from app.services.personalized_training_service import (
    PersonalizedTrainingService,
    evaluate_training_session_task,
)
from app.routers.training_voice import retry_session_evaluation, get_cycle_status
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole


class TestTrainerEvaluationFixes(unittest.IsolatedAsyncioTestCase):

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
            await db.execute(delete(Service))
            await db.execute(delete(Company))
            await db.commit()

    async def test_map_completion_to_dict_without_evaluation(self):
        """Comprueba que _map_completion_to_dict no lanza UnboundLocalError cuando evaluation es None."""
        prompt = TrainingSimulationPrompt(
            simulation_prompt_id=101,
            training_report_id=301,
            hubspot_owner_id="agent_007",
            prompt_number=1,
            title="Simulacion 1",
            scenario_type="roleplay",
            prompt_text="Prompt 1",
        )
        completion = TrainingCompletionStatus(
            completion_id=201,
            training_report_id=301,
            simulation_prompt_id=101,
            hubspot_owner_id="agent_007",
            status="pending",
            call_session_id=None,
            evaluation_id=None,
        )
        completion.prompt = prompt

        result = PersonalizedTrainingService._map_completion_to_dict(completion)

        self.assertIsNotNone(result)
        self.assertEqual(result["completion_id"], 201)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["prompt_number"], 1)
        self.assertEqual(result["title"], "Simulacion 1")
        self.assertIsNone(result["criteria_evaluations"])
        self.assertIsNone(result["criteria"])
        self.assertIsNone(result["score"])
        self.assertIsNone(result["feedback"])

    async def test_retry_evaluation_endpoint_realistic(self):
        """Verifica que retry_session_evaluation y get_cycle_status funcionan sin campos inexistentes."""
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as db:
            company = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
            service = Service(service_id=1, company_id=7, service_name="Front Atencion", service_key="front_atencion")
            report = TrainingAgentReport(
                training_report_id=301,
                hubspot_owner_id="agent_007",
                agent_name="Agent Seven",
                agent_initials="AS",
                company_id=7,
                service_id=1,
                period_start=now,
                period_end=now,
                status="completed",
            )
            sim_prompt = TrainingSimulationPrompt(
                simulation_prompt_id=101,
                training_report_id=301,
                hubspot_owner_id="agent_007",
                prompt_number=1,
                title="Simulacion 1",
                scenario_type="roleplay",
                prompt_text="Prompt text",
            )
            db.add_all([company, service, report, sim_prompt])
            await db.flush()

            session = TrainingCallSession(
                session_id=901,
                call_sid="CA123456789",
                cycle_id=301,
                conversation_id=101,
                agent_id="agent_007",
                status="failed",
                recording_url="https://api.twilio.com/recordings/RE123",
            )
            completion = TrainingCompletionStatus(
                completion_id=501,
                training_report_id=301,
                simulation_prompt_id=101,
                hubspot_owner_id="agent_007",
                status="failed",
                call_session_id=901,
            )
            db.add_all([session, completion])
            await db.commit()

            context = TenantContext(
                user_id=1,
                raw_role="company_admin",
                normalized_role=InternalRole.COMPANY_ADMIN,
                company_id=7,
                allowed_company_ids=[7],
                is_super_admin=False,
            )

            bg_tasks = MagicMock()
            resp = await retry_session_evaluation(
                session_id=901,
                background_tasks=bg_tasks,
                db=db,
                current_user={"sub": "admin@example.com"},
            )

            self.assertIn("Evaluation re-triggered", resp["message"])
            self.assertEqual(resp["session_id"], 901)
            bg_tasks.add_task.assert_called_once_with(evaluate_training_session_task, 901)

            # Verificar que session paso a in_progress y completion a pending
            await db.refresh(session)
            await db.refresh(completion)
            self.assertEqual(session.status, "in_progress")
            self.assertEqual(completion.status, "pending")

            # Verificar get_cycle_status
            status_resp = await get_cycle_status(
                cycle_id=301,
                context=context,
                db=db,
            )
            self.assertEqual(status_resp["cycle_id"], 301)
            self.assertEqual(len(status_resp["simulations"]), 1)
            self.assertEqual(status_resp["simulations"][0]["prompt_number"], 1)
            self.assertEqual(status_resp["simulations"][0]["status"], "pending")

    async def test_evaluate_training_session_unhandled_exception_recovers_to_failed(self):
        """Comprueba que una excepcion no controlada en la evaluacion no deja la sesion en in_progress."""
        now = datetime.now(timezone.utc)
        async with AsyncSession(self.engine) as db:
            company = Company(company_id=7, company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
            service = Service(service_id=1, company_id=7, service_name="Front Atencion", service_key="front_atencion")
            report = TrainingAgentReport(
                training_report_id=302,
                hubspot_owner_id="agent_008",
                agent_name="Agent Eight",
                agent_initials="AE",
                company_id=7,
                service_id=1,
                period_start=now,
                period_end=now,
                status="completed",
            )
            sim_prompt = TrainingSimulationPrompt(
                simulation_prompt_id=102,
                training_report_id=302,
                hubspot_owner_id="agent_008",
                prompt_number=2,
                title="Simulacion 2",
                scenario_type="roleplay",
                prompt_text="Prompt 2",
            )
            session = TrainingCallSession(
                session_id=902,
                call_sid="CA987654321",
                cycle_id=302,
                conversation_id=102,
                agent_id="agent_008",
                status="in_progress",
                recording_url="https://api.twilio.com/recordings/RE987",
            )
            completion = TrainingCompletionStatus(
                completion_id=502,
                training_report_id=302,
                simulation_prompt_id=102,
                hubspot_owner_id="agent_008",
                status="in_progress",
                call_session_id=902,
            )
            db.add_all([company, service, report, sim_prompt, session, completion])
            await db.commit()

        # Simular fallo catastrofico dentro de _evaluate_training_session_task_impl
        with patch("app.services.personalized_training_service._evaluate_training_session_task_impl", side_effect=RuntimeError("Simulated catastrophic crash during evaluation")):
            await evaluate_training_session_task(902)

        async with AsyncSession(self.engine) as db:
            res_sess = await db.execute(select(TrainingCallSession).where(TrainingCallSession.session_id == 902))
            sess = res_sess.scalars().first()
            self.assertIsNotNone(sess)
            self.assertEqual(sess.status, "failed")
            self.assertIn("Simulated catastrophic crash", sess.error_message)

            res_comp = await db.execute(select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 502))
            comp = res_comp.scalars().first()
            self.assertIsNotNone(comp)
            self.assertEqual(comp.status, "pending")


if __name__ == "__main__":
    unittest.main()
