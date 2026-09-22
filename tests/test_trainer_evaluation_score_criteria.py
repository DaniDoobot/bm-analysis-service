"""
Tests for trainer evaluation score/criteria fixes (PR: fix(training): evaluate with active criteria).

Covers:
  1. _extract_robust_score — handles 8.5/10, "8 sobre 10", nested, criteria average fallback
  2. Empty active criteria → completed_without_score (no fabrication)
  3. evaluate_session_task builds prompt with active criteria and schema
  4. evaluate_session_task produces score + criteria_evaluations and persists them
  5. _map_session_evaluation_details populates criteria_scores + evidence from criteria_evaluations
  6. _map_trainer_criteria_scores resolves from criteria_evaluations when flat key is missing
"""
import os
import asyncio
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock, AsyncMock

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test_trainer_score_criteria.db")

from sqlalchemy import BigInteger, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import AsyncSession

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from app.db import Base, get_engine
from app.services.trainer_service import TrainerService


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_criterion(
    criterion_id=1,
    criterion_name="Saludo e Identificación",
    criterion_key="saludo_identificacion",
    output_key="saludo_identificacion_score",
    feed_key="saludo_identificacion_feedback",
    criterion_type="score_1_10",
    criterion_description="El agente saluda y se identifica correctamente.",
    is_active=True,
    deleted_at=None,
    order_index=1,
):
    c = MagicMock()
    c.criterion_id = criterion_id
    c.criterion_name = criterion_name
    c.criterion_key = criterion_key
    c.output_key = output_key
    c.feed_key = feed_key
    c.criterion_type = criterion_type
    c.criterion_description = criterion_description
    c.is_active = is_active
    c.deleted_at = deleted_at
    c.order_index = order_index
    return c


def _make_criterion_bool(
    criterion_id=2,
    criterion_name="Confirmación de Acuerdo",
    criterion_key="confirmacion_acuerdo",
    output_key="confirmacion_acuerdo_ok",
    feed_key="confirmacion_acuerdo_feedback",
    criterion_type="boolean",
    criterion_description="El agente confirma el acuerdo con el paciente.",
):
    c = MagicMock()
    c.criterion_id = criterion_id
    c.criterion_name = criterion_name
    c.criterion_key = criterion_key
    c.output_key = output_key
    c.feed_key = feed_key
    c.criterion_type = criterion_type
    c.criterion_description = criterion_description
    c.is_active = True
    c.deleted_at = None
    c.order_index = 2
    return c


# ─── 1. _extract_robust_score ────────────────────────────────────────────────

class TestExtractRobustScore(unittest.TestCase):
    """Tests for TrainerService._extract_robust_score."""

    def _call(self, parsed_res, criteria_evals=None):
        return TrainerService._extract_robust_score(parsed_res, criteria_evals)

    def test_plain_numeric(self):
        result = self._call({"score": 8.5})
        self.assertEqual(result, Decimal("8.5"))

    def test_string_slash_10(self):
        result = self._call({"score": "8.5/10"})
        self.assertEqual(result, Decimal("8.5"))

    def test_string_space_slash_10(self):
        result = self._call({"score": "7 / 10"})
        self.assertEqual(result, Decimal("7.0"))

    def test_string_sobre_10(self):
        result = self._call({"score": "8 sobre 10"})
        self.assertEqual(result, Decimal("8.0"))

    def test_string_de_10(self):
        result = self._call({"score": "9 de 10"})
        self.assertEqual(result, Decimal("9.0"))

    def test_comma_decimal_separator(self):
        result = self._call({"score": "7,5"})
        self.assertEqual(result, Decimal("7.5"))

    def test_evaluacion_global_key(self):
        result = self._call({"evaluacion_global": 6.0})
        self.assertEqual(result, Decimal("6.0"))

    def test_nested_evaluacion_dict(self):
        result = self._call({"evaluacion": {"score": 9.0}})
        self.assertEqual(result, Decimal("9.0"))

    def test_percentage_score_normalizes(self):
        # 85 is >10 and ≤100, should be divided by 10
        result = self._call({"score": 85})
        self.assertEqual(result, Decimal("8.5"))

    def test_fallback_to_criteria_average(self):
        """If no top-level score, average the criteria scores."""
        parsed = {"feedback": "bien"}
        criteria_evals = [
            {"criterion_key": "a", "score": 8.0},
            {"criterion_key": "b", "score": 6.0},
        ]
        result = self._call(parsed, criteria_evals)
        self.assertEqual(result, Decimal("7.0"))

    def test_no_score_returns_none(self):
        result = self._call({"feedback": "sin puntuación"})
        self.assertIsNone(result)

    def test_empty_dict_returns_none(self):
        result = self._call({})
        self.assertIsNone(result)

    def test_none_input_returns_none(self):
        result = self._call(None)
        self.assertIsNone(result)

    def test_score_exactly_10(self):
        result = self._call({"score": 10})
        self.assertEqual(result, Decimal("10.0"))

    def test_score_zero(self):
        result = self._call({"score": 0})
        self.assertEqual(result, Decimal("0.0"))


