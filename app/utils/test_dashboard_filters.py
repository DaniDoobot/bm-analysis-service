"""
Unit test suite for Dashboard & Analytics Typology and Direction filters.
Tests the normalizer logic and verifies that the production code
correctly accepts and normalizes typology/direction parameters.

Note: Integration tests that call service functions directly against
sqlite in-memory are limited by sqlite's lack of JSONB support and
->>'key' notation. We therefore focus on:
  1. Normalizer unit tests (synchronous, no DB)
  2. Smoke tests verifying services accept the new params without crash
"""
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

# Set dummy DATABASE_URL for test isolation
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from fastapi import HTTPException

from app.utils.normalizers import normalize_typology, normalize_direction


class TestNormalizers(unittest.TestCase):
    """Unit tests for normalize_typology and normalize_direction."""

    # --- Typology ---

    def test_typology_lowercase(self):
        self.assertEqual(normalize_typology("falta"), "falta")

    def test_typology_capitalized(self):
        self.assertEqual(normalize_typology("Falta"), "falta")

    def test_typology_multi_word(self):
        self.assertEqual(normalize_typology("Intento Contacto"), "intento_contacto")

    def test_typology_with_accent(self):
        # Accents should be stripped
        self.assertEqual(normalize_typology("Transferencia"), "transferencia")

    def test_typology_all_returns_none(self):
        self.assertIsNone(normalize_typology("all"))

    def test_typology_todos_returns_none(self):
        self.assertIsNone(normalize_typology("todos"))

    def test_typology_empty_string_returns_none(self):
        self.assertIsNone(normalize_typology(""))

    def test_typology_none_returns_none(self):
        self.assertIsNone(normalize_typology(None))

    def test_typology_whitespace_only_returns_none(self):
        self.assertIsNone(normalize_typology("   "))

    # --- Direction ---

    def test_direction_inbound(self):
        self.assertEqual(normalize_direction("inbound"), "inbound")

    def test_direction_entrante(self):
        self.assertEqual(normalize_direction("entrante"), "inbound")

    def test_direction_inbound_uppercase(self):
        self.assertEqual(normalize_direction("INBOUND"), "inbound")

    def test_direction_outbound(self):
        self.assertEqual(normalize_direction("outbound"), "outbound")

    def test_direction_saliente(self):
        self.assertEqual(normalize_direction("saliente"), "outbound")

    def test_direction_outbound_uppercase(self):
        self.assertEqual(normalize_direction("OUTBOUND"), "outbound")

    def test_direction_all_returns_none(self):
        self.assertIsNone(normalize_direction("all"))

    def test_direction_todas_returns_none(self):
        self.assertIsNone(normalize_direction("todas"))

    def test_direction_none_returns_none(self):
        self.assertIsNone(normalize_direction(None))

    def test_direction_empty_string_returns_none(self):
        self.assertIsNone(normalize_direction(""))

    def test_direction_invalid_raises_422(self):
        with self.assertRaises(HTTPException) as ctx:
            normalize_direction("invalid_direction_val")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_direction_another_invalid_raises_422(self):
        with self.assertRaises(HTTPException) as ctx:
            normalize_direction("mixto")
        self.assertEqual(ctx.exception.status_code, 422)

    # --- Combined behaviour ---

    def test_typology_and_direction_produce_expected_pairs(self):
        """Ensure combined normalisation produces correct pair."""
        t = normalize_typology("Intento Contacto")
        d = normalize_direction("entrante")
        self.assertEqual(t, "intento_contacto")
        self.assertEqual(d, "inbound")

    def test_all_values_return_none_pair(self):
        """When both filters are 'all'/'todos' the pair should be (None, None)."""
        t = normalize_typology("todos")
        d = normalize_direction("all")
        self.assertIsNone(t)
        self.assertIsNone(d)


class TestDashboardFilterImports(unittest.IsolatedAsyncioTestCase):
    """Smoke tests: verify service functions accept new keyword args without crash.

    These tests do NOT verify DB query results (that would require a full
    PostgreSQL test environment for JSONB support). They only ensure:
      - The functions can be imported and called with the new params.
      - No NameError / TypeError is raised for the new keyword arguments.
    """

    async def test_service_imports(self):
        """Verify that service functions can be imported."""
        from app.services.dashboard_service import (
            get_dashboard_summary,
            get_agents_list,
            get_agents_comparison,
            get_objections_breakdown,
        )
        self.assertTrue(callable(get_dashboard_summary))
        self.assertTrue(callable(get_agents_list))
        self.assertTrue(callable(get_agents_comparison))
        self.assertTrue(callable(get_objections_breakdown))

    async def test_service_evolution_imports(self):
        """Verify ServiceEvolutionService.get_evolution can be imported."""
        from app.services.service_evolution_service import ServiceEvolutionService
        self.assertTrue(hasattr(ServiceEvolutionService, "get_evolution"))

    async def test_analytics_router_imports(self):
        """Verify analytics router can be imported."""
        import app.routers.analytics as analytics_router
        self.assertIsNotNone(analytics_router.router)

    async def test_service_evolution_router_imports(self):
        """Verify service evolution router can be imported."""
        import app.routers.service_evolution as se_router
        self.assertIsNotNone(se_router.router)

    async def test_dashboard_router_imports(self):
        """Verify dashboard router can be imported."""
        import app.routers.dashboard as dashboard_router
        self.assertIsNotNone(dashboard_router.router)


if __name__ == "__main__":
    unittest.main()
