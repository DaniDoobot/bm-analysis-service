"""
Unit tests for HubSpot side effect containment by service_id.
Verifies fail-closed gating:
- service_id=1 (Front): allowed to create tickets upon alarm
- service_id=2 (EXPAC): suppressed, no CRM write
- service_id=None: suppressed, no CRM write
- service_id=99 (future): suppressed, no CRM write
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Ensure SQLite compatibility shims for PostgreSQL JSONB if needed
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.config import get_settings
from app.db import Base
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.services.hubspot_service import HubSpotService, is_hubspot_side_effect_allowed, HUBSPOT_SIDE_EFFECT_ALLOWED_SERVICE_IDS
from app.services.mass_evaluation_service import MassEvaluationService


class TestHubSpotServiceContainment(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        self.settings = get_settings()
        self.settings.hubspot_access_token = "mock_token"
        self.settings.hubspot_portal_id = "140451581"
        self.settings.hubspot_ticket_pipeline = "mock_pipe"
        self.settings.hubspot_ticket_stage = "mock_stage"
        self.settings.hubspot_tipo_de_rem = "mock_rem"
        self.settings.hubspot_alarm_tickets_enabled = True
        self.settings.hubspot_alarm_company_id = 1

        self._find_patcher = patch.object(HubSpotService, "find_alarm_ticket", new=AsyncMock(return_value=None))
        self.mock_find = self._find_patcher.start()

    async def asyncTearDown(self):
        self._find_patcher.stop()
        await self.engine.dispose()

    def test_gate_function_fail_closed(self):
        """Verify the centralized is_hubspot_side_effect_allowed gate function."""
        # 1. Front (service_id=1) must be allowed
        self.assertTrue(is_hubspot_side_effect_allowed(1))
        self.assertTrue(is_hubspot_side_effect_allowed("1"))

        # 2. EXPAC (service_id=2) must be blocked
        self.assertFalse(is_hubspot_side_effect_allowed(2))
        self.assertFalse(is_hubspot_side_effect_allowed("2"))

        # 3. None must be blocked
        self.assertFalse(is_hubspot_side_effect_allowed(None))

        # 4. Future/other services (e.g. 99) must be blocked
        self.assertFalse(is_hubspot_side_effect_allowed(99))
        self.assertFalse(is_hubspot_side_effect_allowed("invalid"))

        # 5. Exact allowlist contents
        self.assertEqual(HUBSPOT_SIDE_EFFECT_ALLOWED_SERVICE_IDS, frozenset({1}))

    async def _create_result(self, session: AsyncSession, service_id: int | None, service_name: str | None) -> MassEvaluationResult:
        res = MassEvaluationResult(
            run_id=1,
            job_id=1,
            prompt_id=58,
            prompt_snapshot="Test Prompt Snapshot",
            call_id=f"call_{service_id}",
            status="completed",
            is_evaluable=True,
            company_id=1,
            service_id=service_id,
            service_name=service_name,
            agent_name="Test Agent",
            hubspot_owner_id="31499194",
            call_timestamp=datetime(2026, 9, 11, 10, 0, 0, tzinfo=timezone.utc),
            call_duration_seconds=120,
            direction="INBOUND",
            evaluacion_global=Decimal("3.5"),
            items_json=[{"criterion_key": "alarma", "value": True, "feed_key": "alarma"}],
            hubspot_contact_id="contact_123",
            created_at=datetime.now(timezone.utc)
        )
        session.add(res)
        await session.commit()
        await session.refresh(res)
        return res

    async def test_front_service_1_creates_hubspot_ticket(self):
        """Case A: service_id=1 + valid alarm -> HubSpot POST is executed."""
        async with self.async_session() as session:
            res = await self._create_result(session, service_id=1, service_name="Front")

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=201,
                    json=lambda: {"id": "hs_ticket_front_001", "properties": {}},
                    raise_for_status=lambda: None
                )

                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="Front",
                    agent_name="Test Agent",
                    call_id=res.call_id,
                    call_timestamp=res.call_timestamp,
                    typology_name="Queja",
                    direction="INBOUND",
                    call_duration_seconds=120,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Feed de alarma",
                    contact_id="contact_123",
                    service_id=1,
                )

                self.assertEqual(mock_post.call_count, 1)
                payload = mock_post.call_args[1]["json"]
                self.assertEqual(payload["properties"]["subject"], "REM doobot speechFront")

                stmt = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == res.mass_analysis_id)
                updated = (await session.execute(stmt)).scalars().first()
                self.assertEqual(updated.hubspot_ticket_id, "hs_ticket_front_001")
                self.assertEqual(updated.hubspot_ticket_status, "created")

    async def test_expac_service_2_suppresses_hubspot_ticket(self):
        """Case B: service_id=2 (EXPAC) + valid alarm -> HubSpot write suppressed, alarm preserved."""
        async with self.async_session() as session:
            res = await self._create_result(session, service_id=2, service_name="EXPAC")

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="EXPAC",
                    agent_name="Victoria Arellano",
                    call_id=res.call_id,
                    call_timestamp=res.call_timestamp,
                    typology_name="General",
                    direction="INBOUND",
                    call_duration_seconds=120,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Feed de alarma",
                    contact_id="contact_123",
                    service_id=2,
                )

                # HubSpot POST must NOT be called
                mock_post.assert_not_called()

                # Alarm and result in DB are preserved; ticket was NOT created
                stmt = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == res.mass_analysis_id)
                updated = (await session.execute(stmt)).scalars().first()
                self.assertTrue(updated.alarma)
                self.assertIsNone(updated.hubspot_ticket_id)
                self.assertIsNone(updated.hubspot_ticket_status)

    async def test_none_service_id_suppresses_hubspot_ticket(self):
        """Case C: service_id=None + valid alarm -> HubSpot write suppressed."""
        async with self.async_session() as session:
            res = await self._create_result(session, service_id=None, service_name=None)

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name=None,
                    agent_name="Unknown Agent",
                    call_id=res.call_id,
                    call_timestamp=res.call_timestamp,
                    typology_name="General",
                    direction="INBOUND",
                    call_duration_seconds=120,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Feed de alarma",
                    contact_id="contact_123",
                    service_id=None,
                )

                mock_post.assert_not_called()
                stmt = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == res.mass_analysis_id)
                updated = (await session.execute(stmt)).scalars().first()
                self.assertIsNone(updated.hubspot_ticket_id)
                self.assertIsNone(updated.hubspot_ticket_status)

    async def test_future_service_id_99_suppresses_hubspot_ticket(self):
        """Case D: service_id=99 (future service) + valid alarm -> HubSpot write suppressed."""
        async with self.async_session() as session:
            res = await self._create_result(session, service_id=99, service_name="FutureService")

            with patch("app.services.hubspot_service.httpx.AsyncClient.post") as mock_post:
                await MassEvaluationService._process_alarm_hubspot_ticket(
                    db=session,
                    mass_analysis_id=res.mass_analysis_id,
                    execution_source="automation",
                    company_id=1,
                    service_name="FutureService",
                    agent_name="Agent 99",
                    call_id=res.call_id,
                    call_timestamp=res.call_timestamp,
                    typology_name="General",
                    direction="INBOUND",
                    call_duration_seconds=120,
                    evaluacion_global=res.evaluacion_global,
                    alarma_feed="Feed de alarma",
                    contact_id="contact_123",
                    service_id=99,
                )

                mock_post.assert_not_called()
                stmt = select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == res.mass_analysis_id)
                updated = (await session.execute(stmt)).scalars().first()
                self.assertIsNone(updated.hubspot_ticket_id)
                self.assertIsNone(updated.hubspot_ticket_status)

    async def test_recovery_sweep_skips_expac_alarms(self):
        """Alarm recovery sweep must skip service_id=2 rows and not call HubSpot."""
        async with self.async_session() as session:
            res = await self._create_result(session, service_id=2, service_name="EXPAC")
            # Set stale pending status
            from datetime import timedelta
            stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            res.hubspot_ticket_status = "pending"
            res.created_at = stale_time
            res.updated_at = stale_time
            await session.commit()

            with patch.object(HubSpotService, "create_ticket", new=AsyncMock()) as mock_create:
                stats = await MassEvaluationService.run_alarm_tickets_recovery_sweep(session)
                mock_create.assert_not_called()
                self.assertEqual(stats.get("recovered", 0), 0)
                self.assertGreaterEqual(stats.get("skipped", 0), 1)


if __name__ == "__main__":
    unittest.main()
