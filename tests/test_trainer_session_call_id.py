"""
Tests verifying that TrainerSession evaluation handles call identifiers correctly
without relying on non-existent 'call_sid' attribute.
"""
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_call_id.db"
os.environ["GEMINI_API_KEY"] = "mock_key"

from sqlalchemy import text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.db import get_engine, Base, AsyncSessionLocal
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.trainer import TrainerEvaluationConfig, TrainerSimulation, TrainerSession, TrainerEvaluation
from app.services.trainer_service import TrainerService


class TestTrainerSessionCallIdEvaluation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("test_trainer_call_id.db"):
            try:
                os.remove("test_trainer_call_id.db")
            except Exception:
                pass
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSessionLocal() as db:
            svc = Service(service_id=20, service_key="svc_call_id_test", service_name="CallIdTestService")
            db.add(svc)
            await db.flush()

            prompt = Prompt(prompt_id=20, prompt_name="CallIdPrompt", prompt_type="speech_structure", service_id=20)
            db.add(prompt)
            await db.flush()

            pv = PromptVersion(id=20, prompt_id=20, version_label="v1", prompt="Prompt base", is_current=True, is_archived=False)
            db.add(pv)
            await db.flush()

            crit = PromptCriterion(
                criterion_id=201, prompt_id=20, criterion_name="Empatia", output_key="empatia",
                feed_key="feedback_empatia", criterion_type="score_1_10", is_active=True, order_index=1
            )
            db.add(crit)
            await db.flush()

            cfg = TrainerEvaluationConfig(config_id=20, name="Config20", service_id=20, speech_structure_id=20)
            db.add(cfg)
            await db.flush()

            sim = TrainerSimulation(
                simulation_id=20, code="SIM_CALL_ID", name="Sim Call ID",
                roleplay_prompt="Roleplay client", service_id=20, evaluation_config_id=20
            )
            db.add(sim)
            await db.flush()

            # Session with standard TrainerSession fields: call_id and external_call_sid. NO call_sid attribute.
            sess = TrainerSession(
                session_id=600,
                agent_id="AGENT_600",
                agent_code="AG600",
                simulation_id=20,
                service_id=20,
                call_id="CA_TWILIO_600",
                external_call_sid="CA_TWILIO_600_EXT",
                status="completed",
                evaluation_status="completed_waiting_recording",
                recording_url="https://api.twilio.com/fake/recording/600.mp3",
                duration_seconds=295,
                transcript=None,
            )
            db.add(sess)
            await db.commit()

    async def asyncTearDown(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        if os.path.exists("test_trainer_call_id.db"):
            try:
                os.remove("test_trainer_call_id.db")
            except Exception:
                pass

    async def test_session_has_no_call_sid_attribute(self):
        """Verify that TrainerSession model strictly does not define call_sid."""
        async with AsyncSessionLocal() as db:
            sess = await db.get(TrainerSession, 600)
            self.assertIsNotNone(sess)
            self.assertEqual(sess.call_id, "CA_TWILIO_600")
            self.assertEqual(sess.external_call_sid, "CA_TWILIO_600_EXT")
            self.assertFalse(hasattr(sess, "call_sid"))
            with self.assertRaises(AttributeError):
                _ = sess.call_sid

    async def test_evaluate_session_uses_call_id_without_attribute_error(self):
        """
        Verify evaluate_session_task executes end-to-end when audio needs download & transcription,
        passing call_id to both transcribe_audio and complete_text without raising AttributeError.
        """
        mock_ai_json = (
            '{"score": 8.5, "empatia": 8.5, "feedback_empatia": "Gran empatia.", '
            '"puntos_fuertes": {"items": ["Empatia"]}, "puntos_mejora": {"items": ["Cierre"]}}'
        )

        with patch("app.services.trainer_service.TrainerService.download_trainer_recording_audio", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_tr, \
             patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_ai:

            mock_dl.return_value = b"mock_audio_content"
            mock_tr.return_value = {"text": "Agente: Hola. Cliente: Buenas tardes."}
            mock_ai.return_value = mock_ai_json

            async with AsyncSessionLocal() as db:
                await TrainerService.evaluate_session_task(db, 600)

            # Check that transcribe_audio was called with the correct call_id keyword argument
            mock_tr.assert_called_once()
            tr_kwargs = mock_tr.call_args.kwargs
            self.assertEqual(tr_kwargs.get("call_id"), "CA_TWILIO_600")
            self.assertEqual(tr_kwargs.get("trainer_session_id"), 600)
            self.assertEqual(tr_kwargs.get("audio_duration_seconds"), 295.0)

            # Check that complete_text was called with the correct call_id keyword argument
            mock_ai.assert_called_once()
            ai_kwargs = mock_ai.call_args.kwargs
            self.assertEqual(ai_kwargs.get("call_id"), "CA_TWILIO_600")
            self.assertEqual(ai_kwargs.get("trainer_session_id"), 600)

        # Verify DB state after evaluation
        async with AsyncSessionLocal() as db:
            sess = await db.get(TrainerSession, 600)
            self.assertEqual(sess.evaluation_status, "evaluated")
            self.assertEqual(sess.transcript, "Agente: Hola. Cliente: Buenas tardes.")

            stmt_ev = select(TrainerEvaluation).where(TrainerEvaluation.session_id == 600)
            res_ev = await db.execute(stmt_ev)
            eval_record = res_ev.scalars().first()
            self.assertIsNotNone(eval_record)
            self.assertEqual(eval_record.score, Decimal("8.50"))
            self.assertIsNone(eval_record.error_message)

    async def test_evaluate_session_with_preexisting_transcript(self):
        """
        Verify evaluate_session_task executes when transcript is already populated,
        verifying that complete_text also receives call_id_val without AttributeError.
        """
        mock_ai_json = (
            '{"score": 9.0, "empatia": 9.0, "feedback_empatia": "Excelente.", '
            '"puntos_fuertes": {"items": ["Tono"]}, "puntos_mejora": {"items": []}}'
        )

        async with AsyncSessionLocal() as db:
            sess = await db.get(TrainerSession, 600)
            sess.transcript = "Transcripcion previa existente."
            await db.commit()

        with patch("app.services.trainer_service.TrainerService.download_trainer_recording_audio", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_tr, \
             patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_ai:

            mock_ai.return_value = mock_ai_json

            async with AsyncSessionLocal() as db:
                await TrainerService.evaluate_session_task(db, 600)

            # transcribe_audio should NOT be called since transcript was already set
            mock_dl.assert_not_called()
            mock_tr.assert_not_called()

            # complete_text should be called with call_id="CA_TWILIO_600"
            mock_ai.assert_called_once()
            self.assertEqual(mock_ai.call_args.kwargs.get("call_id"), "CA_TWILIO_600")

        async with AsyncSessionLocal() as db:
            sess = await db.get(TrainerSession, 600)
            self.assertEqual(sess.evaluation_status, "evaluated")


if __name__ == "__main__":
    unittest.main()
