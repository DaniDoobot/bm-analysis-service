"""
Regression tests for training simulation race conditions, short-call cleanup,
callback idempotency, and MissingGreenlet prevention in personalized training.
"""
import json
import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_training_concurrency.db"

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
from app.models.services import Service
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingAgentReport,
    TrainingCallEvaluation,
    TrainingCallSession,
    TrainingCompletionStatus,
    TrainingEvaluationPrompt,
    TrainingSimulationPrompt,
)
from app.services.personalized_training_service import (
    PersonalizedTrainingService,
    _evaluate_training_session_task_impl,
)
from app.routers.training_voice import (
    handle_roleplay_hangup,
    recording_completed,
    start_session_for_prompt,
)


class TestTrainingConcurrencyRaceCondition(unittest.IsolatedAsyncioTestCase):

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
            await db.execute(delete(TrainingAgentSetting))
            await db.execute(delete(Service))
            await db.execute(delete(Company))

            # Seed base company, service, agent setting and report
            company = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            db.add(company)
            service = Service(service_id=1, company_id=1, service_name="Front", service_key="front", is_active=True)
            db.add(service)

            eval_prompt = TrainingEvaluationPrompt(
                id=1,
                service_id=1,
                company_id=1,
                prompt_text="Default evaluation prompt",
                version=1,
                is_active=True,
            )
            db.add(eval_prompt)

            setting = TrainingAgentSetting(
                hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos",
                agent_initials="LD",
                company_id=1,
                training_code="LD23",
                is_enabled=True
            )
            db.add(setting)

            report = TrainingAgentReport(
                training_report_id=291,
                hubspot_owner_id="1375831790",
                agent_name="Luci Dos Santos",
                agent_initials="LD",
                company_id=1,
                service_id=1,
                period_start=datetime.now(timezone.utc) - timedelta(days=7),
                period_end=datetime.now(timezone.utc),
                status="running",
                is_current=True
            )
            db.add(report)

            # Add prompt 101
            prompt = TrainingSimulationPrompt(
                simulation_prompt_id=101,
                training_report_id=291,
                hubspot_owner_id="1375831790",
                prompt_number=1,
                title="Objeción de Precio",
                scenario_type="roleplay",
                prompt_text="El paciente dice que es muy caro."
            )
            db.add(prompt)

            # Add completion status for prompt 101
            comp = TrainingCompletionStatus(
                completion_id=501,
                training_report_id=291,
                simulation_prompt_id=101,
                hubspot_owner_id="1375831790",
                status="pending"
            )
            db.add(comp)

            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_01_short_call_preserves_already_completed_slot(self):
        """
        SCENARIO 1 (Luci incident exact case):
        Session A completes slot 501 with evaluation 246.
        Afterwards, a short session B (<15s) ends.
        RESULT: Slot 501 remains completed, evaluation 246 is preserved.
        """
        async with AsyncSession(self.engine) as db:
            # Session A
            sess_a = TrainingCallSession(
                session_id=264,
                call_sid="CA11a63c9669c2eb1b7730253c3dc1035f",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="evaluated",
                recording_url="https://api.twilio.com/recordings/RE264"
            )
            db.add(sess_a)

            # Evaluation 246
            eval_a = TrainingCallEvaluation(
                evaluation_id=246,
                session_id=264,
                cycle_id=291,
                conversation_id=101,
                agent_id="1375831790",
                prompt_version_id=1,
                score=Decimal("8.5"),
                feedback="Buen manejo de objeciones.",
                result_json={"is_valid_roleplay": True, "score": 8.5}
            )
            db.add(eval_a)

            # Slot 501 is completed by A
            stmt_comp = select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 501)
            comp = (await db.execute(stmt_comp)).scalars().first()
            comp.status = "completed"
            comp.completed_at = datetime.now(timezone.utc)
            comp.evaluation_id = 246
            comp.call_session_id = 264
            comp.training_call_id = "CA11a63c9669c2eb1b7730253c3dc1035f"

            # Session B starts (<15s)
            sess_b = TrainingCallSession(
                session_id=265,
                call_sid="CA999999999999999999999999999999999",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="in_progress"
            )
            db.add(sess_b)
            await db.commit()

        # Execute short-call hangup for session 265 (duration = 13.33s)
        call_start = datetime.now(timezone.utc) - timedelta(seconds=13.33)
        await handle_roleplay_hangup(265, "CA999999999999999999999999999999999", call_start, "hangup")

        # Verify Session B is marked as failed
        async with AsyncSession(self.engine) as db:
            stmt_b = select(TrainingCallSession).where(TrainingCallSession.session_id == 265)
            sess_b_db = (await db.execute(stmt_b)).scalars().first()
            self.assertEqual(sess_b_db.status, "failed")
            self.assertIn("Llamada menor a 15 segundos", sess_b_db.error_message)

            # CRITICAL CHECK: Slot 501 MUST remain completed with evaluation 246
            stmt_c = select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 501)
            comp_db = (await db.execute(stmt_c)).scalars().first()
            self.assertEqual(comp_db.status, "completed")
            self.assertEqual(comp_db.evaluation_id, 246)
            self.assertEqual(comp_db.call_session_id, 264)
            self.assertIsNotNone(comp_db.completed_at)

    async def test_02_concurrent_evaluation_and_short_call_ordering(self):
        """
        SCENARIO 2:
        Session B hangs up short (<15s) while Session A async evaluation is in progress.
        Session A evaluation finishes AFTER B cleanup.
        RESULT: Slot ends up completed with Session A evaluation.
        """
        async with AsyncSession(self.engine) as db:
            sess_a = TrainingCallSession(
                session_id=270,
                call_sid="CA_SESSION_A",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="ended",
                recording_url="https://api.twilio.com/recordings/RE270"
            )
            db.add(sess_a)

            sess_b = TrainingCallSession(
                session_id=271,
                call_sid="CA_SESSION_B",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="in_progress"
            )
            db.add(sess_b)

            stmt_comp = select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 501)
            comp = (await db.execute(stmt_comp)).scalars().first()
            comp.status = "in_progress"
            comp.call_session_id = 270
            comp.training_call_id = "CA_SESSION_A"
            await db.commit()

        # Step 1: Session B hangs up short (<15s)
        call_start_b = datetime.now(timezone.utc) - timedelta(seconds=10)
        await handle_roleplay_hangup(271, "CA_SESSION_B", call_start_b, "hangup")

        # Step 2: Session A async evaluation finishes successfully
        mock_llm_result = {
            "is_valid_roleplay": True,
            "score": 9.0,
            "feedback": "Excelente desempeño.",
            "transcription": "Agente: Hola\nPaciente: Buenas",
            "criteria_evaluations": []
        }
        with patch("app.services.twilio_service.TwilioService.download_audio", new=AsyncMock(return_value=b"fake-audio")), \
             patch("app.services.openai_service.analyze_audio_bytes", new=AsyncMock(return_value=json.dumps(mock_llm_result))), \
             patch("app.services.training_knowledge_service.TrainingKnowledgeService.generate_simulation_knowledge_document", new=AsyncMock()):
            await _evaluate_training_session_task_impl(270)

        # Check final state: slot must be completed with Session A
        async with AsyncSession(self.engine) as db:
            stmt_c = select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 501)
            comp_db = (await db.execute(stmt_c)).scalars().first()
            self.assertEqual(comp_db.status, "completed")
            self.assertEqual(comp_db.call_session_id, 270)
            self.assertIsNotNone(comp_db.evaluation_id)

    async def test_03_session_a_completes_then_session_b_short_call_cleanup(self):
        """
        SCENARIO 3:
        Session A completes first, then Session B short call cleanup executes.
        RESULT: Slot remains completed.
        """
        async with AsyncSession(self.engine) as db:
            sess_a = TrainingCallSession(
                session_id=280,
                call_sid="CA_A_280",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="ended",
                recording_url="https://api.twilio.com/recordings/RE280"
            )
            db.add(sess_a)

            sess_b = TrainingCallSession(
                session_id=281,
                call_sid="CA_B_281",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="in_progress"
            )
            db.add(sess_b)
            await db.commit()

        # Session A completes evaluation first
        mock_llm_result = {
            "is_valid_roleplay": True,
            "score": 8.0,
            "feedback": "Bien.",
            "transcription": "Agente: Hola\nPaciente: Adios",
            "criteria_evaluations": []
        }
        with patch("app.services.twilio_service.TwilioService.download_audio", new=AsyncMock(return_value=b"fake-audio")), \
             patch("app.services.openai_service.analyze_audio_bytes", new=AsyncMock(return_value=json.dumps(mock_llm_result))), \
             patch("app.services.training_knowledge_service.TrainingKnowledgeService.generate_simulation_knowledge_document", new=AsyncMock()):
            await _evaluate_training_session_task_impl(280)

        # Verify A completed the slot
        async with AsyncSession(self.engine) as db:
            stmt_c = select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 501)
            comp_db = (await db.execute(stmt_c)).scalars().first()
            self.assertEqual(comp_db.status, "completed")
            eval_a_id = comp_db.evaluation_id

        # Now Session B executes hangup cleanup for a 12s call
        call_start_b = datetime.now(timezone.utc) - timedelta(seconds=12)
        await handle_roleplay_hangup(281, "CA_B_281", call_start_b, "hangup")

        # Result: Slot remains completed, evaluation intact
        async with AsyncSession(self.engine) as db:
            stmt_c2 = select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 501)
            comp_db2 = (await db.execute(stmt_c2)).scalars().first()
            self.assertEqual(comp_db2.status, "completed")
            self.assertEqual(comp_db2.evaluation_id, eval_a_id)

    async def test_04_recording_completed_ignores_terminal_failed_session(self):
        """
        SCENARIO 4:
        A recording callback arrives for a session that is already terminal 'failed'.
        RESULT: Background evaluation is NOT triggered; no destructive mutations occur.
        """
        async with AsyncSession(self.engine) as db:
            sess_failed = TrainingCallSession(
                session_id=290,
                call_sid="CA_FAILED_290",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="failed",
                error_message="Llamada menor a 15 segundos: 13 segundos."
            )
            db.add(sess_failed)
            await db.commit()

        # Build mock Request for recording_completed
        mock_request = MagicMock()
        mock_request.form = AsyncMock(return_value={
            "CallSid": "CA_FAILED_290",
            "RecordingSid": "RE_FAILED_290",
            "RecordingUrl": "https://api.twilio.com/recordings/RE_FAILED_290",
            "RecordingStatus": "completed"
        })

        async with AsyncSession(self.engine) as db:
            response = await recording_completed(mock_request, db=db)
            self.assertEqual(response.status_code, 200)
            # Verify background task is None or empty
            self.assertIsNone(response.background)

            # Also verify direct call to _evaluate_training_session_task_impl skips failed session
            with patch("app.services.openai_service.analyze_audio_bytes") as mock_eval:
                await _evaluate_training_session_task_impl(290)
                mock_eval.assert_not_called()

    async def test_05_recording_completed_idempotent_on_evaluated_session(self):
        """
        SCENARIO 5:
        Duplicate recording callback arrives for an already evaluated session.
        RESULT: Webhook returns 200 ok and skips triggering a new evaluation.
        """
        async with AsyncSession(self.engine) as db:
            sess_eval = TrainingCallSession(
                session_id=295,
                call_sid="CA_EVALUATED_295",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="evaluated",
                recording_sid="RE_295",
                recording_url="https://api.twilio.com/recordings/RE_295",
                evaluation_completed_at=datetime.now(timezone.utc)
            )
            db.add(sess_eval)

            eval_rec = TrainingCallEvaluation(
                evaluation_id=888,
                session_id=295,
                cycle_id=291,
                conversation_id=101,
                agent_id="1375831790",
                prompt_version_id=1,
                score=Decimal("8.0"),
                result_json={"is_valid_roleplay": True, "score": 8.0}
            )
            db.add(eval_rec)
            await db.commit()

        mock_request = MagicMock()
        mock_request.form = AsyncMock(return_value={
            "CallSid": "CA_EVALUATED_295",
            "RecordingSid": "RE_295_DUP",
            "RecordingUrl": "https://api.twilio.com/recordings/RE_295",
            "RecordingStatus": "completed"
        })

        async with AsyncSession(self.engine) as db:
            response = await recording_completed(mock_request, db=db)
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.background)

    async def test_06_get_agent_detail_missing_greenlet_immunity(self):
        """
        SCENARIO 6:
        get_agent_detail loads a completion status with an evaluation whose call_session was unlinked/None.
        RESULT: Returns 200 without raising MissingGreenlet.
        """
        async with AsyncSession(self.engine) as db:
            sess_9999 = TrainingCallSession(
                session_id=9999,
                call_sid="CA_SESSION_9999",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="evaluated"
            )
            db.add(sess_9999)

            eval_record = TrainingCallEvaluation(
                evaluation_id=999,
                session_id=9999,
                cycle_id=291,
                conversation_id=101,
                agent_id="1375831790",
                prompt_version_id=1,
                score=Decimal("7.5"),
                feedback="Buen tono.",
                result_json={"is_valid_roleplay": True, "score": 7.5}
            )
            db.add(eval_record)

            stmt_comp = select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 501)
            comp = (await db.execute(stmt_comp)).scalars().first()
            comp.status = "completed"
            comp.evaluation_id = 999
            # call_session_id is intentionally None (like after the short-call bug cleared it)
            comp.call_session_id = None
            await db.commit()

        async with AsyncSession(self.engine) as db:
            # Must run cleanly without MissingGreenlet
            detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id="1375831790")
            self.assertIsNotNone(detail)
            self.assertIn("current_report", detail)
            curr = detail["current_report"]
            self.assertIsNotNone(curr)
            self.assertIn("simulations", curr)
            slots = curr["simulations"]
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0]["status"], "completed")
            self.assertEqual(slots[0]["evaluation_id"], 999)

    async def test_07_full_cycle_finalization_4_of_4_completes_report(self):
        """
        SCENARIO 7:
        When all 4 simulations in a cycle complete normally, check_and_finalize_training_cycle
        successfully transitions the report to 'completed'.
        """
        async with AsyncSession(self.engine) as db:
            # Add remaining 3 prompts and completions
            for i in [2, 3, 4]:
                sess_i = TrainingCallSession(
                    session_id=300 + i,
                    call_sid=f"CA_300_{i}",
                    agent_id="1375831790",
                    cycle_id=291,
                    conversation_id=100 + i,
                    status="evaluated"
                )
                db.add(sess_i)

                p = TrainingSimulationPrompt(
                    simulation_prompt_id=100 + i,
                    training_report_id=291,
                    hubspot_owner_id="1375831790",
                    prompt_number=i,
                    title=f"Simulacion {i}",
                    scenario_type="roleplay",
                    prompt_text=f"Prompt {i}"
                )
                db.add(p)

                comp = TrainingCompletionStatus(
                    completion_id=500 + i,
                    training_report_id=291,
                    simulation_prompt_id=100 + i,
                    hubspot_owner_id="1375831790",
                    status="completed",
                    completed_at=datetime.now(timezone.utc),
                    evaluation_id=200 + i,
                    call_session_id=300 + i
                )
                db.add(comp)

                ev = TrainingCallEvaluation(
                    evaluation_id=200 + i,
                    session_id=300 + i,
                    cycle_id=291,
                    conversation_id=100 + i,
                    agent_id="1375831790",
                    prompt_version_id=1,
                    result_json={"is_valid_roleplay": True, "score": 8.0},
                    score=Decimal("8.0")
                )
                db.add(ev)

            # Also mark prompt 1 completed
            sess_301 = TrainingCallSession(
                session_id=301,
                call_sid="CA_301",
                agent_id="1375831790",
                cycle_id=291,
                conversation_id=101,
                status="evaluated"
            )
            db.add(sess_301)

            stmt_comp1 = select(TrainingCompletionStatus).where(TrainingCompletionStatus.completion_id == 501)
            comp1 = (await db.execute(stmt_comp1)).scalars().first()
            comp1.status = "completed"
            comp1.completed_at = datetime.now(timezone.utc)
            comp1.evaluation_id = 201
            comp1.call_session_id = 301

            ev1 = TrainingCallEvaluation(
                evaluation_id=201,
                session_id=301,
                cycle_id=291,
                conversation_id=101,
                agent_id="1375831790",
                prompt_version_id=1,
                result_json={"is_valid_roleplay": True, "score": 8.0},
                score=Decimal("8.0")
            )
            db.add(ev1)
            await db.commit()

        # Finalize cycle
        from app.services.personalized_training_service import check_and_finalize_training_cycle
        with patch("app.services.personalized_training_service.complete_text", new=AsyncMock(return_value='{"summary_final": "Evolución positiva", "strengths": ["Escucha", "Empatía", "Claridad"], "weaknesses": ["Cierre", "Objeciones", "Ritmo"], "recommendations": "Seguir practicando", "objectives_status": []}')), \
             patch("app.services.training_knowledge_service.TrainingKnowledgeService.generate_cycle_knowledge_documents", new=AsyncMock()):
            async with AsyncSession(self.engine) as db:
                await check_and_finalize_training_cycle(db, 291)
                await db.commit()

        async with AsyncSession(self.engine) as db:
            stmt_rep = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == 291)
            rep_db = (await db.execute(stmt_rep)).scalars().first()
            self.assertEqual(rep_db.status, "completed")
            self.assertIsNotNone(rep_db.final_report_json)
