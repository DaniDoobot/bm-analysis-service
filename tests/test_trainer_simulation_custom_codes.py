"""
tests/test_trainer_simulation_custom_codes.py
=============================================
Regression test suite for INCIDENCIA #5:
Trainer simulation custom codes and non-overwriting behavior.

Covers:
1. Custom manual code: MI_SIMULACION_01 -> preserved exactly.
2. Custom manual code: VENTAS-001 -> preserved exactly.
3. Custom manual code: INCIDENCIA_FACTURACION -> preserved exactly.
4. Custom code starting with 'SIM' (e.g. SIM_VENTAS_01) -> NEVER overwritten by auto-generator.
5. Auto-generation when code is omitted (None or "") -> generates expected sequential prefix code (e.g. ATEN0X).
6. Demo simulations with ATEN01, ATEN02 continue working with telephony resolution.
7. Updating a simulation preserves its existing code, or updates to a new custom code cleanly.
8. Duplication preserves behavior:
   - Increments short demo codes (ATEN01 -> ATEN02)
   - Uses _COPY suffix for custom codes (VENTAS-001 -> VENTAS-001_COPY)
9. Duplicate code validation is enforced (cannot create two simulations with same code).
"""
import os
import sys
import unittest

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import BigInteger, select

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
from app.models.trainer import TrainerSimulation
from app.schemas.trainer import TrainerSimulationCreate, TrainerSimulationUpdate
from app.services.trainer_service import TrainerService


