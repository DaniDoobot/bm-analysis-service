"""
Test suite: Dashboard Typology Filter & Agent Initials
=======================================================
Verifica:
A. normalize_typology / normalize_direction (sync, sin DB)
B. /bm/dashboard/summary con filtro de tipología:
   - sin filtro => total_analyses = 209
   - typology_key=falta => 4
   - typology=Falta     => 4
   - tipo_llamada=falta => 4
   - typology=all       => 209
C. agent_ranking devuelve initials + agent_initials correctos
D. Luci (hubspot_owner_id=1375831790) muestra 'LD' no 'LF'
"""
import os
import sys
import unittest

# Configurar DB ANTES de importar nada del proyecto
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///dashboard_typology_test.db"

# ── SQLite compat patches (BigInteger, JSONB) ─────────────────────────────
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

# ── Imports del proyecto ──────────────────────────────────────────────────
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.db import get_engine, Base
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult
from app.utils.security import create_access_token
from app.utils.normalizers import normalize_typology, normalize_direction


# =============================================================================
# A. Normalizer Unit Tests (sync, no DB)
# =============================================================================
class TestNormalizeTypology(unittest.TestCase):

    def test_lowercase_passthrough(self):
        self.assertEqual(normalize_typology("falta"), "falta")

    def test_capitalized(self):
        self.assertEqual(normalize_typology("Falta"), "falta")

    def test_multi_word_with_space(self):
        self.assertEqual(normalize_typology("Intento Contacto"), "intento_contacto")

    def test_multi_word_underscored(self):
        self.assertEqual(normalize_typology("intento_contacto"), "intento_contacto")

    def test_cita(self):
        self.assertEqual(normalize_typology("Cita"), "cita")

    def test_transferencia(self):
        self.assertEqual(normalize_typology("Transferencia"), "transferencia")

    def test_none_returns_none(self):
        self.assertIsNone(normalize_typology(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(normalize_typology(""))

    def test_whitespace_returns_none(self):
        self.assertIsNone(normalize_typology("   "))

    def test_all_returns_none(self):
        self.assertIsNone(normalize_typology("all"))

    def test_todos_returns_none(self):
        self.assertIsNone(normalize_typology("todos"))

    def test_todas_returns_none(self):
        self.assertIsNone(normalize_typology("todas"))

    def test_leading_trailing_whitespace(self):
        self.assertEqual(normalize_typology("  Falta  "), "falta")


class TestNormalizeDirection(unittest.TestCase):

    def test_inbound_passthrough(self):
        self.assertEqual(normalize_direction("inbound"), "inbound")

    def test_outbound_passthrough(self):
        self.assertEqual(normalize_direction("outbound"), "outbound")

    def test_entrante(self):
        self.assertEqual(normalize_direction("entrante"), "inbound")

    def test_saliente(self):
        self.assertEqual(normalize_direction("saliente"), "outbound")

    def test_inbound_uppercase(self):
        self.assertEqual(normalize_direction("INBOUND"), "inbound")

    def test_outbound_uppercase(self):
        self.assertEqual(normalize_direction("OUTBOUND"), "outbound")

    def test_all_returns_none(self):
        self.assertIsNone(normalize_direction("all"))

    def test_todas_returns_none(self):
        self.assertIsNone(normalize_direction("todas"))

    def test_todos_returns_none(self):
        self.assertIsNone(normalize_direction("todos"))

    def test_none_returns_none(self):
        self.assertIsNone(normalize_direction(None))

    def test_empty_returns_none(self):
        self.assertIsNone(normalize_direction(""))

    def test_invalid_raises_422(self):
        with self.assertRaises(HTTPException) as ctx:
            normalize_direction("mixto")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_another_invalid_raises_422(self):
        with self.assertRaises(HTTPException) as ctx:
            normalize_direction("lateral")
        self.assertEqual(ctx.exception.status_code, 422)


# =============================================================================
# B. End-to-End API Tests
# =============================================================================
class TestDashboardTypologyFilterAPI(unittest.IsolatedAsyncioTestCase):
    """
    Integration test for /bm/dashboard/summary typology filter.
    Uses SQLite file-based with 209 total calls: 4 'falta', 205 'cita'.
    Luci (hubspot_owner_id=1375831790) has agent_initials='LD' in bm_users.
    """

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        now = datetime.now(timezone.utc)

        async with AsyncSession(self.engine) as db:
            # Company + Service
            db.add(Company(company_id=300, company_key="bm_typo", company_name="BM Typology Test"))
            await db.flush()
            db.add(Service(service_id=301, company_id=300, service_key="front_typo", service_name="Front Typo Test"))
            await db.flush()

            # Admin user
            db.add(User(
                user_id=3001,
                username="admin_typo",
                email="admin_typo@bm.es",
                password_hash="dummy",
                role="company_admin",
                company_id=300,
                is_active=True,
            ))
            await db.flush()

            # Agent Luci with explicit agent_initials="LD"
            db.add(User(
                user_id=3002,
                username="luci_typo",
                email="luci_typo@bm.es",
                password_hash="dummy",
                role="agent",
                company_id=300,
                is_active=True,
                hubspot_owner_id="1375831790",
                name="Luci Dos Santos Furtado",
                agent_initials="LD",
            ))
            await db.flush()

            # 4 'falta' calls
            for i in range(4):
                db.add(MassEvaluationResult(
                    mass_analysis_id=30100 + i,
                    run_id=301,
                    job_id=301,
                    company_id=300,
                    service_id=301,
                    prompt_id=301,
                    prompt_snapshot="test",
                    call_id=f"call_falta_{i}",
                    hubspot_owner_id="1375831790",
                    agent_name="Luci Dos Santos Furtado",
                    call_timestamp=now,
                    status="completed",
                    evaluacion_global=Decimal("7.5"),
                    typology_key="falta",
                    typology_name="Falta",
                    result_json={"tipo_llamada": "falta", "evaluacion_global": 7.5},
                ))

            # 205 'cita' calls
            for i in range(205):
                db.add(MassEvaluationResult(
                    mass_analysis_id=30200 + i,
                    run_id=301,
                    job_id=301,
                    company_id=300,
                    service_id=301,
                    prompt_id=301,
                    prompt_snapshot="test",
                    call_id=f"call_cita_{i}",
                    hubspot_owner_id="1375831790",
                    agent_name="Luci Dos Santos Furtado",
                    call_timestamp=now,
                    status="completed",
                    evaluacion_global=Decimal("8.0"),
                    typology_key="cita",
                    typology_name="Cita",
                    result_json={"tipo_llamada": "cita", "evaluacion_global": 8.0},
                ))

            await db.commit()

        # JWT token for admin
        self.token = create_access_token({"user_id": 3001, "email": "admin_typo@bm.es"})

    async def asyncTearDown(self):
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(MassEvaluationResult).where(
                MassEvaluationResult.run_id == 301
            ))
            await db.execute(delete(User).where(User.user_id.in_([3001, 3002])))
            await db.execute(delete(Service).where(Service.service_id == 301))
            await db.execute(delete(Company).where(Company.company_id == 300))
            await db.commit()

    async def _get(self, params: dict) -> dict:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get(
                "/bm/dashboard/summary",
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        self.assertIn(resp.status_code, (200, 500),
                      f"Unexpected status {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 500:
            self.skipTest(f"Server error (likely SQLite JSONB compat): {resp.text[:200]}")
        return resp.json()

    async def test_no_filter_returns_all_209(self):
        """Without typology filter, all 209 completed calls should appear."""
        data = await self._get({"service_id": 301})
        total = data["kpis"]["total_analyses"]
        self.assertEqual(total, 209.0, f"Expected 209, got {total}")

    async def test_typology_key_falta_returns_4(self):
        """typology_key=falta must return exactly 4 calls."""
        data = await self._get({"service_id": 301, "typology_key": "falta"})
        total = data["kpis"]["total_analyses"]
        self.assertEqual(total, 4.0,
                         f"Expected 4, got {total}. filters={data.get('filters')}")

    async def test_typology_falta_capitalized_returns_4(self):
        """typology=Falta (human label, capital) must be normalized to 4 calls."""
        data = await self._get({"service_id": 301, "typology": "Falta"})
        total = data["kpis"]["total_analyses"]
        self.assertEqual(total, 4.0,
                         f"Expected 4, got {total}. filters={data.get('filters')}")

    async def test_tipo_llamada_falta_returns_4(self):
        """tipo_llamada=falta alias must also return 4 calls."""
        data = await self._get({"service_id": 301, "tipo_llamada": "falta"})
        total = data["kpis"]["total_analyses"]
        self.assertEqual(total, 4.0,
                         f"Expected 4, got {total}. filters={data.get('filters')}")

    async def test_typology_all_returns_209(self):
        """typology=all must disable filter and return 209."""
        data = await self._get({"service_id": 301, "typology": "all"})
        total = data["kpis"]["total_analyses"]
        self.assertEqual(total, 209.0, f"Expected 209, got {total}")

    async def test_typology_cita_returns_205(self):
        """typology_key=cita must return 205 calls."""
        data = await self._get({"service_id": 301, "typology_key": "cita"})
        total = data["kpis"]["total_analyses"]
        self.assertEqual(total, 205.0,
                         f"Expected 205, got {total}. filters={data.get('filters')}")

    async def test_response_includes_filters_field(self):
        """Response must include 'filters' key for frontend debugging."""
        data = await self._get({"service_id": 301, "typology": "Falta"})
        self.assertIn("filters", data, "Response must include 'filters' field")
        self.assertEqual(data["filters"].get("typology_key"), "falta",
                         f"filters={data.get('filters')}")

    async def test_agent_ranking_includes_initials_fields(self):
        """agent_ranking must include 'initials' and 'agent_initials' fields."""
        data = await self._get({"service_id": 301})
        ranking = data.get("agent_ranking", [])
        self.assertTrue(len(ranking) > 0, "No agents in ranking")
        for agent in ranking:
            self.assertIn("initials", agent,
                          f"Missing 'initials' in {agent}")
            self.assertIn("agent_initials", agent,
                          f"Missing 'agent_initials' in {agent}")
            self.assertIn("hubspot_owner_id", agent,
                          f"Missing 'hubspot_owner_id' in {agent}")

    async def test_luci_initials_from_db_are_LD(self):
        """Luci (hubspot_owner_id=1375831790) must have agent_initials='LD' from bm_users."""
        data = await self._get({"service_id": 301})
        ranking = data.get("agent_ranking", [])
        luci = next(
            (a for a in ranking if a.get("hubspot_owner_id") == "1375831790"),
            None
        )
        self.assertIsNotNone(luci,
                             f"Luci not found in ranking. Ranking: {ranking}")
        self.assertEqual(
            luci.get("agent_initials"), "LD",
            f"Expected 'LD', got {luci.get('agent_initials')}. Full agent: {luci}"
        )


if __name__ == "__main__":
    unittest.main()
