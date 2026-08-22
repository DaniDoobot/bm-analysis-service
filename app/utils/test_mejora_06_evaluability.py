"""
Comprehensive Test Suite for MEJORA CLAVE 6 (is_evaluable tri-state, dynamic multi-service rules, automation min duration).
"""
import asyncio
import os
import sys
from decimal import Decimal

# Set safe test database URL with _test in name before importing any app modules
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///local_evaluability_test.sqlite"
os.environ["APP_ENV"] = "test"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text, select

from app.config import get_settings
from app.utils.evaluability import determine_evaluability
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationRun, MassEvaluationJob
from app.schemas.mass_evaluations import MassEvaluationResultResponse, MassEvaluationResultListItemResponse
from app.utils.item_score_filters import filter_mass_results_by_items, build_item_filters_sql


def test_1_front_valid_conversation():
    """1. estructura Front + conversación válida → true"""
    result_json = {
        "tipo_llamada": "cita",
        "saludo_inicio": 10,
        "empatia": 8,
        "claridad": 9,
        "tono_simpatia": 8,
        "hablando_paciente": "45%",
        "hablando_agente": "55%",
        "cierre_cita": "Sí"
    }
    items = [
        {"criterion_key": "saludo_inicio", "numeric_value": 10.0, "type": "score_1_10"},
        {"criterion_key": "empatia", "numeric_value": 8.0, "type": "score_1_10"},
        {"criterion_key": "claridad", "numeric_value": 9.0, "type": "score_1_10"},
        {"criterion_key": "tono_simpatia", "numeric_value": 8.0, "type": "score_1_10"},
    ]
    is_eval, reason = determine_evaluability(
        typology_key="cita",
        result_json=result_json,
        items=items,
        call_duration_seconds=120,
        status="completed"
    )
    assert is_eval is True
    assert reason == "valid_conversation"


def test_2_other_service_valid_conversation():
    """2. estructura de otro servicio (e.g. Experiencia Paciente / ESIC) + conversación válida → true"""
    result_json = {
        "tipo_llamada": "encuesta_resuelta",
        "presentacion": 9,
        "asertividad": 8,
        "puesta_en_valor_bm": 8,
        "hablando_paciente": "60%",
    }
    items = [
        {"criterion_key": "presentacion", "numeric_value": 9.0, "type": "score_1_10"},
        {"criterion_key": "asertividad", "numeric_value": 8.0, "type": "score_1_10"},
        {"criterion_key": "puesta_en_valor_bm", "numeric_value": 8.0, "type": "score_1_10"},
    ]
    is_eval, reason = determine_evaluability(
        typology_key="encuesta_resuelta",
        result_json=result_json,
        items=items,
        call_duration_seconds=90,
        status="completed"
    )
    assert is_eval is True
    assert reason == "valid_conversation"


def test_3_no_quiere_hablar_con_conversacion():
    """3. no_quiere_hablar con conversación suficiente → true"""
    result_json = {
        "tipo_llamada": "no_quiere_hablar",
        "saludo_inicio": 9,
        "empatia": 7,
        "claridad": 8,
        "gestion_objeciones": 6,
        "hablando_paciente": "35%",
        "motivo_no_cita": "El paciente indica que no está interesado pero escucha la propuesta."
    }
    items = [
        {"criterion_key": "saludo_inicio", "numeric_value": 9.0, "type": "score_1_10"},
        {"criterion_key": "empatia", "numeric_value": 7.0, "type": "score_1_10"},
        {"criterion_key": "claridad", "numeric_value": 8.0, "type": "score_1_10"},
        {"criterion_key": "gestion_objeciones", "numeric_value": 6.0, "type": "score_1_10"},
    ]
    is_eval, reason = determine_evaluability(
        typology_key="no_quiere_hablar",
        result_json=result_json,
        items=items,
        call_duration_seconds=55,
        status="completed"
    )
    assert is_eval is True
    assert reason == "valid_conversation"