class TestTrainerSimulationCustomCodes(unittest.IsolatedAsyncioTestCase):

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
            self.demo_company = Company(
                company_id=7,
                company_name="Empresa Demo",
                company_key="empresa-demo",
                is_demo=True,
                is_active=True,
            )
            db.add(self.demo_company)
            await db.flush()

            self.svc_aten = Service(
                service_id=20,
                service_key="atencion-al-cliente",
                service_name="Atención al Cliente",
                company_id=7,
                is_active=True,
            )
            self.svc_vent = Service(
                service_id=21,
                service_key="ventas",
                service_name="Ventas",
                company_id=7,
                is_active=True,
            )
            db.add_all([self.svc_aten, self.svc_vent])
            await db.flush()

            # Seed existing simulations with standard demo codes
            self.sim_aten01 = TrainerSimulation(
                simulation_id=101,
                company_id=7,
                service_id=20,
                name="Atención Reclamaciones",
                code="ATEN01",
                roleplay_prompt="Roleplay prompt ATEN01",
                status="published",
            )
            self.sim_aten02 = TrainerSimulation(
                simulation_id=102,
                company_id=7,
                service_id=20,
                name="Atención Desescalada",
                code="ATEN02",
                roleplay_prompt="Roleplay prompt ATEN02",
                status="published",
            )
            db.add_all([self.sim_aten01, self.sim_aten02])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_01_create_simulation_with_custom_code_mi_simulacion_01(self):
        """Custom code 'MI_SIMULACION_01' must be preserved exactly."""
        async with self.async_session() as db:
            payload = TrainerSimulationCreate(
                name="Simulación Personalizada 1",
                code="MI_SIMULACION_01",
                service_id=20,
                roleplay_prompt="Prompt para mi simulacion",
            )
            sim = await TrainerService.create_simulation(db, payload)
            self.assertEqual(sim.code, "MI_SIMULACION_01")
            self.assertEqual(sim.name, "Simulación Personalizada 1")

    async def test_02_create_simulation_with_custom_code_ventas_001(self):
        """Custom code 'VENTAS-001' with hyphens must be preserved exactly."""
        async with self.async_session() as db:
            payload = TrainerSimulationCreate(
                name="Simulación Comercial Ventas",
                code="VENTAS-001",
                service_id=21,
                roleplay_prompt="Prompt de ventas",
            )
            sim = await TrainerService.create_simulation(db, payload)
            self.assertEqual(sim.code, "VENTAS-001")

    async def test_03_create_simulation_with_custom_code_incidencia_facturacion(self):
        """Custom long code 'INCIDENCIA_FACTURACION' must be preserved exactly without 6-8 char limit."""
        async with self.async_session() as db:
            payload = TrainerSimulationCreate(
                name="Incidencia de Facturación",
                code="INCIDENCIA_FACTURACION",
                service_id=20,
                roleplay_prompt="Prompt de facturacion",
            )
            sim = await TrainerService.create_simulation(db, payload)
            self.assertEqual(sim.code, "INCIDENCIA_FACTURACION")

    async def test_04_create_simulation_starting_with_sim_is_never_overwritten(self):
        """A custom code starting with 'SIM' (e.g. SIM_VENTAS_01) must NEVER be replaced by auto-generator."""
        async with self.async_session() as db:
            payload = TrainerSimulationCreate(
                name="Simulación con prefijo SIM",
                code="SIM_VENTAS_01",
                service_id=21,
                roleplay_prompt="Prompt de simulacion",
            )
            sim = await TrainerService.create_simulation(db, payload)
            self.assertEqual(sim.code, "SIM_VENTAS_01")
            self.assertFalse(sim.code.startswith("VENT"))

    async def test_05_create_simulation_without_code_auto_generates_sequential_demo_code(self):
        """When code is omitted (None or empty), the system auto-generates the next sequential code."""
        async with self.async_session() as db:
            # Service 20 has ATEN01, ATEN02 -> next must be ATEN03
            payload_none = TrainerSimulationCreate(
                name="Simulación Sin Código None",
                code=None,
                service_id=20,
                roleplay_prompt="Prompt sin codigo",
            )
            sim1 = await TrainerService.create_simulation(db, payload_none)
            self.assertEqual(sim1.code, "ATEN03")

            # Next empty code in service 20 -> ATEN04
            payload_empty = TrainerSimulationCreate(
                name="Simulación Sin Código Empty",
                code="   ",
                service_id=20,
                roleplay_prompt="Prompt vacio",
            )
            sim2 = await TrainerService.create_simulation(db, payload_empty)
            self.assertEqual(sim2.code, "ATEN04")

    async def test_06_existing_demo_simulations_continue_working(self):
        """Existing demo codes like ATEN01, ATEN02 must resolve properly for phone/bot validation."""
        async with self.async_session() as db:
            sim1 = await TrainerService.validate_simulation_code(db, "ATEN01")
            self.assertIsNotNone(sim1)
            self.assertEqual(sim1.simulation_id, 101)

            sim2 = await TrainerService.validate_simulation_code(db, "aten02")
            self.assertIsNotNone(sim2)
            self.assertEqual(sim2.simulation_id, 102)

    async def test_07_edit_simulation_preserves_code_or_updates_custom_code(self):
        """Editing a simulation without altering code preserves it; altering code updates it."""
        async with self.async_session() as db:
            # 1. Update only name and objective -> code remains unchanged
            update_payload = TrainerSimulationUpdate(
                name="Nuevo Nombre ATEN01",
                objective="Nuevo objetivo",
            )
            updated_sim = await TrainerService.update_simulation(db, 101, update_payload)
            self.assertEqual(updated_sim.code, "ATEN01")
            self.assertEqual(updated_sim.name, "Nuevo Nombre ATEN01")

            # 2. Update code explicitly to a custom code
            update_code_payload = TrainerSimulationUpdate(
                code="RECLAMACIONES_CUSTOM_V2",
            )
            updated_sim2 = await TrainerService.update_simulation(db, 101, update_code_payload)
            self.assertEqual(updated_sim2.code, "RECLAMACIONES_CUSTOM_V2")

    async def test_08_duplicate_simulation_convention(self):
        """
        Duplicating a simulation:
        - For short demo codes (e.g. ATEN02), increments to next sequential (ATEN03).
        - For custom codes (e.g. VENTAS-001), produces _COPY suffix.
        """
        async with self.async_session() as db:
            # 1. Duplicate ATEN02 -> ATEN03
            dup_demo = await TrainerService.duplicate_simulation(db, 102)
            self.assertEqual(dup_demo.code, "ATEN03")
            self.assertNotIn("_COPY", dup_demo.code)

            # 2. Create custom simulation VENTAS-001 and duplicate it -> VENTAS-001_COPY
            custom_payload = TrainerSimulationCreate(
                name="Ventas Especial",
                code="VENTAS-001",
                service_id=21,
                roleplay_prompt="Roleplay ventas",
            )
            sim_custom = await TrainerService.create_simulation(db, custom_payload)

            dup_custom = await TrainerService.duplicate_simulation(db, sim_custom.simulation_id)
            self.assertEqual(dup_custom.code, "VENTAS-001_COPY")

    async def test_09_duplicate_code_validation_is_enforced(self):
        """Attempting to create or update to an already existing code raises ValueError."""
        async with self.async_session() as db:
            # Try to create with ATEN01 (already exists)
            payload_dup = TrainerSimulationCreate(
                name="Simulacion Duplicada",
                code="ATEN01",
                service_id=20,
                roleplay_prompt="Prompt duplicado",
            )
            with self.assertRaises(ValueError) as ctx:
                await TrainerService.create_simulation(db, payload_dup)
            self.assertIn("ya existe", str(ctx.exception))

            # Try to update simulation 102 to have code ATEN01
            update_payload = TrainerSimulationUpdate(code="ATEN01")
            with self.assertRaises(ValueError) as ctx:
                await TrainerService.update_simulation(db, 102, update_payload)
            self.assertIn("ya existe", str(ctx.exception))
