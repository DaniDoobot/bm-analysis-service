"""
app/utils/test_demo_simulation_codes.py
=======================================
Comprehensive test suite for Empresa Demo Trainer simulation codes modernization:
1. Helper validations (normalize_simulation_code, is_valid_demo_simulation_code, generate_demo_simulation_code).
2. Simulation creation with validation & auto-generation for Empresa Demo.
3. Simulation duplication convention (sequential 6-8 chars instead of _COPY).
4. Bot telephony simulation code resolution (exact, lower, spaces, hyphens, single-digit spoken codes).
5. Non-regression for Boston Medical Group and other companies.
6. Migration script execution: dry-run preview, apply, and idempotence.
"""
import os
import sys
import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import BigInteger, select, func

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

sys.path.insert(0, os.path.abspath("."))

import app.db
from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
)
from app.schemas.trainer import (
    TrainerSimulationCreate,
    TrainerSimulationUpdate,
)
from app.services.trainer_service import TrainerService
from scripts.migrate_demo_simulation_codes import (
    migrate_demo_simulation_codes,
    resolve_demo_company,
    derive_target_code,
)


class TestDemoSimulationCodes(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        app.db._engine = self.engine
        app.db.SessionLocal = self.async_session

        async with self.async_session() as db:
            # 1. Boston Medical Company (company_id=1, real company)
            self.bm_company = Company(
                company_id=1,
                company_name="Boston Medical Group",
                company_key="boston-medical",
                is_demo=False,
                is_active=True,
            )
            # 2. Empresa Demo (company_id=7, demo company)
            self.demo_company = Company(
                company_id=7,
                company_name="Empresa Demo",
                company_key="empresa-demo",
                is_demo=True,
                is_active=True,
            )
            db.add_all([self.bm_company, self.demo_company])
            await db.flush()

            # Services with unique service_key
            self.bm_service = Service(
                service_id=10,
                service_key="atencion-medica-bm",
                service_name="Atención Médica BM",
                company_id=1,
                is_active=True,
            )
            self.demo_svc_at = Service(
                service_id=20,
                service_key="atencion-al-cliente",
                service_name="Atención al Cliente",
                company_id=7,
                is_active=True,
            )
            self.demo_svc_vn = Service(
                service_id=21,
                service_key="ventas",
                service_name="Ventas",
                company_id=7,
                is_active=True,
            )
            db.add_all([self.bm_service, self.demo_svc_at, self.demo_svc_vn])
            await db.flush()

            # Boston Medical Simulation
            self.bm_sim = TrainerSimulation(
                simulation_id=100,
                company_id=1,
                service_id=10,
                name="Consulta Médica General",
                code="BM-MED-01",
                roleplay_prompt="Roleplay BM",
                status="published",
            )
            db.add(self.bm_sim)

            # Empresa Demo Simulations (Initial legacy codes)
            self.demo_sim1 = TrainerSimulation(
                simulation_id=201,
                company_id=7,
                service_id=20,
                name="Gestión de Reclamación de Facturación Compleja",
                code="SIM-DEMO-AT01",
                roleplay_prompt="Roleplay Demo Reclamacion",
                status="published",
            )
            self.demo_sim2 = TrainerSimulation(
                simulation_id=202,
                company_id=7,
                service_id=20,
                name="Protocolo de Desescalada con Cliente Furioso",
                code="SIM-DEMO-AT02",
                roleplay_prompt="Roleplay Demo Desescalada",
                status="published",
            )
            self.demo_sim3 = TrainerSimulation(
                simulation_id=203,
                company_id=7,
                service_id=21,
                name="Prospección Comercial y Detección de Interés",
                code="SIM-DEMO-VN01",
                roleplay_prompt="Roleplay Demo Prospeccion",
                status="published",
            )
            self.demo_sim4 = TrainerSimulation(
                simulation_id=204,
                company_id=7,
                service_id=21,
                name="Superación de Objeción de Precio y Cierre",
                code="SIM-DEMO-VN02",
                roleplay_prompt="Roleplay Demo Cierre",
                status="published",
            )
            db.add_all([self.demo_sim1, self.demo_sim2, self.demo_sim3, self.demo_sim4])
            await db.flush()

            # Add versions and sessions to test relationship integrity
            ver1 = TrainerSimulationVersion(
                version_id=301,
                simulation_id=201,
                version_number=1,
                roleplay_prompt_snapshot="Prompt v1",
                evaluation_config_snapshot={},
                service_id=20,
                evaluation_config_id=1,
            )
            sess1 = TrainerSession(
                session_id=401,
                simulation_id=201,
                simulation_version_id=301,
                agent_id="agent-01",
                agent_code="AC-F01",
                company_id=7,
                service_id=20,
                call_id="call-401",
                status="completed",
                evaluation_status="evaluated",
            )
            db.add_all([ver1, sess1])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Helper Function Tests
    # ──────────────────────────────────────────────────────────────────────────

    def test_demo_code_format_validation(self):
        """Validate 6 to 8 uppercase alphanumeric rule."""
        # Valid demo codes
        self.assertTrue(TrainerService.is_valid_demo_simulation_code("ATEN01"))
        self.assertTrue(TrainerService.is_valid_demo_simulation_code("ATEN02"))
        self.assertTrue(TrainerService.is_valid_demo_simulation_code("VENT01"))
        self.assertTrue(TrainerService.is_valid_demo_simulation_code("VENT02"))
        self.assertTrue(TrainerService.is_valid_demo_simulation_code("RECL01"))
        self.assertTrue(TrainerService.is_valid_demo_simulation_code("SOPO01"))
        self.assertTrue(TrainerService.is_valid_demo_simulation_code("ATEN001"))  # 7 chars
        self.assertTrue(TrainerService.is_valid_demo_simulation_code("VENTAS01"))  # 8 chars

        # Invalid demo codes
        self.assertFalse(TrainerService.is_valid_demo_simulation_code("SIM-DEMO-AT01"))  # Hyphens
        self.assertFalse(TrainerService.is_valid_demo_simulation_code("ATEN-01"))  # Hyphen
        self.assertFalse(TrainerService.is_valid_demo_simulation_code("ATEN 01"))  # Space
        self.assertFalse(TrainerService.is_valid_demo_simulation_code("AT01"))  # 4 chars (< 6)
        self.assertFalse(TrainerService.is_valid_demo_simulation_code("ATEN000001"))  # 10 chars (> 8)
        self.assertFalse(TrainerService.is_valid_demo_simulation_code("aten01"))  # Lowercase

    def test_demo_code_normalization(self):
        """Verify normalization strips hyphens, spaces, underscores and uppercases."""
        self.assertEqual(TrainerService.normalize_simulation_code(" aten-01 "), "ATEN01")
        self.assertEqual(TrainerService.normalize_simulation_code("VENT 02"), "VENT02")
        self.assertEqual(TrainerService.normalize_simulation_code("recl_01"), "RECL01")
        self.assertEqual(TrainerService.normalize_simulation_code("sim-demo-at01"), "SIMDEMOAT01")
        self.assertEqual(TrainerService.normalize_simulation_code(None), "")

    async def test_generate_demo_simulation_code(self):
        """Verify automatic sequential code generation for Empresa Demo."""
        async with self.async_session() as db:
            # Service 20 is Atención al Cliente -> prefix ATEN
            code_at = await TrainerService.generate_demo_simulation_code(db, 20)
            self.assertTrue(code_at.startswith("ATEN"))
            self.assertTrue(TrainerService.is_valid_demo_simulation_code(code_at))

            # Service 21 is Ventas -> prefix VENT
            code_vn = await TrainerService.generate_demo_simulation_code(db, 21)
            self.assertTrue(code_vn.startswith("VENT"))
            self.assertTrue(TrainerService.is_valid_demo_simulation_code(code_vn))

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Simulation Creation & Duplication Tests
    # ──────────────────────────────────────────────────────────────────────────

    async def test_create_simulation_enforces_demo_convention(self):
        """Verify creating simulations for Empresa Demo adheres to the convention."""
        async with self.async_session() as db:
            # 1. Valid manual code (ATEN03)
            payload_ok = TrainerSimulationCreate(
                name="Simulación Demo Extra",
                code="ATEN03",
                service_id=20,
                roleplay_prompt="Test roleplay prompt",
            )
            sim_ok = await TrainerService.create_simulation(db, payload_ok)
            self.assertEqual(sim_ok.code, "ATEN03")
            self.assertTrue(TrainerService.is_valid_demo_simulation_code(sim_ok.code))

            # 2. Auto-generation when code is empty/None
            payload_auto = TrainerSimulationCreate(
                name="Simulación Demo Auto",
                code=None,
                service_id=21,
                roleplay_prompt="Test roleplay prompt",
            )
            sim_auto = await TrainerService.create_simulation(db, payload_auto)
            self.assertTrue(sim_auto.code.startswith("VENT"))
            self.assertTrue(TrainerService.is_valid_demo_simulation_code(sim_auto.code))

            # 3. Custom code with hyphens/special format is preserved, not rejected
            payload_custom = TrainerSimulationCreate(
                name="Simulación Personalizada",
                code="VENTAS-001",
                service_id=20,
                roleplay_prompt="Test roleplay prompt",
            )
            sim_custom = await TrainerService.create_simulation(db, payload_custom)
            self.assertEqual(sim_custom.code, "VENTAS-001")

            # 4. Boston Medical simulation creation is NOT blocked by Demo rules
            payload_bm = TrainerSimulationCreate(
                name="Simulación Boston Medical",
                code="BM-NEW-PROTOCOL-01",
                service_id=10,
                roleplay_prompt="Roleplay BM",
            )
            sim_bm = await TrainerService.create_simulation(db, payload_bm)
            self.assertEqual(sim_bm.code, "BM-NEW-PROTOCOL-01")

    async def test_duplicate_simulation_convention(self):
        """Verify duplicating demo simulation increments sequential number without _COPY."""
        async with self.async_session() as db:
            # Duplicate ATEN01 (id=201) after code is modernized to ATEN01
            sim201 = await db.get(TrainerSimulation, 201)
            sim201.code = "ATEN01"
            await db.commit()

            dup_sim = await TrainerService.duplicate_simulation(db, 201)
            self.assertEqual(dup_sim.code, "ATEN02")
            self.assertTrue(TrainerService.is_valid_demo_simulation_code(dup_sim.code))
            self.assertNotIn("_COPY", dup_sim.code)

            # Duplicate BM simulation (should use standard _COPY)
            dup_bm = await TrainerService.duplicate_simulation(db, 100)
            self.assertEqual(dup_bm.code, "BM-MED-01_COPY")

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Bot Telephony Validation & Normalization Tests
    # ──────────────────────────────────────────────────────────────────────────

    async def test_validate_simulation_code_voice_resolution(self):
        """Test voice bot simulation code resolution with spaces, hyphens, and single digits."""
        async with self.async_session() as db:
            # First set demo codes to target modern format
            sim1 = await db.get(TrainerSimulation, 201)
            sim1.code = "ATEN01"
            sim3 = await db.get(TrainerSimulation, 203)
            sim3.code = "VENT01"
            await db.commit()

            # Exact uppercase match
            res = await TrainerService.validate_simulation_code(db, "ATEN01")
            self.assertIsNotNone(res)
            self.assertEqual(res.simulation_id, 201)

            # Lowercase spoken transcription
            res = await TrainerService.validate_simulation_code(db, "aten01")
            self.assertIsNotNone(res)
            self.assertEqual(res.simulation_id, 201)

            # Spoken with spaces (e.g. "A T E N 0 1" or "ATEN 01")
            res = await TrainerService.validate_simulation_code(db, "ATEN 01")
            self.assertIsNotNone(res)
            self.assertEqual(res.simulation_id, 201)

            # Spoken with accidental hyphen
            res = await TrainerService.validate_simulation_code(db, "aten-01")
            self.assertIsNotNone(res)
            self.assertEqual(res.simulation_id, 201)

            # Spoken single digit "ATEN 1" or "aten1" resolved to ATEN01
            res = await TrainerService.validate_simulation_code(db, "ATEN1")
            self.assertIsNotNone(res)
            self.assertEqual(res.simulation_id, 201)

            res = await TrainerService.validate_simulation_code(db, "VENT 1")
            self.assertIsNotNone(res)
            self.assertEqual(res.simulation_id, 203)

            # Non-existent simulation code
            res = await TrainerService.validate_simulation_code(db, "UNKNOWN99")
            self.assertIsNone(res)

            # Boston Medical simulation resolution remains 100% operational
            res_bm = await TrainerService.validate_simulation_code(db, "BM-MED-01")
            self.assertIsNotNone(res_bm)
            self.assertEqual(res_bm.simulation_id, 100)

            res_bm_stripped = await TrainerService.validate_simulation_code(db, "BMMED01")
            self.assertIsNotNone(res_bm_stripped)
            self.assertEqual(res_bm_stripped.simulation_id, 100)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Migration Script Tests (Dry-Run, Apply, Idempotence, Safety)
    # ──────────────────────────────────────────────────────────────────────────

    async def test_migration_dry_run_leaves_db_untouched(self):
        """Dry-run should calculate target codes without modifying database."""
        async with self.async_session() as db:
            result = await migrate_demo_simulation_codes(db, apply=False)
            self.assertEqual(result["total_simulations"], 4)
            self.assertEqual(result["updated"], 4)
            self.assertFalse(result["apply"])

        async with self.async_session() as fresh_db:
            # Verify that database was rolled back and codes are still legacy
            sims = (await fresh_db.execute(select(TrainerSimulation).where(TrainerSimulation.company_id == 7))).scalars().all()
            codes = {s.code for s in sims}
            self.assertIn("SIM-DEMO-AT01", codes)
            self.assertIn("SIM-DEMO-VN01", codes)

    async def test_migration_apply_updates_codes_and_preserves_relations(self):
        """Apply migration modernizes demo simulation codes and preserves versions/sessions."""
        async with self.async_session() as db:
            # Execute apply migration
            result = await migrate_demo_simulation_codes(db, apply=True)
            self.assertEqual(result["updated"], 4)
            self.assertTrue(result["apply"])

            # Verify database has new codes
            sims = (await db.execute(
                select(TrainerSimulation).where(TrainerSimulation.company_id == 7).order_by(TrainerSimulation.simulation_id)
            )).scalars().all()
            code_map = {s.simulation_id: s.code for s in sims}
            self.assertEqual(code_map[201], "ATEN01")
            self.assertEqual(code_map[202], "ATEN02")
            self.assertEqual(code_map[203], "VENT01")
            self.assertEqual(code_map[204], "VENT02")

            # Verify all codes satisfy 6-8 alphanumeric rule
            for s in sims:
                self.assertTrue(TrainerService.is_valid_demo_simulation_code(s.code))

            # Verify foreign key / relationship integrity
            ver = await db.get(TrainerSimulationVersion, 301)
            self.assertEqual(ver.simulation_id, 201)

            sess = await db.get(TrainerSession, 401)
            self.assertEqual(sess.simulation_id, 201)

            # Verify Boston Medical simulation was completely untouched
            bm = await db.get(TrainerSimulation, 100)
            self.assertEqual(bm.code, "BM-MED-01")

            # Test Idempotence: running apply second time does 0 updates
            result_second = await migrate_demo_simulation_codes(db, apply=True)
            self.assertEqual(result_second["updated"], 0)
            for item in result_second["plan"]:
                self.assertEqual(item["action"], "ALREADY_COMPLIANT")

    async def test_migration_safety_abort_on_boston_medical(self):
        """Migration must refuse to run if pointed to Boston Medical."""
        async with self.async_session() as db:
            with self.assertRaises(RuntimeError) as ctx:
                await migrate_demo_simulation_codes(db, apply=False, override_company_id=1)
            self.assertIn("SAFETY ABORT", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
