"""
Mock-based verification test for call center agents list.
Validates names, IDs, and that all 7 expected agents are correctly returned by the service.
"""
import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock

# Add workspace directory to path
sys.path.append(".")

from app.services.dashboard_service import get_agents_list

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def test_agents_list_mock():
    logger.info("=== STARTING MOCK-BASED AGENTS LIST TESTS ===")

    # 1. Create a mock AsyncSession
    db = AsyncMock()

    # 2. Setup mock return values for sequential db.execute calls in get_agents_list
    # Query 1: Aggregates of completed jobs per agent
    mock_agg_res = MagicMock()
    mock_agg_res.fetchall.return_value = [] # No evaluations in DB for simplicity

    # Query 2: Fetch result_json for average evaluation calculations
    mock_rj_res = MagicMock()
    mock_rj_res.fetchall.return_value = [] # No evaluations

    db.execute.side_effect = [
        mock_agg_res,
        mock_rj_res
    ]

    # 3. Call get_agents_list
    results = await get_agents_list(db)

    # 4. Assertions
    logger.info(f"Retrieved {len(results)} agents:")
    for a in results:
        logger.info(f" - ID: {a['hubspot_owner_id']} -> Name: {a['agent_name']} (Total analyses: {a['total_analyses']})")

    # Verify count is exactly 7 (the number of known mappings in OWNER_TO_NAME)
    assert len(results) == 7, f"Expected exactly 7 agents, got {len(results)}"

    # Validate each expected agent
    expected_agents = {
        "1459417733": "Santiago Taboada",
        "1375831790": "Luci Dos Santos Furtado",
        "1539993532": "Fernanda Rodrigues",
        "1375831787": "Roberto Galán",
        "1375831791": "Eugenia Carreno",
        "33013277": "Bryan Herrera",
        "33013276": "Cristina Montenegro",
    }

    for oid, expected_name in expected_agents.items():
        agent = next((a for a in results if a["hubspot_owner_id"] == oid), None)
        assert agent is not None, f"Agent with ID {oid} not found in results!"
        assert agent["agent_name"] == expected_name, f"Expected name '{expected_name}', got '{agent['agent_name']}'"
        assert agent["total_analyses"] == 0, f"Expected 0 analyses, got {agent['total_analyses']}"
        assert agent["avg_evaluacion_global"] == 0.0, f"Expected 0.0 global evaluation, got {agent['avg_evaluacion_global']}"
        assert agent["last_analysis_at"] is None, f"Expected None last_analysis_at, got {agent['last_analysis_at']}"

    logger.info("✓ Verification successful: All 7 agents matched exactly by owner_id and name.")
    logger.info("=== ALL MOCK-BASED AGENTS LIST TESTS PASSED SUCCESSFULLY! ===")


if __name__ == "__main__":
    asyncio.run(test_agents_list_mock())
