# -*- coding: utf-8 -*-
"""
Tests for Boston Medical / EXPAC Typologies Regression Fix.

Validates:
TEST A — EXPAC exact match 'cita_trat' (not corrupted to 'cita')
TEST B — EXPAC exact match 'no_contacto' (not corrupted to 'intento_contacto')
TEST C — EXPAC exact match 'cancelacion_reagendo' (not corrupted to 'reagendo')
TEST D — Legacy Front synonyms still resolve properly (no regression for service_id=1)
TEST E — 'otros' priority order is always last in prompt conflict section
TEST F — 'devolucion_llamada_bot' is included in generated schema enum with 'otros' last
TEST G — Mass evaluation persistence resolves valid EXPAC typology (never NULL)
TEST H — Parity between Mass Evaluations and Laboratorio
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

# Force safe test database URL before importing any app modules
TEST_DB_FILE = "expac_test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_FILE}"
os.environ["APP_ENV"] = "test"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.analysis_persistence import (
    normalize_tipo_llamada,
    resolve_service_typology,
)
from app.services.prompt_builder import (
    sanitize_legacy_typologies_block,
    sanitize_inputs_completely,
    _build_output_format,
)
from app.models.criteria import PromptCriterion


class TypologyMock:
    def __init__(self, typology_id: int, key: str, name: str, sort_order: int = 0):
        self.typology_id = typology_id
        self.typology_key = key
        self.key = key
        self.typology_name = name
        self.sort_order = sort_order
        self.description = f"Llamada clasificada como {name}."


class TestExpacTypologiesRegression(unittest.TestCase):

    def setUp(self):
        # 13 Real EXPAC Typologies (service_id=2)
        self.expac_mock_list = [
            TypologyMock(9, "seguimiento_encuesta", "Seguimiento Encuesta"),
            TypologyMock(10, "solicitud", "Solicitud"),
            TypologyMock(11, "queja", "Queja"),
            TypologyMock(12, "otros", "Otros"),
            TypologyMock(26, "cita_trat", "Cita Trat"),
            TypologyMock(27, "no_contacto", "No Contacto"),
            TypologyMock(28, "cancelacion_reagendo", "Cancelacion Reagendo"),
            TypologyMock(29, "efectos_secundarios", "Efectos Secundarios"),
            TypologyMock(30, "hablar_con_medico", "Hablar Con Medico"),
            TypologyMock(31, "solicitud_medicacion", "Solicitud Medicacion"),
            TypologyMock(32, "incidencia_envio", "Incidencia Envio"),
            TypologyMock(33, "informacion_envio", "Informacion Envio"),
            TypologyMock(70, "devolucion_llamada_bot", "Devolucion Llamada Bot"),
        ]

        # In Mass Evaluations, typology_by_key is a dict of dicts
        self.expac_dict_by_key = {
            t.typology_key: {
                "typology_id": t.typology_id,
                "typology_key": t.typology_key,
                "typology_name": t.typology_name,
            }
            for t in self.expac_mock_list
        }

        # In Analysis persistence / Laboratorio, typology_by_key is a dict of ORM objects
        self.expac_orm_by_key = {
            t.typology_key: t for t in self.expac_mock_list
        }

        # Front Desk Typologies (service_id=1)
        self.front_mock_list = [
            TypologyMock(1, "cita", "Cita"),
            TypologyMock(2, "confirmacion", "Confirmación"),
            TypologyMock(3, "cancelacion", "Cancelación"),
            TypologyMock(4, "reagendo", "Reagendo"),
            TypologyMock(5, "falta", "Falta"),
            TypologyMock(6, "intento_contacto", "Intento de Contacto"),
            TypologyMock(7, "transferencia", "Transferencia"),
            TypologyMock(8, "otros", "Otros"),
        ]
        self.front_dict_by_key = {
            t.typology_key: {
                "typology_id": t.typology_id,
                "typology_key": t.typology_key,
                "typology_name": t.typology_name,
            }
            for t in self.front_mock_list
        }

    # -------------------------------------------------------------------------
    # TEST A: EXPAC exact match 'cita_trat'
    # -------------------------------------------------------------------------
    def test_a_expac_exact_match_cita_trat(self):
        """TEST A: 'cita_trat' must NOT be corrupted to 'cita' and must match EXPAC catalog."""
        # Demonstrate legacy normalizer would corrupt it
        legacy_result = normalize_tipo_llamada("cita_trat")
        self.assertEqual(legacy_result, "cita", "Legacy normalizer should turn 'cita_trat' to 'cita'")
        self.assertNotIn(legacy_result, self.expac_dict_by_key, "'cita' does not exist in EXPAC")

        # New resolver must preserve exact match
        matched, resolved_key = resolve_service_typology("cita_trat", self.expac_dict_by_key)
        self.assertIsNotNone(matched, "cita_trat should match in EXPAC")
        self.assertEqual(resolved_key, "cita_trat")
        self.assertEqual(matched["typology_id"], 26)
        self.assertEqual(matched["typology_key"], "cita_trat")
        self.assertEqual(matched["typology_name"], "Cita Trat")

        # Case-insensitive / whitespace match
        matched_ws, key_ws = resolve_service_typology("  CITA_TRAT \n", self.expac_dict_by_key)
        self.assertIsNotNone(matched_ws)
        self.assertEqual(key_ws, "cita_trat")

    # -------------------------------------------------------------------------
    # TEST B: EXPAC exact match 'no_contacto'
    # -------------------------------------------------------------------------
    def test_b_expac_exact_match_no_contacto(self):
        """TEST B: 'no_contacto' must NOT be corrupted to 'intento_contacto'."""
        legacy_result = normalize_tipo_llamada("no_contacto")
        self.assertEqual(legacy_result, "intento_contacto")
        self.assertNotIn(legacy_result, self.expac_dict_by_key)

        matched, resolved_key = resolve_service_typology("no_contacto", self.expac_dict_by_key)
        self.assertIsNotNone(matched)
        self.assertEqual(resolved_key, "no_contacto")
        self.assertEqual(matched["typology_id"], 27)
        self.assertEqual(matched["typology_key"], "no_contacto")

    # -------------------------------------------------------------------------
    # TEST C: EXPAC exact match 'cancelacion_reagendo'
    # -------------------------------------------------------------------------
    def test_c_expac_exact_match_cancelacion_reagendo(self):
        """TEST C: 'cancelacion_reagendo' must NOT be corrupted to 'reagendo'."""
        legacy_result = normalize_tipo_llamada("cancelacion_reagendo")
        self.assertEqual(legacy_result, "reagendo")
        self.assertNotIn(legacy_result, self.expac_dict_by_key)

        matched, resolved_key = resolve_service_typology("cancelacion_reagendo", self.expac_dict_by_key)
        self.assertIsNotNone(matched)
        self.assertEqual(resolved_key, "cancelacion_reagendo")
        self.assertEqual(matched["typology_id"], 28)
        self.assertEqual(matched["typology_key"], "cancelacion_reagendo")

    # -------------------------------------------------------------------------
    # TEST D: Legacy Front synonyms
    # -------------------------------------------------------------------------
    def test_d_legacy_front_synonyms(self):
        """TEST D: Legacy Front synonyms still resolve properly for service_id=1 without regression."""
        # 1. 'cambio de cita' -> 'reagendo'
        matched1, key1 = resolve_service_typology("cambio de cita", self.front_dict_by_key)
        self.assertIsNotNone(matched1)
        self.assertEqual(key1, "reagendo")
        self.assertEqual(matched1["typology_id"], 4)

        # 2. 'reprogramacion' -> 'reagendo'
        matched2, key2 = resolve_service_typology("reprogramacion", self.front_dict_by_key)
        self.assertIsNotNone(matched2)
        self.assertEqual(key2, "reagendo")

        # 3. 'confirmar asistencia' -> 'confirmacion'
        matched3, key3 = resolve_service_typology("confirmar asistencia", self.front_dict_by_key)
        self.assertIsNotNone(matched3)
        self.assertEqual(key3, "confirmacion")

        # 4. 'anulacion' -> 'cancelacion'
        matched4, key4 = resolve_service_typology("anulacion", self.front_dict_by_key)
        self.assertIsNotNone(matched4)
        self.assertEqual(key4, "cancelacion")

        # 5. Exact match on Front still works immediately
        matched5, key5 = resolve_service_typology("cita", self.front_dict_by_key)
        self.assertIsNotNone(matched5)
        self.assertEqual(key5, "cita")

    # -------------------------------------------------------------------------
    # TEST E: Priority of 'otros' is always last
    # -------------------------------------------------------------------------
    def test_e_priority_otros_always_last(self):
        """TEST E: 'otros' appears as the last item in the generated prompt conflict priorities."""
        prompt_template = (
            "### DEFINICIÓN DE TIPOS DE LLAMADA\n"
            "- legacy: algo\n\n"
            "### PRIORIDADES EN CASO DE CONFLICTO\n"
            "1. legacy\n\n"
            "### CRITERIOS DE ANÁLISIS\n"
        )
        sanitized = sanitize_legacy_typologies_block(prompt_template, self.expac_mock_list)

        # 1. Header and semantic text
        self.assertIn("### PRIORIDADES EN CASO DE CONFLICTO", sanitized)
        self.assertIn("Debes priorizar siempre las tipologías específicas", sanitized)
        self.assertIn("ÚNICAMENTE como último recurso", sanitized)

        # 2. 'otros' must be item #13 (last)
        lines = [line.strip() for line in sanitized.splitlines() if line.strip()]
        priority_lines = [
            line for line in lines
            if line[0].isdigit() and ". " in line
        ]
        self.assertEqual(len(priority_lines), 13)
        self.assertEqual(priority_lines[-1], "13. otros", "'otros' must be the last priority item")
        self.assertNotEqual(priority_lines[3], "4. otros", "'otros' must NOT be item #4")

        # Verify all specific clinical typologies come before 'otros'
        priority_text = "\n".join(priority_lines)
        idx_cita = priority_text.find("cita_trat")
        idx_efectos = priority_text.find("efectos_secundarios")
        idx_bot = priority_text.find("devolucion_llamada_bot")
        idx_otros = priority_text.find("otros")

        self.assertLess(idx_cita, idx_otros, "'cita_trat' must be prioritized before 'otros'")
        self.assertLess(idx_efectos, idx_otros, "'efectos_secundarios' must be prioritized before 'otros'")
        self.assertLess(idx_bot, idx_otros, "'devolucion_llamada_bot' must be prioritized before 'otros'")

    # -------------------------------------------------------------------------
    # TEST F: Schema includes 'devolucion_llamada_bot' with 'otros' last
    # -------------------------------------------------------------------------
    def test_f_schema_includes_devolucion_llamada_bot(self):
        """TEST F: 'devolucion_llamada_bot' is included in allowed_values and schema enum."""
        criterion = PromptCriterion(
            criterion_id=1,
            criterion_key="tipo_llamada",
            output_key="tipo_llamada",
            criterion_type="category",
            allowed_values=[],  # empty initially
        )

        _, cleaned_criteria, _ = sanitize_inputs_completely(
            draft_data=None,
            criteria=[criterion],
            active_typologies=self.expac_mock_list,
            criterion_typologies_map={},
        )

        allowed = cleaned_criteria[0].allowed_values
        self.assertIn("devolucion_llamada_bot", allowed)
        self.assertIn("cita_trat", allowed)
        self.assertIn("otros", allowed)
        self.assertEqual(allowed[-1], "otros", "'otros' must be the last allowed value")

        # Check JSON schema string generated
        schema_json = _build_output_format(cleaned_criteria, self.expac_mock_list)
        self.assertIn('"devolucion_llamada_bot"', schema_json)
        self.assertIn('"cita_trat"', schema_json)
        self.assertIn('"otros"|null', schema_json)

    # -------------------------------------------------------------------------
    # TEST G: Mass Evaluation Persistence
    # -------------------------------------------------------------------------
    def test_g_mass_evaluation_persistence_simulation(self):
        """TEST G: Simulate mass evaluation resolution with model output 'cita_trat' -> never NULL."""
        clean_result = {"tipo_llamada": "cita_trat", "evaluacion_global": 9.5}
        detected_typology_key_raw = clean_result.get("tipo_llamada")

        matched_typology, resolved_key = resolve_service_typology(
            detected_typology_key_raw, self.expac_dict_by_key
        )
        detected_typology_key = resolved_key or (
            normalize_tipo_llamada(detected_typology_key_raw)
            if detected_typology_key_raw
            else None
        )

        typology_id = matched_typology["typology_id"] if matched_typology else None
        typology_key = matched_typology["typology_key"] if matched_typology else None
        typology_name = matched_typology["typology_name"] if matched_typology else None

        self.assertIsNotNone(typology_id, "typology_id must NOT be NULL")
        self.assertEqual(typology_id, 26)
        self.assertEqual(typology_key, "cita_trat")
        self.assertEqual(typology_name, "Cita Trat")
        self.assertEqual(detected_typology_key, "cita_trat")

    # -------------------------------------------------------------------------
    # TEST H: Parity between Mass Evaluations and Laboratorio
    # -------------------------------------------------------------------------
    def test_h_mass_and_laboratorio_parity(self):
        """TEST H: Both Mass Evaluations and Laboratorio resolve the exact same typology for identical input."""
        test_inputs = [
            ("cita_trat", 26, "cita_trat"),
            ("no_contacto", 27, "no_contacto"),
            ("cancelacion_reagendo", 28, "cancelacion_reagendo"),
            ("efectos_secundarios", 29, "efectos_secundarios"),
            ("hablar_con_medico", 30, "hablar_con_medico"),
            ("solicitud_medicacion", 31, "solicitud_medicacion"),
            ("incidencia_envio", 32, "incidencia_envio"),
            ("informacion_envio", 33, "informacion_envio"),
            ("devolucion_llamada_bot", 70, "devolucion_llamada_bot"),
            ("otros", 12, "otros"),
        ]

        for raw_val, expected_id, expected_key in test_inputs:
            # Mass evaluation path (dict catalog)
            mass_matched, mass_key = resolve_service_typology(raw_val, self.expac_dict_by_key)
            self.assertIsNotNone(mass_matched, f"Mass path failed for {raw_val}")
            self.assertEqual(mass_matched["typology_id"], expected_id)
            self.assertEqual(mass_key, expected_key)

            # Laboratorio path (ORM catalog)
            lab_matched, lab_key = resolve_service_typology(raw_val, self.expac_orm_by_key)
            self.assertIsNotNone(lab_matched, f"Laboratorio path failed for {raw_val}")
            self.assertEqual(lab_matched.typology_id, expected_id)
            self.assertEqual(lab_key, expected_key)

            # Full parity assertion
            self.assertEqual(mass_key, lab_key)
            self.assertEqual(mass_matched["typology_id"], lab_matched.typology_id)
            self.assertEqual(mass_matched["typology_name"], lab_matched.typology_name)

    @classmethod
    def tearDownClass(cls):
        try:
            if os.path.exists(TEST_DB_FILE):
                os.remove(TEST_DB_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
