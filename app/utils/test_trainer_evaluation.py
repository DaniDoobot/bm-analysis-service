"""Tests for trainer evaluation service: score fallback and session score consistency."""
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///trainer_eval_test.db"
os.environ["GEMINI_API_KEY"] = "mock_key"

# Safety Confirmation Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution was blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite Type Compilers for Compatibility
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.db import get_engine, Base, AsyncSessionLocal
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession


class DbSetupMixin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import app.models  # noqa
        engine = get_engine()
        if os.path.exists("trainer_eval_test.db"):
            try:
                os.remove("trainer_eval_test.db")
            except Exception:
                pass
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        if os.path.exists("trainer_eval_test.db"):
            try:
                os.remove("trainer_eval_test.db")
            except Exception:
                pass


async def _create_eval_fixture(db):
    from app.models.services import Service
    from app.models.prompts import Prompt, PromptVersion
    from app.models.criteria import PromptCriterion
    from app.models.trainer import TrainerEvaluationConfig, TrainerSimulation, TrainerSession

    svc = Service(service_id=10, service_key="svc_eval_test", service_name="EvalTestService")
    db.add(svc)
    await db.flush()

    prompt = Prompt(prompt_id=10, prompt_name="EvalPrompt", prompt_type="speech_structure", service_id=10)
    db.add(prompt)
    await db.flush()

    pv = PromptVersion(id=10, prompt_id=10, version_label="v1", prompt="Evalua.", is_current=True, is_archived=False)
    db.add(pv)
    await db.flush()

    crit1 = PromptCriterion(criterion_id=101, prompt_id=10, criterion_name="Empatia", output_key="empatia",
                             feed_key="feedback_empatia", criterion_type="score_1_10", is_active=True, order_index=1)
    crit2 = PromptCriterion(criterion_id=102, prompt_id=10, criterion_name="Claridad", output_key="claridad",
                             feed_key="feedback_claridad", criterion_type="score_1_10", is_active=True, order_index=2)
    crit3 = PromptCriterion(criterion_id=103, prompt_id=10, criterion_name="Saludo", output_key="saludo_correcto",
                             feed_key=None, criterion_type="boolean", is_active=True, order_index=3)
    db.add_all([crit1, crit2, crit3])
    await db.flush()

    cfg = TrainerEvaluationConfig(config_id=10, name="TestConfig", service_id=10, speech_structure_id=10, extra_instructions=None)
    db.add(cfg)
    await db.flush()

    sim = TrainerSimulation(simulation_id=10, code="SIMEVAL", name="Eval Test Sim",
                             roleplay_prompt="Carmen es clienta.", service_id=10, evaluation_config_id=10)
    db.add(sim)
    await db.flush()

    sess = TrainerSession(session_id=500, agent_id="AGENT_EVAL_500", agent_code="EVAL500",
                          simulation_id=10, service_id=10, call_id="call_eval_500",
                          status="completed", evaluation_status="completed_waiting_recording",
                          recording_url="https://fake.url/recording.mp3",
                          transcript="Hola, soy el agente.")
    db.add(sess)
    await db.commit()


