"""
tests/test_trainer_simulation_service_mismatch.py
=================================================
Test suite for Trainer simulation validation and service-mismatch error messages.

Verifies:
1. Valid simulation code within the agent's assigned service -> success (valid=True, status='valid').
2. Valid simulation code belonging to a DIFFERENT service -> rejected (valid=False, status='service_mismatch', informative message referencing agent's current service).
3. Non-existent / invalid simulation code -> rejected (valid=False, status='not_found').
4. Agent with multiple services in bm_user_services -> allowed for any assigned service, rejected for foreign services.
5. Voice / DTMF endpoint responses when service mismatch occurs.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import BigInteger, select

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

sys.path.insert(0, os.path.abspath("."))

from app.db import Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.teams import UserServiceAssociation
from app.models.personalized_training import TrainingAgentSetting
from app.models.trainer import TrainerSimulation
from app.services.trainer_service import TrainerService


class TestTrainerSimulationServiceMismatch(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.session_maker() as db:
            # 1. Company
            company = Company(
                company_id=1,
                company_name="Empresa Test",
                company_key="empresa-test",
                is_active=True,
            )
            db.add(company)
            await db.flush()

            # 2. Services
            svc_customer_service = Service(
                service_id=10,
                company_id=1,
                service_name="Atención al Cliente",
                service_key="atencion-cliente",
                is_active=True,
            )
            svc_sales = Service(
                service_id=20,
                company_id=1,
                service_name="Ventas Telefónicas",
                service_key="ventas-telefonicas",
                is_active=True,
            )
            svc_claims = Service(
                service_id=30,
                company_id=1,
                service_name="Reclamaciones",
                service_key="reclamaciones",
                is_active=True,
            )
            db.add_all([svc_customer_service, svc_sales, svc_claims])
            await db.flush()

            # 3. Users and Training Agent Settings
            # Agent 1: Customer Service only
            user_agent1 = User(
                user_id=101,
                username="agente_atencion",
                email="atencion@test.com",
                role="agent",
                company_id=1,
                primary_service_id=10,
                hubspot_owner_id="HS_OWNER_101",
                password_hash="fakehash",
                is_active=True,
            )
            setting_agent1 = TrainingAgentSetting(
                setting_id=1,
                company_id=1,
                hubspot_owner_id="HS_OWNER_101",
                agent_name="Ana García",
                agent_initials="AG",
                training_code="AG01",
                training_numeric_code="1001",
                training_code_enabled=True,
            )

            # Agent 2: Multi-service (Ventas + Reclamaciones)
            user_agent2 = User(
                user_id=102,
                username="agente_multi",
                email="multi@test.com",
                role="agent",
                company_id=1,
                primary_service_id=20,
                hubspot_owner_id="HS_OWNER_102",
                password_hash="fakehash",
                is_active=True,
            )
            setting_agent2 = TrainingAgentSetting(
                setting_id=2,
                company_id=1,
                hubspot_owner_id="HS_OWNER_102",
                agent_name="Carlos Ruiz",
                agent_initials="CR",
                training_code="CR02",
                training_numeric_code="1002",
                training_code_enabled=True,
            )
            # Secondary association to claims (service 30)
            assoc_user2_claims = UserServiceAssociation(user_id=102, service_id=30)

            db.add_all([user_agent1, setting_agent1, user_agent2, setting_agent2, assoc_user2_claims])
            await db.flush()

            # 4. Trainer Simulations
            sim_atencion = TrainerSimulation(
                simulation_id=1,
                company_id=1,
                service_id=10,
                name="Atención Incidencia Básica",
                code="ATEN01",
                roleplay_prompt="Eres un cliente con duda...",
                status="published",
            )
            sim_ventas = TrainerSimulation(
                simulation_id=2,
                company_id=1,
                service_id=20,
                name="Prospección Ventas Frías",
                code="VENT01",
                roleplay_prompt="Eres un prospecto comercial...",
                status="published",
            )
            sim_reclamaciones = TrainerSimulation(
                simulation_id=3,
                company_id=1,
                service_id=30,
                name="Gestión Reclamación Grave",
                code="RECL01",
                roleplay_prompt="Eres un cliente enfadado...",
                status="published",
            )
            db.add_all([sim_atencion, sim_ventas, sim_reclamaciones])
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_valid_simulation_same_service(self):
        """When agent enters a valid code of their current service, it succeeds."""
        async with self.session_maker() as db:
            res = await TrainerService.validate_simulation_for_agent(
                db, simulation_code="ATEN01", agent_id="HS_OWNER_101"
            )
            self.assertTrue(res["valid"])
            self.assertEqual(res["status"], "valid")
            self.assertIsNone(res["reason"])
            self.assertIsNotNone(res["simulation"])
            self.assertEqual(res["simulation"].simulation_id, 1)

    async def test_service_mismatch_rejection_with_clear_message(self):
        """When agent enters a valid code of another service, it is rejected with service_mismatch and informative message."""
        async with self.session_maker() as db:
            # Ana (Customer Service) enters VENT01 (Ventas)
            res = await TrainerService.validate_simulation_for_agent(
                db, simulation_code="VENT01", agent_id="HS_OWNER_101"
            )
            self.assertFalse(res["valid"])
            self.assertEqual(res["status"], "service_mismatch")
            self.assertEqual(res["reason"], "service_mismatch")
            self.assertIsNotNone(res["message"])
            self.assertIn("Atención al Cliente", res["message"])
            self.assertIn("pertenece a otro servicio", res["message"])
            self.assertEqual(res["agent_service_name"], "Atención al Cliente")

    async def test_non_existent_code_returns_not_found(self):
        """When simulation code does not exist at all, return not_found."""
        async with self.session_maker() as db:
            res = await TrainerService.validate_simulation_for_agent(
                db, simulation_code="INVALID999", agent_id="HS_OWNER_101"
            )
            self.assertFalse(res["valid"])
            self.assertEqual(res["status"], "not_found")
            self.assertEqual(res["reason"], "not_found")
            self.assertIsNone(res["simulation"])

    async def test_multi_service_agent_can_access_all_assigned_services(self):
        """Agent with primary service Ventas and secondary service Reclamaciones can access both."""
        async with self.session_maker() as db:
            # Ventas (service 20) -> allowed
            res_v = await TrainerService.validate_simulation_for_agent(
                db, simulation_code="VENT01", agent_id="HS_OWNER_102"
            )
            self.assertTrue(res_v["valid"])
            self.assertEqual(res_v["status"], "valid")

            # Reclamaciones (service 30) -> allowed
            res_r = await TrainerService.validate_simulation_for_agent(
                db, simulation_code="RECL01", agent_id="HS_OWNER_102"
            )
            self.assertTrue(res_r["valid"])
            self.assertEqual(res_r["status"], "valid")

            # Atención (service 10) -> rejected (service_mismatch)
            res_a = await TrainerService.validate_simulation_for_agent(
                db, simulation_code="ATEN01", agent_id="HS_OWNER_102"
            )
            self.assertFalse(res_a["valid"])
            self.assertEqual(res_a["status"], "service_mismatch")
            self.assertIn("pertenece a otro servicio", res_a["message"])

    async def test_spoken_code_normalization_with_service_mismatch(self):
        """Single digit or spaced spoken codes still resolve and validate service isolation."""
        async with self.session_maker() as db:
            # Spoken 'VENT 1' -> 'VENT01' -> service mismatch for Customer Service agent
            res = await TrainerService.validate_simulation_for_agent(
                db, simulation_code="VENT 1", agent_id="HS_OWNER_101"
            )
            self.assertFalse(res["valid"])
            self.assertEqual(res["status"], "service_mismatch")
            self.assertIn("pertenece a otro servicio", res["message"])

    async def test_training_hub_verify_simulation_dtmf_endpoint_service_mismatch(self):
        """DTMF endpoint in training hub returns explicit service mismatch explanation in TwiML."""
        from httpx import AsyncClient, ASGITransport
        from app.main import app
        from app.dependencies import get_db

        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/training/hub/verify-simulation-dtmf?agent_id=HS_OWNER_101&call_sid=CA12345",
                    data={"Digits": "VENT01"}
                )
                self.assertEqual(res.status_code, 200)
                self.assertIn("Atención al Cliente", res.text)
                self.assertIn("pertenece a otro servicio", res.text)
                self.assertIn("<Hangup/>", res.text)
        finally:
            app.dependency_overrides.clear()

    async def test_trainer_phone_verify_simulation_endpoint_service_mismatch(self):
        """DTMF endpoint in trainer phone returns explicit service mismatch explanation in TwiML."""
        from httpx import AsyncClient, ASGITransport
        from app.main import app
        from app.dependencies import get_db

        async def override_get_db():
            async with self.session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                res = await client.post(
                    "/bm/trainer/phone/verify-simulation-numeric-code?agent_id=HS_OWNER_101",
                    data={"Digits": "VENT01", "CallSid": "CA12345"}
                )
                self.assertEqual(res.status_code, 200)
                self.assertIn("Atención al Cliente", res.text)
                self.assertIn("pertenece a otro servicio", res.text)
                self.assertIn("<Hangup/>", res.text)
        finally:
            app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