# ─── 2. Empty criteria → completed_without_score ─────────────────────────────

class TestEmptyCriteriaHandling(unittest.IsolatedAsyncioTestCase):
    """Session with no active criteria must be marked completed_without_score, not call AI."""

    async def test_empty_criteria_marks_completed_without_score(self):
        # Mock DB objects
        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        # Build mock session
        mock_sess = MagicMock()
        mock_sess.session_id = 42
        mock_sess.evaluation_status = "started"
        mock_sess.recording_url = None
        mock_sess.transcript = "Hola, soy María de Boston Medical Group. ¿Cómo estás?"
        mock_sess.simulation_id = 10
        mock_sess.simulation_version_id = None

        # Build mock simulation
        mock_sim = MagicMock()
        mock_sim.simulation_id = 10
        mock_sim.name = "ATEN01"
        mock_sim.objective = "Gestionar reclamación"
        mock_sim.difficulty = "media"
        mock_sim.roleplay_prompt = "Paciente confundido"
        mock_sim.evaluation_config_id = 5

        # Build mock config
        mock_cfg = MagicMock()
        mock_cfg.config_id = 5
        mock_cfg.speech_structure_id = 20
        mock_cfg.extra_instructions = ""

        # Build mock prompt_version
        mock_prompt_version = MagicMock()
        mock_prompt_version.prompt = "Evalúa al agente humano en la llamada."

        # Simulate DB execute results
        def make_execute_result(value=None, scalars_list=None):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = scalars_list if scalars_list is not None else []
            return r

        execute_calls = iter([
            make_execute_result(mock_sess),            # fetch session
            # NO version query (simulation_version_id is None)
            make_execute_result(mock_sim),             # fetch simulation
            make_execute_result(mock_cfg),             # fetch config
            make_execute_result(mock_prompt_version),  # fetch prompt_version
            # empty criteria list
            make_execute_result(scalars_list=[]),
        ])
        mock_db.execute = AsyncMock(side_effect=lambda stmt: next(execute_calls))

        # Ensure openai_service is NOT called
        with patch("app.services.trainer_service.openai_service.complete_text") as mock_ai:
            await TrainerService.evaluate_session_task(mock_db, 42)
            mock_ai.assert_not_called()

        # Session must be marked completed_without_score
        self.assertEqual(mock_sess.evaluation_status, "completed_without_score")
        # Must persist a TrainerEvaluation record
        mock_db.add.assert_called_once()
        added = mock_db.add.call_args[0][0]
        self.assertIsNone(added.score)
        self.assertIn("no_active_criteria_configured", added.result_json.get("error", ""))


# ─── 3. Prompt builder includes active criteria and response schema ──────────