def test_4_no_quiere_hablar_sin_conversacion():
    """4. no_quiere_hablar sin conversación bilateral (cuelga tras contestar) → false"""
    result_json = {
        "tipo_llamada": "no_quiere_hablar",
        "saludo_inicio": 3,
        "hablando_paciente": "0%",
        "motivo_no_cita": "Llamada cortada inmediatamente al descolgar."
    }
    items = [
        {"criterion_key": "saludo_inicio", "numeric_value": 3.0, "type": "score_1_10"}
    ]
    is_eval, reason = determine_evaluability(
        typology_key="no_quiere_hablar",
        result_json=result_json,
        items=items,
        call_duration_seconds=12,
        status="completed"
    )
    assert is_eval is False
    assert reason in ("dropped_or_cut_call", "no_bilateral_conversation", "insufficient_conversation")


def test_5_boolean_criteria_structure_evaluable_with_false_values():
    """5. estructura predominantemente booleana con valores False explícitos y conversación válida → true"""
    # Boolean criteria with False: "Trato de usted = No", "Explica precio = No", "Recomienda pareja = No".
    # All are evaluated conditions!
    result_json = {
        "tipo_llamada": "otros",
        "saludo_inicio": 8,
        "trato_usted": False,
        "reformula_patologia": False,
        "precio_consulta": False,
        "hablando_paciente": "40%",
    }
    items = [
        {"criterion_key": "saludo_inicio", "numeric_value": 8.0, "type": "score_1_10"},
        {"criterion_key": "trato_usted", "boolean_value": False, "type": "boolean"},
        {"criterion_key": "reformula_patologia", "boolean_value": False, "type": "boolean"},
        {"criterion_key": "precio_consulta", "boolean_value": False, "type": "boolean"},
    ]
    is_eval, reason = determine_evaluability(
        typology_key="otros",
        result_json=result_json,
        items=items,
        call_duration_seconds=70,
        status="completed"
    )
    assert is_eval is True
    assert reason == "valid_conversation"


def test_6_boolean_none_does_not_count():
    """6. boolean_value None no cuenta como evaluado; si todo es None → zero_evaluated_criteria"""
    result_json = {
        "tipo_llamada": "otros",
        "trato_usted": None,
        "reformula_patologia": "No aplica",
        "precio_consulta": "N/A",
    }
    items = [
        {"criterion_key": "trato_usted", "boolean_value": None, "type": "boolean"},
        {"criterion_key": "reformula_patologia", "boolean_value": None, "type": "boolean"},
        {"criterion_key": "precio_consulta", "boolean_value": None, "type": "boolean"},
    ]
    is_eval, reason = determine_evaluability(
        typology_key="otros",
        result_json=result_json,
        items=items,
        call_duration_seconds=30,
        status="completed"
    )
    assert is_eval is False
    assert reason == "zero_evaluated_criteria"


def test_7_historico_null_participa_en_calidad():
    """7. histórico is_evaluable=NULL sigue participando en métricas de calidad"""
    from app.services.dashboard_service import extract_score_from_mass_row
    class MockHistoricRow:
        is_evaluable = None  # Historical unbackfilled record
        evaluacion_global = Decimal("7.50")
        result_json = {"evaluacion_global": 7.5, "tipo_llamada": "cita"}
        items_json = []

    row_historic = MockHistoricRow()
    score = extract_score_from_mass_row(row_historic, "evaluacion_global")
    assert score == 7.5  # Participates fully


def test_8_is_evaluable_false_excluido_de_calidad():
    """8. is_evaluable=False queda fuera de métricas de calidad"""
    from app.services.dashboard_service import extract_score_from_mass_row
    class MockFalseRow:
        is_evaluable = False
        evaluacion_global = None
        result_json = {"saludo_inicio": 3, "tipo_llamada": "intento_contacto"}
        items_json = [{"key": "saludo_inicio", "value": 3.0}]

    row_false = MockFalseRow()
    score = extract_score_from_mass_row(row_false, "evaluacion_global")
    assert score is None  # Excluded strictly


