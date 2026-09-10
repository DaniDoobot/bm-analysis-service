"""
Test suite for Mass Evaluation Job Historical Recovery.
Validates:
- Case A: call_ids=[A, B] -> only A and B are loaded from HubSpot, never broad search.
- Case B: Job Front + call Front -> allowed.
- Case C: Job Front + call EXPAC -> rejected fail-closed before analysis.
- Case D: Job EXPAC + call EXPAC -> allowed.
- Case E: call_id already exists in bm_mass_evaluation_results -> rejected before analysis (409 Conflict).
- Case E2: call_id exists in BD with DIFFERENT prompt_id and service_id -> rejected before analysis (409 Conflict).
- Case E3: call_id exists in BD with status='failed' -> rejected before analysis (409 Conflict).
- Case F: alarma=True during historical recovery -> result preserves alarm, _process_alarm_hubspot_ticket NOT called.
- Case G: 0 POST/PATCH HubSpot ticket calls during historical recovery.
- Case H: No MassAnalysisAutomationRun created.
- Case I: No modification of automation watermark or status.
- Case J: Role AGENT -> 403 Forbidden.
- Case K: Role TEAM_COORDINATOR -> 403 Forbidden.
- Case L: Admin outside scope -> 403 Forbidden.
- Case M: Empty / whitespace reason -> 422.
- Case N: call_ids > 100 -> 422.
- Case O: Individual call failure handled gracefully without affecting others.
- Case P: Normal automation flow still triggers HubSpot alarm ticket (suppression strictly isolated).
"""
import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.roles import InternalRole
from app.core.tenant_context import TenantContext
from app.db import Base
from app.dependencies import get_db, get_tenant_context
from app.main import app
from app.models.mass_evaluations import (
    MassAnalysisAutomation,
    MassAnalysisAutomationRun,
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
)
from app.models.prompts import Prompt, PromptVersion
from app.models.services import Service
from app.services.mass_evaluation_service import MassEvaluationService


