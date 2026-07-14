"""
Unit tests for Trainer evaluation details, criteria mapping, score calculations.
Tests call TrainerService methods directly to avoid cross-session ORM issues.
Runs on a safe local SQLite database.
"""
import os
import sys

# Override DATABASE_URL BEFORE any imports
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///trainer_eval_test.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite compatibility shims for PostgreSQL-only types
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import BigInteger

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

import unittest
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine
from app.services.db_init_service import init_db
from app.services.trainer_service import TrainerService
from app.models.services import Service
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSession,
    TrainerEvaluation,
)
from app.models.personalized_training import TrainingAgentSetting


class TestTrainerEvaluationDetails(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        from app.db import Base
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        # Initialize DB (seeds default typologies/services where possible)
        await init_db()
        self.db = AsyncSession(self.engine, expire_on_commit=False)

        # 1. Ensure service exists (init_db may have seeded one already)
        res_svc = await self.db.execute(select(Service).where(Service.service_id == 1))
        self.service = res_svc.scalars().first()
        if not self.service:
            self.service = await self.db.merge(
                Service(service_id=1, service_key="front", service_name="Front Desk")
            )
            await self.db.flush()
        else:
            # Ensure name is what we expect for assertion
            self.service.service_name = "Front Desk"
            await self.db.flush()

        # 2. Create speech prompt structure with explicit ID
        self.prompt = Prompt(
            prompt_id=5099,
            prompt_name="Front evaluation structure",
            prompt_type="audio",
            is_active=True,
            service_id=1
        )
        self.db.add(self.prompt)
        await self.db.flush()

        # 3. Create active criteria
        self.crit_empatia = PromptCriterion(
            criterion_id=38001,
            prompt_id=5099,
            criterion_key="empatia",
            criterion_name="Empatía",
            criterion_description="Evalúa la empatía",
            criterion_type="score_1_10",
            output_key="empatia",
            feed_key="empatia_feed",
            is_active=True
        )
        self.crit_saludo = PromptCriterion(
            criterion_id=38002,
            prompt_id=5099,
            criterion_key="saludo",
            criterion_name="Saludo",
            criterion_description="Saludo inicial",
            criterion_type="boolean",
            output_key="saludo",
            is_active=True
        )
        self.crit_tipo = PromptCriterion(
            criterion_id=38003,
            prompt_id=5099,
            criterion_key="tipo_llamada",
            criterion_name="Tipo de llamada",
            criterion_description="Tipo de llamada de entrada",
            criterion_type="category",
            output_key="tipo_llamada",
            is_active=True
        )
        self.db.add_all([self.crit_empatia, self.crit_saludo, self.crit_tipo])
        await self.db.flush()

        # 4. Create agent setting
        self.agent_setting = TrainingAgentSetting(
            hubspot_owner_id="agent_123",
            agent_name="Cristina Montenegro",
            agent_initials="CM",
            training_code="CM77",
            training_numeric_code="777",
            is_enabled=True,
            training_code_enabled=True
        )
        self.db.add(self.agent_setting)

        # 5. Create evaluation config
        self.eval_config = TrainerEvaluationConfig(
            config_id=1099,
            name="Front Eval Config",
            service_id=1,
            speech_structure_id=5099,
            is_active=True
        )
        self.db.add(self.eval_config)
        await self.db.flush()

        # 6. Create simulation
        self.sim = TrainerSimulation(
            simulation_id=1099,
            name="Prueba1",
            code="323334",
            service_id=1,
            evaluation_config_id=1099,
            roleplay_prompt="Actúa como paciente",
            status="published"
        )
        self.db.add(self.sim)
        await self.db.flush()

        # ── Sessions ──────────────────────────────────────────────────────────────

        # Session A: Evaluated with direct numeric score
        self.session_a = TrainerSession(
            session_id=10101,
            simulation_id=1099,
            agent_id="agent_123",
            agent_code="CM77",
            service_id=1,
            call_id="call_a",
            status="completed",
            evaluation_status="evaluated",
            recording_url="http://api.twilio.com/Recordings/rec_a",
            transcript="Hola, soy Cristina."
        )
        self.db.add(self.session_a)
        await self.db.flush()

        self.eval_a = TrainerEvaluation(
            evaluation_id=20101,
            session_id=10101,
            evaluation_config_id=1099,
            prompt_snapshot="Snapshot prompt system",
            result_json={
                "empatia": 9,
                "empatia_feed": "Excelente trato.",
                "saludo": "si",
                "tipo_llamada": "cita"
            },
            score=Decimal("9.00"),
            summary="Trato muy empático."
        )
        self.db.add(self.eval_a)

        # Session B: Evaluated but direct score is null → average from criteria
        self.session_b = TrainerSession(
            session_id=10102,
            simulation_id=1099,
            agent_id="agent_123",
            agent_code="CM77",
            service_id=1,
            call_id="call_b",
            status="completed",
            evaluation_status="evaluated",
            recording_url="http://api.twilio.com/Recordings/rec_b",
            transcript="Hola."
        )
        self.db.add(self.session_b)
        await self.db.flush()

        self.eval_b = TrainerEvaluation(
            evaluation_id=20102,
            session_id=10102,
            evaluation_config_id=1099,
            prompt_snapshot="Snapshot",
            result_json={
                "empatia": 8,
                "empatia_feed": "Buen tono.",
                "saludo": "no",
                "tipo_llamada": "otros"
            },
            score=None,  # intentionally null
            summary="Sin score global directo."
        )
        self.db.add(self.eval_b)

        # Session C: No evaluation at all
        self.session_c = TrainerSession(
            session_id=10103,
            simulation_id=1099,
            agent_id="agent_123",
            agent_code="CM77",
            service_id=1,
            call_id="call_c",
            status="completed",
            evaluation_status="started"
        )
        self.db.add(self.session_c)

        # Session D: Partial (empty) evaluation JSON
        self.session_d = TrainerSession(
            session_id=10104,
            simulation_id=1099,
            agent_id="agent_123",
            agent_code="CM77",
            service_id=1,
            call_id="call_d",
            status="completed",
            evaluation_status="evaluated"
        )
        self.db.add(self.session_d)
        await self.db.flush()

        self.eval_d = TrainerEvaluation(
            evaluation_id=20104,
            session_id=10104,
            evaluation_config_id=1099,
            prompt_snapshot="Snapshot",
            result_json={},  # empty
            score=None
        )
        self.db.add(self.eval_d)
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        try:
            if os.path.exists("trainer_eval_test.db"):
                os.remove("trainer_eval_test.db")
        except Exception:
            pass

    # ── Test 1: list_sessions returns denormalized basic fields ──────────────────

    async def test_1_list_sessions_basic_denormalized_fields(self):
        sessions, total = await TrainerService.list_sessions(self.db)
        self.assertGreaterEqual(len(sessions), 4)

        s = next(x for x in sessions if x.session_id == 10101)
        self.assertEqual(s.__dict__["simulation_name"], "Prueba1")
        self.assertEqual(s.__dict__["simulation_code"], "323334")
        self.assertEqual(s.__dict__["service_name"], "Front Desk")
        self.assertEqual(s.__dict__["agent_name"], "Cristina Montenegro")
        self.assertEqual(s.agent_code, "CM77")

    # ── Test 2: get_session_detail returns denormalized basic fields ─────────────

    async def test_2_get_session_detail_basic_denormalized_fields(self):
        s = await TrainerService.get_session_detail(self.db, 10101)
        self.assertIsNotNone(s)
        self.assertEqual(s.__dict__["simulation_name"], "Prueba1")
        self.assertEqual(s.__dict__["simulation_code"], "323334")
        self.assertEqual(s.__dict__["service_name"], "Front Desk")
        self.assertEqual(s.__dict__["agent_name"], "Cristina Montenegro")

    # ── Test 3: Direct score returned from evaluation ────────────────────────────

    async def test_3_score_from_evaluation_score_field(self):
        s = await TrainerService.get_session_detail(self.db, 10101)
        self.assertEqual(s.__dict__["score"], 9.0)
        self.assertEqual(s.__dict__["score_max"], 10)
        self.assertEqual(s.__dict__["score_source"], "evaluation_score")
        self.assertEqual(s.__dict__["evaluation_summary"], "Trato muy empático.")

    # ── Test 4: Average calculation when direct score is null ────────────────────

    async def test_4_score_calculated_from_criteria_average(self):
        s = await TrainerService.get_session_detail(self.db, 10102)
        self.assertEqual(s.__dict__["score"], 8.0)
        self.assertEqual(s.__dict__["score_max"], 10)
        self.assertEqual(s.__dict__["score_source"], "criteria_average")

    # ── Test 5: Session without evaluation does not crash ────────────────────────

    async def test_5_session_without_evaluation_no_crash(self):
        s = await TrainerService.get_session_detail(self.db, 10103)
        self.assertIsNotNone(s)
        self.assertIsNone(s.__dict__["score"])
        self.assertEqual(s.__dict__["score_source"], "none")
        self.assertEqual(s.__dict__["criteria_scores"], [])

    # ── Test 6: Partial evaluation JSON does not crash ────────────────────────────

    async def test_6_partial_evaluation_json_no_crash(self):
        s = await TrainerService.get_session_detail(self.db, 10104)
        self.assertIsNotNone(s)
        self.assertIsNone(s.__dict__["score"])
        # criteria_scores should still contain all criteria, all with null values
        scores = s.__dict__["criteria_scores"]
        self.assertEqual(len(scores), 3)
        empatia = next(x for x in scores if x["output_key"] == "empatia")
        self.assertIsNone(empatia["score"])
        self.assertIsNone(empatia["value"])
        self.assertEqual(empatia["display_value"], "No evaluable")

    # ── Test 7 & 8: criteria_scores structure for score_1_10 ─────────────────────

    async def test_7_8_criteria_scores_score_1_10(self):
        s = await TrainerService.get_session_detail(self.db, 10101)
        scores = s.__dict__["criteria_scores"]
        self.assertEqual(len(scores), 3)

        empatia = next(x for x in scores if x["output_key"] == "empatia")
        self.assertEqual(empatia["criterion_name"], "Empatía")
        self.assertEqual(empatia["item_type"], "score_1_10")
        self.assertEqual(empatia["feed_key"], "empatia_feed")
        self.assertEqual(empatia["score"], 9.0)
        self.assertEqual(empatia["max_score"], 10)
        self.assertEqual(empatia["value"], 9.0)
        self.assertEqual(empatia["feedback"], "Excelente trato.")
        self.assertEqual(empatia["display_value"], "9/10")
        self.assertTrue(empatia["is_score"])

    # ── Test 9: boolean and category criteria ────────────────────────────────────

    async def test_9_criteria_boolean_and_category(self):
        s = await TrainerService.get_session_detail(self.db, 10101)
        scores = s.__dict__["criteria_scores"]

        # boolean
        saludo = next(x for x in scores if x["output_key"] == "saludo")
        self.assertEqual(saludo["item_type"], "boolean")
        self.assertEqual(saludo["value"], True)
        self.assertEqual(saludo["display_value"], "Sí")
        self.assertFalse(saludo["is_score"])
        self.assertIsNone(saludo["score"])

        # category
        tipo = next(x for x in scores if x["output_key"] == "tipo_llamada")
        self.assertEqual(tipo["item_type"], "category")
        self.assertEqual(tipo["value"], "cita")
        self.assertEqual(tipo["display_value"], "cita")
        self.assertFalse(tipo["is_score"])

    # ── Test 10: score_items / non_score_items / extraction_values ───────────────

    async def test_10_score_and_non_score_items(self):
        s = await TrainerService.get_session_detail(self.db, 10101)
        score_items = s.__dict__["score_items"]
        non_score_items = s.__dict__["non_score_items"]
        extraction = s.__dict__["extraction_values"]

        self.assertEqual(len(score_items), 1)        # only empatia
        self.assertEqual(len(non_score_items), 2)    # saludo + tipo_llamada
        self.assertIn("empatia", extraction)
        self.assertIn("saludo", extraction)
        self.assertIn("tipo_llamada", extraction)

    # ── Test 11: evaluation_config_id and speech_structure_id populated ──────────

    async def test_11_config_and_structure_meta(self):
        s = await TrainerService.get_session_detail(self.db, 10101)
        self.assertEqual(s.__dict__["evaluation_config_id"], 1099)
        self.assertEqual(s.__dict__["evaluation_config_name"], "Front Eval Config")
        self.assertEqual(s.__dict__["speech_structure_id"], 5099)

    # ── Test 12: call_status and transcription aliases populated ─────────────────

    async def test_12_alias_fields_call_status_transcription(self):
        s = await TrainerService.get_session_detail(self.db, 10101)
        self.assertEqual(s.__dict__["call_status"], "completed")
        self.assertEqual(s.__dict__["transcription"], "Hola, soy Cristina.")

    # ── Test 13: _map_trainer_criteria_scores unit mapping ───────────────────────

    async def test_13_map_criteria_scores_static_method(self):
        result_json = {
            "empatia": 7,
            "empatia_feed": "Buena empatía.",
            "saludo": "no",
            "tipo_llamada": "otros"
        }
        active_criteria = [self.crit_empatia, self.crit_saludo, self.crit_tipo]
        scores = TrainerService._map_trainer_criteria_scores(result_json, active_criteria)

        self.assertEqual(len(scores), 3)

        empatia = next(x for x in scores if x["output_key"] == "empatia")
        self.assertEqual(empatia["score"], 7.0)
        self.assertEqual(empatia["display_value"], "7/10")

        saludo = next(x for x in scores if x["output_key"] == "saludo")
        self.assertEqual(saludo["value"], False)
        self.assertEqual(saludo["display_value"], "No")

        tipo = next(x for x in scores if x["output_key"] == "tipo_llamada")
        self.assertEqual(tipo["value"], "otros")

    # ── Test 14: Empty result_json doesn't crash ─────────────────────────────────

    async def test_14_empty_result_json_no_crash(self):
        active_criteria = [self.crit_empatia, self.crit_saludo]
        scores = TrainerService._map_trainer_criteria_scores({}, active_criteria)
        self.assertEqual(len(scores), 2)
        for s in scores:
            self.assertIsNone(s["value"])
            self.assertEqual(s["display_value"], "No evaluable")


if __name__ == "__main__":
    unittest.main()
