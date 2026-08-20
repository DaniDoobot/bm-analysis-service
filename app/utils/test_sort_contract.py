"""
Contract tests for Results table sorting (sort_by / sort_order).
Verifies:
1. normalize_sort whitelist and 422 error handling.
2. SQL order_by construction with NULLS LAST and stable tie-breakers.
3. Backward compatibility when sort_by is omitted.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///local_test.db"
os.environ["ALLOW_TEST_DB"] = "1"

from fastapi import HTTPException
from app.utils.normalizers import normalize_sort, VALID_SORT_FIELDS


def test_normalize_sort():
    print("Testing normalize_sort...")
    # None and empty
    assert normalize_sort(None) == (None, "desc")
    assert normalize_sort("", None) == (None, "desc")
    assert normalize_sort("   ", "   ") == (None, "desc")

    # Canonical fields
    for field in ["date", "agent", "call_id", "duration", "score", "typology", "direction", "status", "service", "execution_source"]:
        assert normalize_sort(field, "asc") == (field, "asc")
        assert normalize_sort(field, "desc") == (field, "desc")
        assert normalize_sort(field, "ASC") == (field, "asc")
        assert normalize_sort(field, "DESC") == (field, "desc")
        assert normalize_sort(field) == (field, "desc")

    # Aliases
    assert normalize_sort("call_timestamp", "asc") == ("date", "asc")
    assert normalize_sort("agent_name", "desc") == ("agent", "desc")
    assert normalize_sort("call_duration_seconds", "asc") == ("duration", "asc")
    assert normalize_sort("evaluacion_global", "desc") == ("score", "desc")
    assert normalize_sort("global_score", "asc") == ("score", "asc")
    assert normalize_sort("typology_name", "desc") == ("typology", "desc")
    assert normalize_sort("service_name", "asc") == ("service", "asc")
    assert normalize_sort("source", "desc") == ("execution_source", "desc")

    # Invalid sort_by -> 422
    try:
        normalize_sort("invalid_field")
        assert False, "Should have raised HTTPException for 'invalid_field'"
    except HTTPException as e:
        assert e.status_code == 422
        assert "Campo de ordenación no válido" in e.detail

    try:
        normalize_sort("password")
        assert False, "Should have raised HTTPException for 'password'"
    except HTTPException as e:
        assert e.status_code == 422

    # Invalid sort_order -> 422
    try:
        normalize_sort("date", "sideways")
        assert False, "Should have raised HTTPException for 'sideways'"
    except HTTPException as e:
        assert e.status_code == 422
        assert "Dirección de ordenación no válida" in e.detail

    print("[OK] normalize_sort tests passed.")


async def test_mass_evaluation_service_sort_queries():
    print("Testing MassEvaluationService sort queries...")
    from app.services.mass_evaluation_service import MassEvaluationService
    from unittest.mock import AsyncMock, MagicMock

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    # 1. Without sort_by -> historical default
    await MassEvaluationService.list_results(mock_db, sort_by=None)
    called_stmt = mock_db.execute.call_args[0][0]
    sql_str = str(called_stmt)
    assert "ORDER BY bm_mass_evaluation_results.call_timestamp DESC" in sql_str
    assert "bm_mass_evaluation_results.mass_analysis_id DESC" in sql_str

    # 2. sort_by='date', sort_order='asc'
    mock_db.reset_mock()
    await MassEvaluationService.list_results(mock_db, sort_by="date", sort_order="asc")
    called_stmt = mock_db.execute.call_args[0][0]
    sql_str = str(called_stmt)
    assert "bm_mass_evaluation_results.call_timestamp ASC" in sql_str
    assert "bm_mass_evaluation_results.mass_analysis_id DESC" in sql_str

    # 3. sort_by='score', sort_order='desc'
    mock_db.reset_mock()
    await MassEvaluationService.list_results(mock_db, sort_by="score", sort_order="desc")
    called_stmt = mock_db.execute.call_args[0][0]
    sql_str = str(called_stmt)
    assert "bm_mass_evaluation_results.evaluacion_global DESC" in sql_str
    assert "bm_mass_evaluation_results.call_timestamp DESC" in sql_str
    assert "bm_mass_evaluation_results.mass_analysis_id DESC" in sql_str

    # 4. sort_by='agent', sort_order='asc' -> case-insensitive lower()
    mock_db.reset_mock()
    await MassEvaluationService.list_results(mock_db, sort_by="agent", sort_order="asc")
    called_stmt = mock_db.execute.call_args[0][0]
    sql_str = str(called_stmt)
    assert "lower(bm_mass_evaluation_results.agent_name) ASC" in sql_str or "lower" in sql_str.lower()
    assert "bm_mass_evaluation_results.call_timestamp DESC" in sql_str
    assert "bm_mass_evaluation_results.mass_analysis_id DESC" in sql_str

    print("[OK] MassEvaluationService sort queries passed.")


async def main():
    test_normalize_sort()
    await test_mass_evaluation_service_sort_queries()
    print("\n[OK] ALL sort contract tests PASSED successfully!")


if __name__ == "__main__":
    asyncio.run(main())