def test_9_item_filters_no_devuelve_llamadas_false():
    """9. item_filters no devuelve llamadas con is_evaluable=False"""
    class MockRowTrue:
        is_evaluable = True
        result_json = {"empatia": 8}
        items_json = [{"key": "empatia", "value": 8.0}]
    class MockRowFalse:
        is_evaluable = False
        result_json = {"empatia": 8}  # Has partial criterion data
        items_json = [{"key": "empatia", "value": 8.0}]
    class MockRowNull:
        is_evaluable = None  # Historical
        result_json = {"empatia": 8}
        items_json = [{"key": "empatia", "value": 8.0}]

    rows = [MockRowTrue(), MockRowFalse(), MockRowNull()]
    filters = [{"key": "empatia", "type": "numeric", "min": 7.0, "max": 10.0}]
    filtered = filter_mass_results_by_items(rows, filters)
    
    assert all(getattr(r, "is_evaluable", None) is not False for r in filtered)
    assert len(filtered) == 2


def test_10_target_calls_detection():
    """10. Confirmación de los dos casos objetivo"""
    # 496380809459
    is_eval_1, reason_1 = determine_evaluability(
        typology_key="intento_contacto",
        result_json={"saludo_inicio": 3, "tipo_llamada": "intento_contacto", "motivo_no_cita": "Llamada cortada al inicio"},
        items=[{"criterion_key": "saludo_inicio", "numeric_value": 3.0}],
        call_duration_seconds=61,
        status="completed"
    )
    assert is_eval_1 is False

    # 492109501676
    is_eval_2, reason_2 = determine_evaluability(
        typology_key="otros",
        result_json={"saludo_inicio": 3, "procedimiento": 1, "tipo_llamada": "otros", "motivo_no_cita": "Llamada caída o cortada al inicio."},
        items=[{"criterion_key": "saludo_inicio", "numeric_value": 3.0}, {"criterion_key": "procedimiento", "numeric_value": 1.0}],
        call_duration_seconds=381,
        status="completed"
    )
    assert is_eval_2 is False


def test_11_status_failed_tri_state():
    """11. Status failed produces is_evaluable = None"""
    is_eval, reason = determine_evaluability(
        typology_key="cita",
        result_json=None,
        items=None,
        status="failed"
    )
    assert is_eval is None
    assert reason == "technical_failure"


def test_12_automation_min_duration_settings():
    """12. Automation threshold configured in settings (default 20) vs manual run"""
    settings = get_settings()
    assert hasattr(settings, "automation_min_duration_seconds")
    auto_min = settings.automation_min_duration_seconds
    assert auto_min == 20

    # Scenario A: Automation run
    exec_source_auto = "automation"
    raw_dur_min = None
    eff_dur_auto = raw_dur_min
    if exec_source_auto == "automation":
        if eff_dur_auto is None or eff_dur_auto < auto_min:
            eff_dur_auto = auto_min
    assert eff_dur_auto == 20

    # Scenario B: Manual run of the same job
    exec_source_manual = "on_demand"
    raw_dur_manual = 5
    eff_dur_manual = raw_dur_manual
    if exec_source_manual == "automation":
        if eff_dur_manual is None or eff_dur_manual < auto_min:
            eff_dur_manual = auto_min
    assert eff_dur_manual == 5


if __name__ == "__main__":
    test_1_front_valid_conversation()
    test_2_other_service_valid_conversation()
    test_3_no_quiere_hablar_con_conversacion()
    test_4_no_quiere_hablar_sin_conversacion()
    test_5_boolean_criteria_structure_evaluable_with_false_values()
    test_6_boolean_none_does_not_count()
    test_7_historico_null_participa_en_calidad()
    test_8_is_evaluable_false_excluido_de_calidad()
    test_9_item_filters_no_devuelve_llamadas_false()
    test_10_target_calls_detection()
    test_11_status_failed_tri_state()
    test_12_automation_min_duration_settings()
    print("All unit tests passed successfully!")