class TestPromptBuilderIncludesCriteria(unittest.IsolatedAsyncioTestCase):
    """evaluate_session_task must inject criterion names/keys into prompt sent to AI."""

    async def test_prompt_contains_criterion_names_and_schema(self):
        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        mock_sess = MagicMock()
        mock_sess.session_id = 55
        mock_sess.evaluation_status = "started"
        mock_sess.recording_url = None
        mock_sess.transcript = "Buenos días, soy Carlos de Boston Medical Group."
        mock_sess.simulation_id = 11
        mock_sess.simulation_version_id = None

        mock_sim = MagicMock()
        mock_sim.simulation_id = 11
        mock_sim.name = "ATEN02"
        mock_sim.objective = "Cierre de venta"
        mock_sim.difficulty = "alta"
        mock_sim.roleplay_prompt = "Paciente interesado pero con dudas de precio"
        mock_sim.evaluation_config_id = 6

        mock_cfg = MagicMock()
        mock_cfg.config_id = 6
        mock_cfg.speech_structure_id = 21
        mock_cfg.extra_instructions = "Sé estricto con el saludo."

        mock_prompt_version = MagicMock()
        mock_prompt_version.prompt = "Estructura base de evaluación BMG."

        crit1 = _make_criterion(
            criterion_id=1,
            criterion_name="Saludo e Identificación",
            output_key="saludo_identificacion_score",
        )
        crit2 = _make_criterion(
            criterion_id=2,
            criterion_name="Presentación de Servicio",
            criterion_key="presentacion_servicio",
            output_key="presentacion_servicio_score",
        )

        ai_response = '{"score": 8.0, "feedback": "Buen desempeño", "strengths": [], "improvement_points": [], "result_json": {}, "criteria_evaluations": [{"criterion_key": "saludo_identificacion", "criterion_name": "Saludo e Identificación", "score": 8.0, "passed": true, "expected_behavior": "Saludar", "observed_behavior": "Saludó", "evidence_quote": "Buenos días...", "relevant_turns": [1], "reasoning": "Correcto", "improvement_tip": "Continúa"}]}'

        def make_exec(value=None, scalars_list=None):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = scalars_list if scalars_list is not None else []
            return r

        execute_calls = iter([
            make_exec(mock_sess),                    # fetch session
            # NO version query (simulation_version_id is None)
            make_exec(mock_sim),                     # fetch simulation
            make_exec(mock_cfg),                     # fetch config
            make_exec(mock_prompt_version),          # fetch prompt_version
            make_exec(scalars_list=[crit1, crit2]),  # fetch active_criteria
        ])
        mock_db.execute = AsyncMock(side_effect=lambda stmt: next(execute_calls))

        captured_messages = []

        async def capture_ai(messages, response_format=None):
            captured_messages.extend(messages)
            return ai_response

        with patch("app.services.trainer_service.openai_service.complete_text", side_effect=capture_ai):
            await TrainerService.evaluate_session_task(mock_db, 55)

        self.assertTrue(len(captured_messages) > 0, "AI must be called with messages")
        system_content = next(
            (m["content"] for m in captured_messages if m["role"] == "system"), ""
        )
        # Criterion names must be present in the system prompt
        self.assertIn("Saludo e Identificación", system_content)
        self.assertIn("Presentación de Servicio", system_content)
        # criteria_evaluations schema directive must be present
        self.assertIn("criteria_evaluations", system_content)
        # output_key must be referenced
        self.assertIn("saludo_identificacion_score", system_content)


# ─── 4. evaluate_session_task produces score + criteria_evaluations ──────────

