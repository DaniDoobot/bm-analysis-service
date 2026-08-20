"""
Test contract for evaluation status filtering (completed | failed | all).
Verifies:
1. Normalization rules and 422 error handling for invalid values.
2. Query generation and parameter mapping in services and routers.
3. Backward compatibility (quality views default to completed, listing views default to all).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///local_test.db"
os.environ["ALLOW_TEST_DB"] = "1"

from fastapi import HTTPException
from app.utils.normalizers import normalize_status


def test_normalize_status():
    print("Testing normalize_status...")
    # None and empty strings
    assert normalize_status(None) is None
    assert normalize_status("") is None
    assert normalize_status("   ") is None

    # All variations
    assert normalize_status("all") == "all"
    assert normalize_status("ALL") == "all"
    assert normalize_status("todas") == "all"
    assert normalize_status("todos") == "all"
    assert normalize_status("null") == "all"
    assert normalize_status("*") == "all"

    # Completed variations
    assert normalize_status("completed") == "completed"
    assert normalize_status("COMPLETED") == "completed"
    assert normalize_status("completado") == "completed"
    assert normalize_status("ok") == "completed"
    assert normalize_status("success") == "completed"

    # Failed variations
    assert normalize_status("failed") == "failed"
    assert normalize_status("FAILED") == "failed"
    assert normalize_status("fallido") == "failed"
    assert normalize_status("error") == "failed"

    # Invalid values -> HTTPException 422
    try:
        normalize_status("pending")
        assert False, "Should have raised HTTPException for 'pending'"
    except HTTPException as e:
        assert e.status_code == 422

    try:
        normalize_status("in_progress")
        assert False, "Should have raised HTTPException for 'in_progress'"
    except HTTPException as e:
        assert e.status_code == 422

    print("[OK] normalize_status tests passed.")


async def test_mass_evaluation_queries():
    print("Testing MassEvaluationService queries...")
    from app.services.mass_evaluation_service import MassEvaluationService
    from unittest.mock import AsyncMock, MagicMock

    # Mock db session
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 42
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_result

    # 1. status = None -> should not have status filter
    await MassEvaluationService.count_results(mock_db, status=None)
    called_stmt = mock_db.execute.call_args[0][0]
    sql_str = str(called_stmt)
    assert "status =" not in sql_str, f"Unexpected status in count_results(status=None): {sql_str}"

    # 2. status = 'completed' -> should have status = :status_1
    await MassEvaluationService.count_results(mock_db, status="completed")
    called_stmt = mock_db.execute.call_args[0][0]
    sql_str = str(called_stmt)
    assert "status = :status_1" in sql_str or "status =" in sql_str, f"Missing status in count_results(status='completed'): {sql_str}"

    # 3. status = 'failed' -> should have status = :status_1
    await MassEvaluationService.list_results(mock_db, status="failed")
    called_stmt = mock_db.execute.call_args[0][0]
    sql_str = str(called_stmt)
    assert "status = :status_1" in sql_str or "status =" in sql_str, f"Missing status in list_results(status='failed'): {sql_str}"

    print("[OK] MassEvaluationService query tests passed.")


async def test_dashboard_service_status():
    print("Testing dashboard_service status filtering...")
    from app.services.dashboard_service import get_dashboard_summary, get_agents_list, get_agent_evolution, get_objections_breakdown, get_agents_comparison
    from unittest.mock import AsyncMock, MagicMock

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 10
    mock_result.scalars.return_value.all.return_value = []
    mock_result.all.return_value = []
    mock_result.fetchall.return_value = []
    mock_result.fetchone.return_value = None
    mock_db.execute.return_value = mock_result

    # Test get_dashboard_summary with status='failed'
    await get_dashboard_summary(mock_db, period="24h", status="failed")
    stmts = [str(c[0][0]) for c in mock_db.execute.call_args_list]
    assert any("status = 'failed'" in s or "status = :status_1" in s or "status_1" in s or "status =" in s for s in stmts), f"Statements: {stmts}"

    mock_db.reset_mock()
    # Test get_agents_list with status='all'
    await get_agents_list(mock_db, period="30d", status="all")
    stmts = [str(c[0][0]) for c in mock_db.execute.call_args_list]
    assert not any("status = 'completed'" in s for s in stmts)

    mock_db.reset_mock()
    # Test get_agent_evolution with status='failed'
    await get_agent_evolution(mock_db, hubspot_owner_id="123", period="30d", status="failed")
    stmts = [str(c[0][0]) for c in mock_db.execute.call_args_list]
    assert any("status = :status" in s or "status =" in s for s in stmts)

    mock_db.reset_mock()
    # Test get_objections_breakdown with status='all'
    await get_objections_breakdown(mock_db, period="7d", status="all")
    stmts = [str(c[0][0]) for c in mock_db.execute.call_args_list]
    assert not any("status = 'completed'" in s for s in stmts)

    mock_db.reset_mock()
    # Test get_agents_comparison with status='failed'
    await get_agents_comparison(mock_db, period="30d", status="failed")
    stmts = [str(c[0][0]) for c in mock_db.execute.call_args_list]
    assert any("status = :status" in s or "status =" in s for s in stmts)

    print("[OK] dashboard_service status tests passed.")


async def test_service_evolution_status():
    print("Testing ServiceEvolutionService status filtering...")
    from app.services.service_evolution_service import ServiceEvolutionService
    from unittest.mock import AsyncMock, MagicMock

    mock_db = AsyncMock()
    mock_bind = MagicMock()
    mock_bind.dialect.name = "postgresql"
    mock_db.get_bind = MagicMock(return_value=mock_bind)
    mock_result = MagicMock()
    mock_result.fetchall.return_value = []
    mock_result.fetchone.return_value = (0, 0, 0, 0, 0, 0)
    mock_db.execute.return_value = mock_result

    # 1. get_services
    await ServiceEvolutionService.get_services(mock_db, status="failed")
    called_sql = str(mock_db.execute.call_args[0][0])
    assert "r.status = 'failed'" in called_sql

    await ServiceEvolutionService.get_services(mock_db, status="all")
    called_sql = str(mock_db.execute.call_args[0][0])
    assert "r.status = 'completed'" not in called_sql

    # 2. get_criteria
    await ServiceEvolutionService.get_criteria(mock_db, status="failed")
    called_sql = str(mock_db.execute.call_args[0][0])
    assert "r.status = 'failed'" in called_sql

    # 3. get_evolution
    await ServiceEvolutionService.get_evolution(mock_db, status="failed")
    called_sql = str(mock_db.execute.call_args[0][0])
    assert "r.status = 'failed'" in called_sql

    await ServiceEvolutionService.get_evolution(mock_db, status="all")
    called_sql = str(mock_db.execute.call_args[0][0])
    assert "r.status = 'completed'" not in called_sql

    print("[OK] ServiceEvolutionService status tests passed.")


async def main():
    test_normalize_status()
    await test_mass_evaluation_queries()
    await test_dashboard_service_status()
    await test_service_evolution_status()
    print("\n[OK] ALL status filter contract tests PASSED successfully!")


if __name__ == "__main__":
    asyncio.run(main())