class TestEvaluationCriteriaFallbackScore(DbSetupMixin):
    async def test_criteria_only_score_marks_evaluated(self):
        from app.services.trainer_service import TrainerService
        from app.models.trainer import TrainerEvaluation, TrainerSession

        async with AsyncSessionLocal() as db:
            await _create_eval_fixture(db)

        mock_ai_json = '{"empatia": 6.0, "feedback_empatia": "Buena empatia.", "claridad": 8.0, "feedback_claridad": "Clara.", "saludo_correcto": true, "puntos_fuertes": {"t": "ok"}, "puntos_mejora": {"t": "ok"}}'

        with patch("app.services.trainer_service.TrainerService.download_trainer_recording_audio", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_tr, \
             patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_ai:
            mock_dl.return_value = b"fake_audio"
            mock_tr.return_value = {"text": "Hola."}
            mock_ai.return_value = mock_ai_json

            async with AsyncSessionLocal() as db:
                sess = await db.get(TrainerSession, 500)
                sess.transcript = None
                await db.commit()

            async with AsyncSessionLocal() as db:
                await TrainerService.evaluate_session_task(db, 500)

        async with AsyncSessionLocal() as db:
            sess = await db.get(TrainerSession, 500)
            self.assertEqual(sess.evaluation_status, "evaluated")

            res = await db.execute(select(TrainerEvaluation).where(TrainerEvaluation.session_id == 500))
            eval_rec = res.scalar()
            self.assertIsNotNone(eval_rec)
            self.assertIsNotNone(eval_rec.score)
            expected_avg = round((6.0 + 8.0) / 2, 2)
            self.assertAlmostEqual(float(eval_rec.score), expected_avg, places=2)


class TestEvaluationNoScoreNoCriteria(DbSetupMixin):
    async def test_no_criteria_match_marks_without_score(self):
        from app.services.trainer_service import TrainerService
        from app.models.trainer import TrainerEvaluation, TrainerSession

        async with AsyncSessionLocal() as db:
            await _create_eval_fixture(db)

        mock_ai_json = '{"resumen": "El agente fue correcto.", "total": "n/a"}'

        with patch("app.services.trainer_service.TrainerService.download_trainer_recording_audio", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_tr, \
             patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_ai:
            mock_dl.return_value = b"fake_audio"
            mock_tr.return_value = {"text": "Hola."}
            mock_ai.return_value = mock_ai_json

            async with AsyncSessionLocal() as db:
                sess = await db.get(TrainerSession, 500)
                sess.transcript = None
                await db.commit()

            async with AsyncSessionLocal() as db:
                await TrainerService.evaluate_session_task(db, 500)

        async with AsyncSessionLocal() as db:
            sess = await db.get(TrainerSession, 500)
            self.assertEqual(sess.evaluation_status, "completed_without_score")

            res = await db.execute(select(TrainerEvaluation).where(TrainerEvaluation.session_id == 500))
            eval_rec = res.scalar()
            self.assertIsNotNone(eval_rec)
            self.assertIsNone(eval_rec.score)


class TestSessionScoreConsistency(DbSetupMixin):
    async def _setup_with_eval(self, score, result_json):
        from app.models.trainer import TrainerEvaluation, TrainerSession

        async with AsyncSessionLocal() as db:
            await _create_eval_fixture(db)
            sess = await db.get(TrainerSession, 500)
            sess.evaluation_status = "evaluated" if score is not None else "completed_without_score"
            sess.transcript = "Hola."
            await db.flush()
            eval_rec = TrainerEvaluation(
                session_id=500, evaluation_config_id=10, prompt_snapshot="p",
                result_json=result_json,
                score=Decimal(str(score)) if score is not None else None,
                summary=result_json.get("feedback"),
            )
            db.add(eval_rec)
            await db.commit()

    async def test_list_and_detail_same_score_when_set(self):
        await self._setup_with_eval(7.5, {"empatia": 6.0, "claridad": 9.0, "feedback": "Bien."})
        from app.services.trainer_service import TrainerService

        async with AsyncSessionLocal() as db:
            sessions, _ = await TrainerService.list_sessions(db)
            s = next((x for x in sessions if x.session_id == 500), None)
            list_score = s.__dict__.get("score")
            list_source = s.__dict__.get("score_source")

        async with AsyncSessionLocal() as db:
            detail = await TrainerService.get_session_detail(db, 500)
            detail_score = detail.__dict__.get("score")
            detail_source = detail.__dict__.get("score_source")

        self.assertAlmostEqual(list_score, 7.5, places=2)
        self.assertAlmostEqual(detail_score, 7.5, places=2)
        self.assertEqual(list_source, "evaluation_score")
        self.assertEqual(detail_source, "evaluation_score")

    async def test_list_and_detail_criteria_average_when_score_null(self):
        await self._setup_with_eval(None, {"empatia": 4.0, "claridad": 6.0, "saludo_correcto": True})
        from app.services.trainer_service import TrainerService

        async with AsyncSessionLocal() as db:
            sessions, _ = await TrainerService.list_sessions(db)
            s = next((x for x in sessions if x.session_id == 500), None)
            list_score = s.__dict__.get("score")
            list_source = s.__dict__.get("score_source")

        async with AsyncSessionLocal() as db:
            detail = await TrainerService.get_session_detail(db, 500)
            detail_score = detail.__dict__.get("score")
            detail_source = detail.__dict__.get("score_source")

        expected = round((4.0 + 6.0) / 2, 2)
        self.assertAlmostEqual(list_score, expected, places=2)
        self.assertAlmostEqual(detail_score, expected, places=2)
        self.assertEqual(list_source, "criteria_average")
        self.assertEqual(detail_source, "criteria_average")


class TestEvaluationTopLevelScorePrecedence(DbSetupMixin):
    async def test_top_level_score_takes_precedence(self):
        from app.services.trainer_service import TrainerService
        from app.models.trainer import TrainerEvaluation, TrainerSession

        async with AsyncSessionLocal() as db:
            await _create_eval_fixture(db)

        mock_ai_json = '{"evaluacion_global": 9.5, "empatia": 5.0, "claridad": 4.0, "feedback": "Excelente."}'

        with patch("app.services.trainer_service.TrainerService.download_trainer_recording_audio", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.openai_service.transcribe_audio", new_callable=AsyncMock) as mock_tr, \
             patch("app.services.openai_service.complete_text", new_callable=AsyncMock) as mock_ai:
            mock_dl.return_value = b"fake_audio"
            mock_tr.return_value = {"text": "Hola."}
            mock_ai.return_value = mock_ai_json

            async with AsyncSessionLocal() as db:
                sess = await db.get(TrainerSession, 500)
                sess.transcript = None
                await db.commit()

            async with AsyncSessionLocal() as db:
                await TrainerService.evaluate_session_task(db, 500)

        async with AsyncSessionLocal() as db:
            res = await db.execute(select(TrainerEvaluation).where(TrainerEvaluation.session_id == 500))
            eval_rec = res.scalar()
            self.assertAlmostEqual(float(eval_rec.score), 9.5, places=2)
            sess = await db.get(TrainerSession, 500)
            self.assertEqual(sess.evaluation_status, "evaluated")


if __name__ == "__main__":
    unittest.main()