class TestEvaluateSessionTaskPersistsScoreAndCriteria(unittest.IsolatedAsyncioTestCase):
    """Full evaluate_session_task flow: verify TrainerEvaluation is persisted with score and criteria_evaluations."""

    async def test_produces_score_and_criteria_evaluations(self):
        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        mock_sess = MagicMock()
        mock_sess.session_id = 77
        mock_sess.evaluation_status = "started"
        mock_sess.recording_url = None
        mock_sess.transcript = "Hola, buenos días, soy Laura de BMG. Llamo para hacer seguimiento a su consulta."
        mock_sess.simulation_id = 12
        mock_sess.simulation_version_id = None

        mock_sim = MagicMock()
        mock_sim.simulation_id = 12
        mock_sim.name = "ATEN01"
        mock_sim.objective = "Seguimiento paciente"
        mock_sim.difficulty = "media"
        mock_sim.roleplay_prompt = "Paciente con reclamación de facturación"
        mock_sim.evaluation_config_id = 7

        mock_cfg = MagicMock()
        mock_cfg.config_id = 7
        mock_cfg.speech_structure_id = 30
        mock_cfg.extra_instructions = ""

        mock_prompt_version = MagicMock()
        mock_prompt_version.prompt = "Evalúa rigurosamente al agente humano."

        crit1 = _make_criterion(
            criterion_id=10,
            criterion_name="Saludo e Identificación",
            criterion_key="saludo",
            output_key="saludo_score",
            feed_key="saludo_feedback",
        )

        ai_json_response = """{
            "score": 7.5,
            "feedback": "El agente mantuvo buena actitud durante toda la llamada.",
            "strengths": ["Empatía con el paciente", "Tono calmado"],
            "improvement_points": ["Mejorar la propuesta de valor"],
            "result_json": {"saludo_score": 7.5},
            "criteria_evaluations": [
                {
                    "criterion_key": "saludo",
                    "criterion_name": "Saludo e Identificación",
                    "score": 7.5,
                    "passed": true,
                    "expected_behavior": "Saludar con nombre y empresa",
                    "observed_behavior": "Dijo: Hola buenos días soy Laura de BMG",
                    "evidence_quote": "soy Laura de BMG",
                    "relevant_turns": [1],
                    "reasoning": "El agente se presentó correctamente.",
                    "improvement_tip": "Podría añadir número de referencia del caso."
                }
            ]
        }"""

        def make_exec(value=None, scalars_list=None):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = scalars_list if scalars_list is not None else []
            return r

        execute_calls = iter([
            make_exec(mock_sess),            # fetch session
            # NO version query (simulation_version_id is None)
            make_exec(mock_sim),             # fetch simulation
            make_exec(mock_cfg),             # fetch config
            make_exec(mock_prompt_version),  # fetch prompt_version
            make_exec(scalars_list=[crit1]), # fetch active_criteria
        ])
        mock_db.execute = AsyncMock(side_effect=lambda stmt: next(execute_calls))

        with patch("app.services.trainer_service.openai_service.complete_text", return_value=ai_json_response):
            await TrainerService.evaluate_session_task(mock_db, 77)

        # Session must be "evaluated"
        self.assertEqual(mock_sess.evaluation_status, "evaluated")

        # TrainerEvaluation must have been added
        mock_db.add.assert_called_once()
        eval_record = mock_db.add.call_args[0][0]

        # Score persisted
        self.assertEqual(eval_record.score, Decimal("7.5"))

        # criteria_evaluations in result_json
        crit_evals = eval_record.result_json.get("criteria_evaluations", [])
        self.assertIsInstance(crit_evals, list)
        self.assertGreater(len(crit_evals), 0)

        first_ce = crit_evals[0]
        self.assertEqual(first_ce["criterion_name"], "Saludo e Identificación")
        self.assertIsNotNone(first_ce.get("score"))
        self.assertIsNotNone(first_ce.get("evidence_quote"))
        self.assertIsNotNone(first_ce.get("expected_behavior"))
        self.assertIsNotNone(first_ce.get("observed_behavior"))
        self.assertIsNotNone(first_ce.get("reasoning"))

        # summary
        self.assertIsNotNone(eval_record.summary)


# ─── 5. _map_session_evaluation_details populates criteria_evaluations ───────