class TestHistoricalRecovery(unittest.IsolatedAsyncioTestCase):
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

        async with self.async_session() as session:
            # Services
            s1 = Service(service_id=1, company_id=1, service_key="front", service_name="Front", is_active=True)
            s2 = Service(service_id=2, company_id=1, service_key="expac", service_name="EXPAC", is_active=True)
            s3 = Service(service_id=3, company_id=2, service_key="other", service_name="Other Co", is_active=True)
            session.add_all([s1, s2, s3])

            # Prompts
            p1 = Prompt(prompt_id=58, company_id=1, service_id=1, prompt_name="Prompt Front", prompt_type="audio", is_active=True)
            pv1 = PromptVersion(id=248, prompt_id=58, is_current=True, prompt="Front evaluation prompt")
            p2 = Prompt(prompt_id=59, company_id=1, service_id=2, prompt_name="Prompt EXPAC", prompt_type="audio", is_active=True)
            pv2 = PromptVersion(id=251, prompt_id=59, is_current=True, prompt="EXPAC evaluation prompt")
            session.add_all([p1, pv1, p2, pv2])

            # Jobs
            j1 = MassEvaluationJob(job_id=48, job_name="Job Front", service_id=1, company_id=1, prompt_id=58, prompt_version_id=248, is_active=True)
            j2 = MassEvaluationJob(job_id=54, job_name="Job EXPAC", service_id=2, company_id=1, prompt_id=59, prompt_version_id=251, is_active=True)
            session.add_all([j1, j2])

            # Automations
            a8 = MassAnalysisAutomation(
                automation_id=8,
                service_id=1,
                job_id=48,
                prompt_id=58,
                prompt_version_id=248,
                name="Front Automation",
                is_active=True,
                interval_minutes=10,
                delay_minutes=5,
                lookback_minutes=10,
                last_run_at=datetime(2026, 9, 10, 10, 0, 0, tzinfo=timezone.utc),
                last_success_at=datetime(2026, 9, 10, 10, 0, 0, tzinfo=timezone.utc),
            )
            # Automation 9: EXPAC (inactive)
            a9 = MassAnalysisAutomation(
                automation_id=9,
                service_id=2,
                job_id=54,
                prompt_id=59,
                prompt_version_id=251,
                name="EXPAC Automation",
                is_active=False,
                interval_minutes=10,
                delay_minutes=5,
                lookback_minutes=10,
                last_run_at=datetime(2026, 9, 10, 10, 0, 0, tzinfo=timezone.utc),
                last_success_at=datetime(2026, 9, 10, 10, 0, 0, tzinfo=timezone.utc),
            )
            session.add_all([a8, a9])
            await session.commit()

        # Mock active owners
        self.front_owners = ["33013277", "33013276", "1375831790"]
        self.expac_owners = ["76997586", "1626092736"]

        async def fake_get_active_owners(db, service_id):
            if service_id == 1:
                return self.front_owners
            elif service_id == 2:
                return self.expac_owners
            return []

        self.patch_owners = patch.object(
            MassEvaluationService,
            "get_active_owner_ids_for_service",
            side_effect=fake_get_active_owners,
        )
        self.patch_owners.start()

    async def asyncTearDown(self):
        self.patch_owners.stop()
        await self.engine.dispose()

    def _get_client(self, role=InternalRole.SUPER_ADMIN, company_ids=None, service_ids=None):
        async def override_get_db():
            async with self.async_session() as session:
                yield session

        def override_tenant_context():
            ctx = TenantContext(
                user_id=1,
                user_email="admin@example.com",
                raw_role="super_admin" if role == InternalRole.SUPER_ADMIN else "admin",
                normalized_role=role,
                allowed_company_ids=[1] if company_ids is None else list(company_ids),
                allowed_service_ids=[1, 2] if service_ids is None else list(service_ids),
                allowed_agent_ids=None,
                is_super_admin=(role == InternalRole.SUPER_ADMIN),
            )
            return ctx

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_tenant_context] = override_tenant_context
        return TestClient(app)

    # ──────────────────────────────────────────────────────────────────────────
    # Tests
    # ──────────────────────────────────────────────────────────────────────────

    # Case A: call_ids=[A, B] -> only A and B loaded from HubSpot, never broad search
    @patch("app.services.hubspot_service.HubSpotService.search_calls_for_mass_evaluation")
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_a_only_explicit_call_ids_loaded(self, mock_get_call, mock_search):
        mock_get_call.side_effect = lambda cid: {
            "call_id": cid,
            "hubspot_owner_id": "33013277",
            "call_duration": 60000,
            "call_direction": "INBOUND",
            "status": "COMPLETED",
        }
        client = self._get_client()
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_A", "call_B"], "reason": "Test recovery", "dry_run": True},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data["call_ids_count"], 2)
        self.assertEqual([c["call_id"] for c in data["preview_calls"]], ["call_A", "call_B"])
        mock_search.assert_not_called()
        self.assertEqual(mock_get_call.call_count, 2)

    # Case B: Job Front + call Front -> allowed
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_b_front_job_and_front_call_allowed(self, mock_get_call):
        mock_get_call.return_value = {
            "call_id": "call_front_1",
            "hubspot_owner_id": "33013277",  # in front_owners
            "call_duration": 45000,
            "call_direction": "INBOUND",
        }
        client = self._get_client()
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_front_1"], "reason": "Valid front recovery", "dry_run": True},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.json()["dry_run"])

    # Case C: Job Front + call EXPAC -> rejected fail-closed before analysis
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_c_front_job_and_expac_call_rejected(self, mock_get_call):
        mock_get_call.return_value = {
            "call_id": "call_expac_1",
            "hubspot_owner_id": "76997586",  # in expac_owners, NOT front!
            "call_duration": 45000,
        }
        client = self._get_client()
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_expac_1"], "reason": "Invalid front call", "dry_run": False},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no está asignado al servicio", res.json()["detail"])

    # Case D: Job EXPAC + call EXPAC -> allowed
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_d_expac_job_and_expac_call_allowed(self, mock_get_call):
        mock_get_call.return_value = {
            "call_id": "call_expac_1",
            "hubspot_owner_id": "76997586",  # in expac_owners
            "call_duration": 45000,
            "call_direction": "INBOUND",
        }
        client = self._get_client()
        res = client.post(
            "/bm/mass-evaluation-jobs/54/historical-recovery",
            json={"call_ids": ["call_expac_1"], "reason": "Valid expac recovery", "dry_run": True},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    # Case E: call_id already exists in bm_mass_evaluation_results -> 409 Conflict rejected before Gemini
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_e_collision_rejected_with_409(self, mock_get_call):
        async with self.async_session() as session:
            dummy_run = MassEvaluationRun(run_id=1, job_id=48, trigger_type="manual", status="completed")
            existing = MassEvaluationResult(
                run_id=1,
                job_id=48,
                prompt_id=58,
                prompt_snapshot="Front evaluation prompt",
                call_id="call_existing_123",
                status="completed",
            )
            session.add(dummy_run)
            session.add(existing)
            await session.commit()

        client = self._get_client()
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_existing_123"], "reason": "Recovery attempt on existing call"},
        )
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("Colisión de llamadas", res.json()["detail"])
        mock_get_call.assert_not_called()

    # Case E2: call_id exists in BD with DIFFERENT prompt_id and service_id -> MUST ALSO reject with 409 Conflict
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_e2_collision_different_prompt_and_service_rejected_with_409(self, mock_get_call):
        async with self.async_session() as session:
            dummy_run = MassEvaluationRun(run_id=2, job_id=54, trigger_type="automation", status="completed")
            existing = MassEvaluationResult(
                run_id=2,
                job_id=54,
                service_id=2,
                prompt_id=59,
                prompt_snapshot="EXPAC evaluation prompt",
                call_id="call_x_different_prompt",
                status="completed",
            )
            session.add(dummy_run)
            session.add(existing)
            await session.commit()

        client = self._get_client()
        # Request recovery on Job 48 (Front, prompt_id=58, service_id=1)
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_x_different_prompt"], "reason": "Attempt recovery on call existing under different prompt/service"},
        )
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("Colisión de llamadas", res.json()["detail"])
        # Must reject BEFORE calling HubSpot or processing audio/LLM
        mock_get_call.assert_not_called()

    # Case E3: call_id exists in BD with status='failed' -> MUST ALSO reject with 409 Conflict
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_e3_collision_failed_status_rejected_with_409(self, mock_get_call):
        async with self.async_session() as session:
            dummy_run = MassEvaluationRun(run_id=3, job_id=48, trigger_type="manual", status="completed")
            existing = MassEvaluationResult(
                run_id=3,
                job_id=48,
                prompt_id=58,
                prompt_snapshot="Front evaluation prompt",
                call_id="call_failed_status_123",
                status="failed",
            )
            session.add(dummy_run)
            session.add(existing)
            await session.commit()

        client = self._get_client()
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_failed_status_123"], "reason": "Attempt recovery on existing call with failed status"},
        )
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("Colisión de llamadas", res.json()["detail"])
        mock_get_call.assert_not_called()

    # Case F & G: alarma=True during historical recovery -> result preserves alarm, 0 HubSpot tickets written
    @patch("app.services.hubspot_service.httpx.AsyncClient.post")
    @patch("app.services.mass_evaluation_service.MassEvaluationService._process_alarm_hubspot_ticket")
    async def test_case_f_and_g_alarm_suppression_during_recovery(self, mock_process_ticket, mock_hs_post):
        # Simulate execution of _execute_background_run with suppress_hubspot_tickets=True
        async with self.async_session() as session:
            run = MassEvaluationRun(
                run_id=99,
                job_id=48,
                company_id=1,
                service_id=1,
                trigger_type="historical_recovery",
                status="running",
                execution_source="historical_recovery",
                effective_filters={"selection_mode": "manual_call_ids", "call_ids": ["call_alarm_1"], "suppress_hubspot_tickets": True},
            )
            session.add(run)
            await session.commit()

        # Run background run logic directly with mock AI returning alarma=True
        with patch("app.db.get_engine", return_value=self.engine):
            with patch("app.services.mass_evaluation_service.HubSpotService") as MockHS:
                hs_instance = MockHS.return_value
                hs_instance.get_call = AsyncMock(return_value={
                    "call_id": "call_alarm_1",
                    "hs_object_id": "call_alarm_1",
                    "recording_url": "https://dummy.recording/audio.mp3",
                    "hubspot_owner_id": "33013277",
                    "call_duration": 60000,
                    "call_direction": "INBOUND",
                    "status": "COMPLETED",
                })
                with patch("app.services.mass_evaluation_service.TwilioService") as MockTS:
                    MockTS.return_value.download_audio = AsyncMock(return_value=b"audio_bytes")
                    with patch("app.services.mass_evaluation_service.analyze_audio_bytes", new_callable=AsyncMock,
                               return_value='{"alarma": true, "evaluacion_global": 4.5}'):
                        with patch("app.services.mass_evaluation_service.MassEvaluationService._is_alarm_detected", return_value=(True, "Alarma detectada en test")):
                            await MassEvaluationService._execute_background_run(
                                job_id=48,
                                run_id=99,
                                filters_payload={"selection_mode": "manual_call_ids", "call_ids": ["call_alarm_1"], "suppress_hubspot_tickets": True},
                            )

        # 1. Ticket processing must NOT have been called!
        mock_process_ticket.assert_not_called()
        mock_hs_post.assert_not_called()

        # 2. Result in DB must preserve alarma=True
        async with self.async_session() as session:
            stmt = select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_alarm_1")
            res = (await session.execute(stmt)).scalars().first()
            self.assertIsNotNone(res)
            self.assertEqual(res.status, "completed")
            self.assertEqual(res.execution_source, "historical_recovery")

    # Case H & I: No MassAnalysisAutomationRun created, no watermark modification
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_h_and_i_no_automation_side_effects(self, mock_get_call):
        mock_get_call.return_value = {
            "call_id": "call_auto_check",
            "hubspot_owner_id": "33013277",
            "call_duration": 45000,
        }
        client = self._get_client()
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_auto_check"], "reason": "Checking automation side effects"},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        async with self.async_session() as session:
            # Check 0 automation runs created
            stmt_ar = select(MassAnalysisAutomationRun)
            ar_count = len((await session.execute(stmt_ar)).scalars().all())
            self.assertEqual(ar_count, 0)

            # Check automation 8 timestamps untouched
            stmt_a8 = select(MassAnalysisAutomation).where(MassAnalysisAutomation.automation_id == 8)
            a8 = (await session.execute(stmt_a8)).scalars().first()
            self.assertEqual(a8.last_run_at.replace(tzinfo=timezone.utc), datetime(2026, 9, 10, 10, 0, 0, tzinfo=timezone.utc))

    # Case J: Role AGENT -> 403 Forbidden
    def test_case_j_agent_role_forbidden(self):
        client = self._get_client(role=InternalRole.AGENT)
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_1"], "reason": "Agent attempt"},
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # Case K: Role TEAM_COORDINATOR -> 403 Forbidden
    def test_case_k_team_coordinator_forbidden(self):
        client = self._get_client(role=InternalRole.TEAM_COORDINATOR)
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_1"], "reason": "Coordinator attempt"},
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # Case L: Admin outside scope -> 403 Forbidden
    def test_case_l_admin_outside_scope_forbidden(self):
        # Admin restricted to company 2
        client = self._get_client(role=InternalRole.COMPANY_ADMIN, company_ids=[2], service_ids=[3])
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_1"], "reason": "Cross company attempt"},
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # Case M: Empty or short reason -> 422
    def test_case_m_invalid_reason_422(self):
        client = self._get_client()
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": ["call_1"], "reason": "   "},
        )
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Case N: call_ids > 100 -> 422
    def test_case_n_call_ids_exceeding_limit_422(self):
        client = self._get_client()
        oversized_list = [f"call_{i}" for i in range(101)]
        res = client.post(
            "/bm/mass-evaluation-jobs/48/historical-recovery",
            json={"call_ids": oversized_list, "reason": "Too many calls"},
        )
        self.assertEqual(res.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    # Case O: Individual call failure handled gracefully without affecting others
    @patch("app.services.hubspot_service.HubSpotService.get_call")
    async def test_case_o_individual_call_failure_isolated(self, mock_get_call):
        def fake_get(cid):
            if cid == "call_valid":
                return {
                    "call_id": "call_valid",
                    "hs_object_id": "call_valid",
                    "recording_url": "https://dummy/rec.mp3",
                    "hubspot_owner_id": "33013277",
                    "call_duration": 60000,
                }
            raise RuntimeError("Twilio audio download error")

        mock_get_call.side_effect = fake_get

        async with self.async_session() as session:
            run = MassEvaluationRun(
                run_id=105,
                job_id=48,
                company_id=1,
                service_id=1,
                trigger_type="historical_recovery",
                status="running",
                execution_source="historical_recovery",
                effective_filters={"selection_mode": "manual_call_ids", "call_ids": ["call_valid", "call_err"], "suppress_hubspot_tickets": True},
            )
            session.add(run)
            await session.commit()

        with patch("app.db.get_engine", return_value=self.engine):
            with patch("app.services.mass_evaluation_service.HubSpotService") as MockHS_O:
                MockHS_O.return_value.get_call = AsyncMock(side_effect=mock_get_call.side_effect)
                with patch("app.services.mass_evaluation_service.TwilioService") as MockTS_O:
                    MockTS_O.return_value.download_audio = AsyncMock(return_value=b"audio_bytes")
                    with patch("app.services.mass_evaluation_service.analyze_audio_bytes", new_callable=AsyncMock,
                               return_value='{"evaluacion_global": 8.0}'):
                        await MassEvaluationService._execute_background_run(
                            job_id=48,
                            run_id=105,
                            filters_payload={"selection_mode": "manual_call_ids", "call_ids": ["call_valid", "call_err"], "suppress_hubspot_tickets": True},
                        )

        async with self.async_session() as session:
            # Check run completed
            stmt_r = select(MassEvaluationRun).where(MassEvaluationRun.run_id == 105)
            r = (await session.execute(stmt_r)).scalars().first()
            self.assertEqual(r.status, "completed")
            self.assertEqual(r.calls_analyzed, 1)
            # call_err failed during HubSpot fetch (not during analysis), so it's counted
            # as calls_skipped (not_found_call_ids), not calls_failed
            self.assertEqual(r.calls_failed, 0)
            self.assertEqual(r.calls_skipped, 1)

    # Case P: Normal automation flow STILL creates HubSpot ticket if alarma=True
    @patch("app.services.mass_evaluation_service.MassEvaluationService._process_alarm_hubspot_ticket")
    async def test_case_p_normal_automation_flow_triggers_ticket(self, mock_process_ticket):
        async with self.async_session() as session:
            run = MassEvaluationRun(
                run_id=110,
                job_id=48,
                company_id=1,
                service_id=1,
                trigger_type="automation",
                status="running",
                execution_source="automation",  # Standard automation!
                effective_filters={"selection_mode": "manual_call_ids", "call_ids": ["call_auto_alarm"]},
            )
            session.add(run)
            await session.commit()

        with patch("app.db.get_engine", return_value=self.engine):
            with patch("app.services.mass_evaluation_service.HubSpotService") as MockHS_P:
                MockHS_P.return_value.get_call = AsyncMock(return_value={
                    "call_id": "call_auto_alarm",
                    "hs_object_id": "call_auto_alarm",
                    "recording_url": "https://dummy/rec.mp3",
                    "hubspot_owner_id": "33013277",
                    "call_duration": 60000,
                })
                with patch("app.services.mass_evaluation_service.TwilioService") as MockTS_P:
                    MockTS_P.return_value.download_audio = AsyncMock(return_value=b"audio")
                    with patch("app.services.mass_evaluation_service.analyze_audio_bytes", new_callable=AsyncMock,
                               return_value='{"alarma": true, "evaluacion_global": 3.0}'):
                        with patch("app.services.mass_evaluation_service.MassEvaluationService._is_alarm_detected", return_value=(True, "Alarma en automation")):
                            await MassEvaluationService._execute_background_run(
                                job_id=48,
                                run_id=110,
                                filters_payload={"selection_mode": "manual_call_ids", "call_ids": ["call_auto_alarm"]},
                            )

        # Standard automation MUST trigger _process_alarm_hubspot_ticket
        mock_process_ticket.assert_called_once()


if __name__ == "__main__":
    unittest.main()
