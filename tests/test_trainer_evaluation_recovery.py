import os
import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock, AsyncMock

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_trainer_recovery.db"

from sqlalchemy import BigInteger, delete, select, and_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.db import Base, get_engine
from app.dependencies import get_current_user, get_tenant_context
from app.core.tenant_context import TenantContext
from app.core.roles import InternalRole
from app.models.users import User
from app.models.companies import Company
from app.models.services import Service
from app.models.trainer import TrainerSession, TrainerSimulation, TrainerEvaluation


class TestTrainerEvaluationRecovery(unittest.IsolatedAsyncioTestCase):
    """Exhaustive test suite for recovering stuck Trainer simulations (12 scenarios)."""

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            co = await db.get(Company, 1)
            if not co:
                co = Company(company_id=1, company_name="BMG", company_key="bmg", is_active=True)
                db.add(co)
                await db.commit()

            svc = await db.get(Service, 1)
            if not svc:
                svc = Service(service_id=1, company_id=1, service_name="Facturacion", service_key="facturacion", is_active=True)
                db.add(svc)
                await db.commit()

            sim = await db.get(TrainerSimulation, 1)
            if not sim:
                sim = TrainerSimulation(
                    simulation_id=1,
                    service_id=1,
                    company_id=1,
                    name="ATEN01 - Facturacion Compleja",
                    code="ATEN01",
                    objective="Test",
                    roleplay_prompt="Test roleplay",
                    status="published"
                )
                db.add(sim)
                await db.commit()

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ── Scenario 1: recording_completed over session started -> completed + evaluation ──
    @patch("app.routers.trainer_voice.check_and_trigger_evaluation", new_callable=AsyncMock)
    async def test_1_recording_completed_promotes_started_session_to_completed(self, mock_trigger):
        call_sid = "CA_test_started_101"
        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=901,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id=call_sid,
                status="started",
                evaluation_status="started",
                recording_url=None,
                duration_seconds=None
            )
            db.add(sess)
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post(
                "/bm/trainer/phone/recording-completed",
                data={
                    "CallSid": call_sid,
                    "RecordingUrl": "https://api.twilio.com/RE101.wav",
                    "RecordingStatus": "completed",
                    "RecordingDuration": "101"
                }
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["status"], "ok")

        async with AsyncSession(self.engine) as db:
            updated = await db.get(TrainerSession, 901)
            self.assertEqual(updated.status, "completed")
            self.assertEqual(updated.duration_seconds, 101)
            self.assertEqual(updated.recording_url, "https://api.twilio.com/RE101.wav")
            self.assertIsNotNone(updated.ended_at)
            mock_trigger.assert_called_once()

    # ── Scenario 2: recording_url existing over session started -> does not wait for stream ──
    @patch("app.services.trainer_service.TrainerService.evaluate_session_task", new_callable=AsyncMock)
    async def test_2_check_and_trigger_evaluation_triggers_when_recording_exists_on_started(self, mock_eval):
        from app.routers.trainer_voice import check_and_trigger_evaluation

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=902,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_trigger_902",
                status="started",
                evaluation_status="started",
                recording_url="https://api.twilio.com/mock_rec_902.wav",
                duration_seconds=101
            )
            db.add(sess)
            await db.commit()

            await check_and_trigger_evaluation(db, 902)

            updated = await db.get(TrainerSession, 902)
            self.assertEqual(updated.status, "completed")
            self.assertEqual(updated.evaluation_status, "evaluation_pending")

    # ── Scenario 3: session completed with recording but evaluation_status started -> evaluates ──
    @patch("app.services.trainer_service.TrainerService.evaluate_session_task", new_callable=AsyncMock)
    async def test_3_completed_with_recording_evaluates(self, mock_eval):
        from app.routers.trainer_voice import check_and_trigger_evaluation

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=903,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_completed_903",
                status="completed",
                evaluation_status="started",
                recording_url="https://api.twilio.com/mock_rec_903.wav",
                duration_seconds=60
            )
            db.add(sess)
            await db.commit()

            await check_and_trigger_evaluation(db, 903)

            updated = await db.get(TrainerSession, 903)
            self.assertEqual(updated.evaluation_status, "evaluation_pending")

    # ── Scenario 4: two simultaneous triggers -> atomic guard prevents duplicate evaluations ──
    @patch("app.services.trainer_service.TrainerService.evaluate_session_task", new_callable=AsyncMock)
    async def test_4_concurrent_triggers_prevent_duplicate_evaluations(self, mock_eval):
        from app.routers.trainer_voice import check_and_trigger_evaluation

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=904,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_concurrent_904",
                status="completed",
                evaluation_status="completed_waiting_recording",
                recording_url="https://api.twilio.com/mock_rec_904.wav",
                duration_seconds=75
            )
            db.add(sess)
            await db.commit()

        # Run two triggers concurrently in separate DB sessions
        async with AsyncSession(self.engine) as db1, AsyncSession(self.engine) as db2:
            await asyncio.gather(
                check_and_trigger_evaluation(db1, 904),
                check_and_trigger_evaluation(db2, 904)
            )

        # Allow background task to settle
        await asyncio.sleep(0.1)

        async with AsyncSession(self.engine) as db:
            updated = await db.get(TrainerSession, 904)
            self.assertEqual(updated.evaluation_status, "evaluation_pending")

        # evaluate_session_task should be called exactly once
        self.assertEqual(mock_eval.call_count, 1)

    # ── Scenario 5: reconcile recovers started + recording ──
    @patch("app.services.trainer_service.TrainerService.evaluate_session_task", new_callable=AsyncMock)
    async def test_5_reconcile_recovers_started_with_recording(self, mock_eval):
        admin_user = User(user_id=1, email="admin@test.com", role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin_user

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=905,
                agent_id="demo_owner_11",
                agent_code="DEMO11",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_rec_905",
                status="started",
                evaluation_status="started",
                recording_url="https://api.twilio.com/mock_rec_905.wav",
                duration_seconds=95
            )
            db.add(sess)
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/trainer/phone/sessions/reconcile")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "ok")
            reconciled_ids = [r["session_id"] for r in data["reconciled"]]
            self.assertIn(905, reconciled_ids)

        async with AsyncSession(self.engine) as db:
            updated = await db.get(TrainerSession, 905)
            self.assertEqual(updated.status, "completed")
            self.assertEqual(updated.evaluation_status, "evaluation_pending")

    # ── Scenario 6: reconcile recovers completed + recording without evaluation ──
    @patch("app.services.trainer_service.TrainerService.evaluate_session_task", new_callable=AsyncMock)
    async def test_6_reconcile_recovers_completed_with_recording_no_eval(self, mock_eval):
        admin_user = User(user_id=1, email="admin@test.com", role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin_user

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=906,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_rec_906",
                status="completed",
                evaluation_status="completed_waiting_recording",
                recording_url="https://api.twilio.com/mock_rec_906.wav",
                duration_seconds=110
            )
            db.add(sess)
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/trainer/phone/sessions/reconcile")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            reconciled_ids = [r["session_id"] for r in data["reconciled"]]
            self.assertIn(906, reconciled_ids)

        async with AsyncSession(self.engine) as db:
            updated = await db.get(TrainerSession, 906)
            self.assertEqual(updated.evaluation_status, "evaluation_pending")

    # ── Scenario 7: reconcile detects old session without recording and queries Twilio ──
    @patch("httpx.AsyncClient.get")
    @patch("app.services.trainer_service.TrainerService.evaluate_session_task", new_callable=AsyncMock)
    async def test_7_reconcile_queries_twilio_for_missing_recording(self, mock_eval, mock_twilio_get):
        admin_user = User(user_id=1, email="admin@test.com", role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin_user

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=907,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_twilio_query_907",
                status="completed",
                evaluation_status="completed_waiting_recording",
                recording_url=None,
                duration_seconds=None
            )
            db.add(sess)
            await db.commit()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "recordings": [
                {
                    "sid": "RE907",
                    "status": "completed",
                    "duration": "105",
                    "uri": "/2010-04-01/Accounts/ACmock/Recordings/RE907.json"
                }
            ]
        }
        mock_twilio_get.return_value = mock_resp

        with patch("app.routers.trainer_voice.settings") as mock_settings:
            mock_settings.twilio_account_sid = "ACmock"
            mock_settings.twilio_auth_token = "token_mock"

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                res = await ac.post("/bm/trainer/phone/sessions/reconcile")
                self.assertEqual(res.status_code, 200)
                data = res.json()
                reconciled_ids = [r["session_id"] for r in data["reconciled"]]
                self.assertIn(907, reconciled_ids)

        async with AsyncSession(self.engine) as db:
            updated = await db.get(TrainerSession, 907)
            self.assertEqual(updated.recording_url, "https://api.twilio.com/2010-04-01/Accounts/ACmock/Recordings/RE907.wav")
            self.assertEqual(updated.duration_seconds, 105)
            self.assertEqual(updated.evaluation_status, "evaluation_pending")

    # ── Scenario 8: retry-evaluation works for authorized user ──
    @patch("app.services.trainer_service.TrainerService.evaluate_session_task", new_callable=AsyncMock)
    async def test_8_retry_trainer_session_evaluation_success(self, mock_eval):
        admin_ctx = TenantContext(
            user_id=1,
            user_email="admin@test.com",
            raw_role="admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=None
        )
        app.dependency_overrides[get_tenant_context] = lambda: admin_ctx

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=908,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_retry_908",
                status="completed",
                evaluation_status="evaluation_error",
                recording_url="https://api.twilio.com/mock_rec_908.wav",
                duration_seconds=101
            )
            db.add(sess)
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/trainer/phone/sessions/908/retry-evaluation")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["status"], "ok")

        async with AsyncSession(self.engine) as db:
            updated = await db.get(TrainerSession, 908)
            self.assertEqual(updated.evaluation_status, "evaluation_pending")

    # ── Scenario 9: retry-evaluation denies unauthorized access to another agent's session ──
    async def test_9_retry_evaluation_denies_unauthorized_agent(self):
        agent_ctx = TenantContext(
            user_id=2,
            user_email="agent2@test.com",
            raw_role="agent",
            normalized_role=InternalRole.AGENT,
            is_super_admin=False,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=["demo_owner_02"]  # does NOT own demo_owner_01
        )
        app.dependency_overrides[get_tenant_context] = lambda: agent_ctx

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=909,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_unauth_909",
                status="completed",
                evaluation_status="evaluation_error",
                recording_url="https://api.twilio.com/mock_rec_909.wav",
                duration_seconds=80
            )
            db.add(sess)
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/trainer/phone/sessions/909/retry-evaluation")
            self.assertEqual(res.status_code, 403)
            self.assertIn("No tienes permisos", res.json()["detail"])

    # ── Scenario 10: unexpected worker error -> evaluation_error ──
    async def test_10_unexpected_worker_error_sets_evaluation_error(self):
        from app.routers.trainer_voice import check_and_trigger_evaluation

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=910,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_crash_910",
                status="completed",
                evaluation_status="completed_waiting_recording",
                recording_url="https://api.twilio.com/mock_rec_910.wav",
                duration_seconds=65
            )
            db.add(sess)
            await db.commit()

        # Patch evaluate_session_task to raise an unexpected catastrophic error
        with patch("app.services.trainer_service.TrainerService.evaluate_session_task", side_effect=RuntimeError("Simulated LLM API crash")):
            async with AsyncSession(self.engine) as db:
                await check_and_trigger_evaluation(db, 910)

            # Allow the background task to run and catch exception
            await asyncio.sleep(0.3)

        async with AsyncSession(self.engine) as db:
            updated = await db.get(TrainerSession, 910)
            self.assertEqual(updated.evaluation_status, "evaluation_error")
            stmt_ev = select(TrainerEvaluation).where(TrainerEvaluation.session_id == 910)
            res_ev = await db.execute(stmt_ev)
            eval_record = res_ev.scalars().first()
            self.assertIsNotNone(eval_record)
            self.assertIn("Simulated LLM API crash", eval_record.error_message)

    # ── Scenario 11: evaluation in running is not duplicated ──
    @patch("app.services.trainer_service.TrainerService.evaluate_session_task", new_callable=AsyncMock)
    async def test_11_evaluation_running_not_duplicated(self, mock_eval):
        from app.routers.trainer_voice import check_and_trigger_evaluation

        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=911,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id="CA_test_running_911",
                status="completed",
                evaluation_status="running",
                recording_url="https://api.twilio.com/mock_rec_911.wav",
                duration_seconds=70
            )
            db.add(sess)
            await db.commit()

            await check_and_trigger_evaluation(db, 911)

        # evaluate_session_task should NOT be called because it's already running
        self.assertEqual(mock_eval.call_count, 0)

        # Also test that retry endpoint rejects running session with 409
        admin_ctx = TenantContext(
            user_id=1,
            user_email="admin@test.com",
            raw_role="admin",
            normalized_role=InternalRole.SUPER_ADMIN,
            is_super_admin=True,
            allowed_company_ids=[1],
            allowed_service_ids=[1],
            allowed_agent_ids=None
        )
        app.dependency_overrides[get_tenant_context] = lambda: admin_ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/bm/trainer/phone/sessions/911/retry-evaluation")
            self.assertEqual(res.status_code, 409)

    # ── Scenario 12: failed / short calls (<15s) do not remain in started ──
    async def test_12_short_call_marked_failed_not_started(self):
        call_sid = "CA_test_short_912"
        async with AsyncSession(self.engine) as db:
            sess = TrainerSession(
                session_id=912,
                agent_id="demo_owner_01",
                agent_code="DEMO01",
                simulation_id=1,
                service_id=1,
                call_id=call_sid,
                status="started",
                evaluation_status="started",
                recording_url=None,
                duration_seconds=None
            )
            db.add(sess)
            await db.commit()

        # Twilio sends webhook with duration 8 seconds (< 15s)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post(
                "/bm/trainer/phone/recording-completed",
                data={
                    "CallSid": call_sid,
                    "RecordingUrl": "https://api.twilio.com/RE_short.wav",
                    "RecordingStatus": "completed",
                    "RecordingDuration": "8"
                }
            )
            self.assertEqual(res.status_code, 200)

        async with AsyncSession(self.engine) as db:
            updated = await db.get(TrainerSession, 912)
            self.assertEqual(updated.status, "failed")
            self.assertEqual(updated.evaluation_status, "failed")
            self.assertEqual(updated.duration_seconds, 8)


if __name__ == "__main__":
    unittest.main()