class TestMapSessionEvaluationDetails(unittest.IsolatedAsyncioTestCase):
    """_map_session_evaluation_details must populate criteria_evaluations from result_json."""

    async def test_criteria_evaluations_populated(self):
        mock_db = AsyncMock(spec=AsyncSession)

        crit_evals_data = [
            {
                "criterion_key": "saludo",
                "criterion_name": "Saludo e Identificación",
                "score": 8.0,
                "passed": True,
                "expected_behavior": "Saludar con nombre y empresa",
                "observed_behavior": "Se presentó correctamente",
                "evidence_quote": "Buenos días, soy Laura de BMG",
                "relevant_turns": [1],
                "reasoning": "Saludo correcto y completo",
                "improvement_tip": "Incluir referencia del caso",
            }
        ]
        result_json = {
            "score": 8.0,
            "feedback": "Buen desempeño",
            "criteria_evaluations": crit_evals_data,
            "saludo_score": 8.0,
        }

        mock_evaluation = MagicMock()
        mock_evaluation.evaluation_config_id = 7
        mock_evaluation.result_json = result_json
        mock_evaluation.summary = "Evaluación completa del agente"
        mock_evaluation.score = Decimal("8.0")

        mock_session = MagicMock()
        mock_session.status = "completed"
        mock_session.transcript = "Buenos días..."
        mock_session.evaluation = mock_evaluation
        mock_session.evaluation_status = "evaluated"
        mock_session.simulation = MagicMock()
        mock_session.simulation.evaluation_config_id = 7

        crit1 = _make_criterion(
            criterion_id=10,
            criterion_name="Saludo e Identificación",
            criterion_key="saludo",
            output_key="saludo_score",
        )

        mock_cfg = MagicMock()
        mock_cfg.config_id = 7
        mock_cfg.speech_structure_id = 30
        mock_cfg.name = "Config ATEN"
        mock_cfg.speech_structure_name = "Estructura BMG"

        def make_exec(value=None, scalars_list=None):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = scalars_list or []
            return r

        execute_calls = iter([
            make_exec(mock_cfg),
            make_exec(scalars_list=[crit1]),
        ])
        mock_db.execute = AsyncMock(side_effect=lambda stmt: next(execute_calls))

        await TrainerService._map_session_evaluation_details(mock_db, mock_session)

        # criteria_evaluations must be populated
        ce = mock_session.__dict__.get("criteria_evaluations", [])
        self.assertIsInstance(ce, list)
        self.assertEqual(len(ce), 1)
        self.assertEqual(ce[0]["criterion_name"], "Saludo e Identificación")
        self.assertEqual(ce[0]["evidence_quote"], "Buenos días, soy Laura de BMG")

        # Score must be resolved
        self.assertEqual(mock_session.__dict__["score"], 8.0)
        self.assertEqual(mock_session.__dict__["score_source"], "evaluation_score")

        # criteria_scores must be populated
        cs = mock_session.__dict__.get("criteria_scores", [])
        self.assertGreater(len(cs), 0)
        first_cs = cs[0]
        # Should not be "No evaluable" when criteria_evaluations has the data
        self.assertNotEqual(first_cs.get("display_value"), "No evaluable")


# ─── 6. _map_trainer_criteria_scores resolves from criteria_evaluations ──────

