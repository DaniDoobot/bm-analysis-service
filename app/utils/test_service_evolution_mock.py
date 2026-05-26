"""
Mock-based unit tests for Service Evolution dashboard typologies logic.
Allows local validation on Windows without a live PostgreSQL database connection.
"""
import asyncio
import logging
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

# Add workspace directory to path
sys.path.append(".")

from app.services.service_evolution_service import ServiceEvolutionService
from app.schemas.service_evolution import ServiceEvolutionResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def test_evolution_typologies_with_mock_db():
    logger.info("=== STARTING MOCK-BASED SERVICE EVOLUTION TYPOLOGIES TESTS ===")

    # 1. Create a mock AsyncSession
    db = AsyncMock()

    # 2. Setup mock return values for sequential db.execute calls in get_evolution
    # Let's map our expected query result sets:
    
    # Query 1: Resolve service name (select service_name from bm_services...)
    mock_service_name_res = MagicMock()
    mock_service_name_res.fetchone.return_value = ("Front",)
    
    # Query 2: Summary metrics (count of calls, averages, cierre_cita_rate...)
    mock_summary_res = MagicMock()
    mock_summary_res.fetchone.return_value = (371, Decimal("7.80"), Decimal("8.00"), Decimal("7.90"), Decimal("8.10"), Decimal("0.45"))
    
    # Query 3: Main typology
    mock_main_typo_res = MagicMock()
    mock_main_typo_res.fetchone.return_value = ("Cita",)
    
    # Query 4: Series data
    mock_series_res = MagicMock()
    mock_series_res.fetchall.return_value = [
        ("2026-05-25", 1, "Front", 371, Decimal("7.80"), Decimal("7.20"), Decimal("7.90"), Decimal("7.50"), Decimal("8.00"), Decimal("8.10"), Decimal("8.20"), Decimal("7.60"), Decimal("7.40"), Decimal("8.00"), Decimal("0.45"))
    ]
    
    # Query 5: Active typologies (Query 1 in our refactored split)
    # Includes Cita, Otros, Reagendo, Confirmacion, Cancelacion, Falta, AND "Información" (key "informacion") with 0 calls!
    mock_active_typo_res = MagicMock()
    mock_active_typo_res.fetchall.return_value = [
        (1, "cita", "Cita", 192, Decimal("8.00"), Decimal("0.6406")),
        (2, "otros", "Otros", 107, Decimal("7.00"), Decimal("0.0")),
        (3, "reagendo", "Reagendo", 33, Decimal("8.50"), Decimal("0.6364")),
        (4, "confirmacion", "Confirmación", 17, Decimal("8.00"), Decimal("0.5882")),
        (5, "cancelacion", "Cancelación", 13, Decimal("6.00"), Decimal("0.0")),
        (6, "falta", "Falta", 4, Decimal("5.00"), Decimal("0.0")),
        (7, "informacion", "Información", 0, None, None), # 0 calls, None averages/rates
    ]
    
    # Query 6: Unclassified calls (Query 2 in our refactored split)
    mock_unclass_res = MagicMock()
    mock_unclass_res.fetchone.return_value = (5, Decimal("6.50"), Decimal("0.20")) # 5 calls, 20% cierre rate

    # Query 7: Agent split
    mock_agent_res = MagicMock()
    mock_agent_res.fetchall.return_value = [
        ("agent1", "Juan Pérez", 371, Decimal("7.80"), Decimal("8.00"), Decimal("0.45"))
    ]
    
    # Query 8: Criteria Ranking
    mock_ranking_res = MagicMock()
    mock_ranking_res.fetchall.return_value = [
        ("claridad", "Claridad", Decimal("8.00"), 371),
        ("empatia", "Empatía", Decimal("7.90"), 371),
        ("procedimiento", "Procedimiento", Decimal("8.10"), 371)
    ]

    # Assign sequential return values to db.execute
    db.execute.side_effect = [
        mock_service_name_res,
        mock_summary_res,
        mock_main_typo_res,
        mock_series_res,
        mock_active_typo_res,
        mock_unclass_res,
        mock_agent_res,
        mock_ranking_res
    ]

    # 3. Invoke ServiceEvolutionService.get_evolution
    response = await ServiceEvolutionService.get_evolution(
        db,
        service_id=1,
        date_from="2026-05-25",
        date_to="2026-05-26",
        granularity="day"
    )

    # 4. Assertions to validate all requirements
    logger.info("Verifying response structure and content...")
    assert isinstance(response, ServiceEvolutionResponse), "Should return a ServiceEvolutionResponse schema instance"
    
    # Assert filters and summary
    assert response.filters.service_id == 1
    assert response.filters.service_name == "Front"
    assert response.summary.total_calls == 371
    
    # Assert typologies list
    typologies = response.by_typology
    logger.info("Retrieved typologies split list:")
    for t in typologies:
        logger.info(f" - {t.typology_name} (key: {t.typology_key}, ID: {t.typology_id}) -> Calls: {t.total_calls}, Cierre: {t.cierre_cita_rate}")

    # Requirement 1 & 2: Active typologies list should have 8 items (7 active + 1 unclassified)
    assert len(typologies) == 8, f"Expected exactly 8 typologies, got {len(typologies)}"
    
    # Check "Información" (Requirement 3: calls = 0, rates = None)
    info_typo = next((t for t in typologies if t.typology_key == "informacion"), None)
    assert info_typo is not None, "Active typology 'Información' was not included!"
    assert info_typo.typology_id == 7, "Información typology_id should be 7"
    assert info_typo.typology_name == "Información"
    assert info_typo.total_calls == 0, "Información total_calls should be 0"
    assert info_typo.avg_evaluacion_global is None, "Información avg_evaluacion_global should be None"
    assert info_typo.cierre_cita_rate is None, "Información cierre_cita_rate should be None"
    logger.info("✓ Requirement verified: Typology with 0 calls is included with correct null values.")

    # Requirement 4 & 5 & 6: 'Sin clasificar' row (renamed from Unclassified, ID is null, appended at the end)
    unclass_typo = typologies[-1] # must be the last element
    assert unclass_typo.typology_key == "unclassified", "Last typology should be the 'unclassified' row"
    assert unclass_typo.typology_id is None, "Unclassified row typology_id should be None"
    assert unclass_typo.typology_name == "Sin clasificar", "Unclassified row typology_name should be 'Sin clasificar'"
    assert unclass_typo.total_calls == 5, "Unclassified row total_calls should be 5"
    assert unclass_typo.cierre_cita_rate == 0.20, f"Unclassified closure rate should be 0.20, got {unclass_typo.cierre_cita_rate}"
    logger.info("✓ Requirement verified: 'Sin clasificar' row is appended at the very end with correct mapping.")

    # Verify original list order (first 6 active typologies)
    assert typologies[0].typology_key == "cita"
    assert typologies[1].typology_key == "otros"
    assert typologies[2].typology_key == "reagendo"
    assert typologies[3].typology_key == "confirmacion"
    assert typologies[4].typology_key == "cancelacion"
    assert typologies[5].typology_key == "falta"
    logger.info("✓ Requirement verified: Typology sort_order and names are respected correctly.")

    logger.info("=== ALL MOCK-BASED EVOLUTION TYPOLOGIES TESTS PASSED SUCCESSFULLY! ===")


if __name__ == "__main__":
    asyncio.run(test_evolution_typologies_with_mock_db())