class TestMapTrainerCriteriaScoresResolvesFromCritEvals(unittest.TestCase):
    """_map_trainer_criteria_scores must look up criteria_evaluations when flat output_key is absent."""

    def test_resolves_score_from_criteria_evaluations_not_no_evaluable(self):
        crit1 = _make_criterion(
            criterion_id=1,
            criterion_name="Saludo e Identificación",
            criterion_key="saludo",
            output_key="saludo_score",
            feed_key="saludo_feedback",
            criterion_type="score_1_10",
        )

        # result_json has criteria_evaluations but no flat "saludo_score" key
        result_json = {
            "score": 8.5,
            "criteria_evaluations": [
                {
                    "criterion_key": "saludo",
                    "criterion_name": "Saludo e Identificación",
                    "score": 8.5,
                    "passed": True,
                    "expected_behavior": "Saludar formalmente",
                    "observed_behavior": "Saludó de forma profesional",
                    "evidence_quote": "Buenos días, soy Laura de BMG",
                    "relevant_turns": [1],
                    "reasoning": "Saludo correcto",
                    "improvement_tip": "Sin observaciones",
                }
            ]
        }

        scores = TrainerService._map_trainer_criteria_scores(result_json, [crit1])
        self.assertEqual(len(scores), 1)
        first = scores[0]

        # Must NOT be "No evaluable"
        self.assertNotEqual(first["display_value"], "No evaluable")
        self.assertIsNotNone(first["score"])
        self.assertEqual(first["score"], 8.5)
        self.assertEqual(first["display_value"], "8.5/10")

        # Rich evidence fields must be populated from criteria_evaluations
        self.assertEqual(first["expected_behavior"], "Saludar formalmente")
        self.assertEqual(first["observed_behavior"], "Saludó de forma profesional")
        self.assertEqual(first["evidence_quote"], "Buenos días, soy Laura de BMG")
        self.assertEqual(first["reasoning"], "Saludo correcto")

    def test_boolean_criterion_resolves_from_criteria_evaluations(self):
        crit_bool = _make_criterion_bool(
            criterion_id=2,
            criterion_name="Confirmación de Acuerdo",
            criterion_key="confirmacion_acuerdo",
            output_key="confirmacion_acuerdo_ok",
            feed_key="confirmacion_acuerdo_feedback",
            criterion_type="boolean",
        )

        result_json = {
            "criteria_evaluations": [
                {
                    "criterion_key": "confirmacion_acuerdo",
                    "criterion_name": "Confirmación de Acuerdo",
                    "score": None,
                    "passed": True,
                    "expected_behavior": "Confirmar el acuerdo verbalmente",
                    "observed_behavior": "Confirmó el acuerdo",
                    "evidence_quote": "Perfecto, quedamos así entonces",
                    "relevant_turns": [8],
                    "reasoning": "El agente confirmó explícitamente",
                    "improvement_tip": "",
                }
            ]
        }

        scores = TrainerService._map_trainer_criteria_scores(result_json, [crit_bool])
        self.assertEqual(len(scores), 1)
        first = scores[0]

        self.assertNotEqual(first["display_value"], "No evaluable")
        self.assertIn(first["display_value"], ("Sí", "No"))
        self.assertEqual(first["value"], True)

    def test_flat_key_still_works_when_present(self):
        """Flat output_key values still take priority when present."""
        crit1 = _make_criterion(
            criterion_id=1,
            criterion_name="Saludo e Identificación",
            criterion_key="saludo",
            output_key="saludo_score",
            criterion_type="score_1_10",
        )

        result_json = {
            "saludo_score": 9.0,  # flat key present
            "criteria_evaluations": [
                {
                    "criterion_key": "saludo",
                    "criterion_name": "Saludo e Identificación",
                    "score": 7.0,  # different from flat key
                    "passed": True,
                    "expected_behavior": "Saludar",
                    "observed_behavior": "Saludó",
                    "evidence_quote": "Hola",
                    "relevant_turns": [1],
                    "reasoning": "OK",
                    "improvement_tip": "",
                }
            ]
        }

        scores = TrainerService._map_trainer_criteria_scores(result_json, [crit1])
        first = scores[0]
        # Flat key takes priority: should be 9.0
        self.assertEqual(first["score"], 9.0)
        self.assertEqual(first["display_value"], "9/10")

    def test_no_criteria_returns_empty_list(self):
        result = TrainerService._map_trainer_criteria_scores({}, [])
        self.assertEqual(result, [])

    def test_no_result_json_returns_no_evaluable(self):
        crit1 = _make_criterion(
            criterion_id=1,
            criterion_name="Saludo e Identificación",
            criterion_key="saludo",
            output_key="saludo_score",
        )
        scores = TrainerService._map_trainer_criteria_scores(None, [crit1])
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["display_value"], "No evaluable")


if __name__ == "__main__":
    unittest.main()
