"""
Tests for cycle simulation recording audio endpoints, authorization scoping,
and recording_url field exposure across Agent and Admin views.
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_cycle_audio.db"
os.environ["GEMINI_API_KEY"] = "mock_key"

from sqlalchemy import text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from httpx import AsyncClient, ASGITransport, Request, Response, HTTPStatusError
from app.db import get_engine, Base, AsyncSessionLocal
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.personalized_training import (
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCallSession,
    TrainingCallEvaluation,
    TrainingCompletionStatus,
    TrainingAgentSetting,
)
from app.services.personalized_training_service import PersonalizedTrainingService
from app.dependencies import get_current_user
from app.main import app


class TestCycleRecordingAudio(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("test_cycle_audio.db"):
            try:
                os.remove("test_cycle_audio.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        now = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as db:
            # Companies
            c1 = Company(company_id=1, company_name="Company 1", company_key="comp1")
            c2 = Company(company_id=2, company_name="Company 2", company_key="comp2")
            db.add_all([c1, c2])
            await db.flush()

            # Services
            s1 = Service(service_id=1, company_id=1, service_key="svc_c1", service_name="Service 1")
            s2 = Service(service_id=2, company_id=2, service_key="svc_c2", service_name="Service 2")
            db.add_all([s1, s2])
            await db.flush()

            # Teams
            t1 = Team(team_id=1, company_id=1, service_id=1, team_name="Team Alpha")
            t2 = Team(team_id=2, company_id=1, service_id=1, team_name="Team Beta")
            db.add_all([t1, t2])
            await db.flush()

            # Users
            # 1. Superadmin
            u_super = User(user_id=1, username="super", email="super@test.com", password_hash="hash", role="super_admin", is_active=True, company_id=None)
            # 2. Company Admin (Company 1)
            u_admin1 = User(user_id=2, username="admin1", email="admin1@test.com", password_hash="hash", role="company_admin", is_active=True, company_id=1)
            # 3. Company Admin (Company 2)
            u_admin2 = User(user_id=3, username="admin2", email="admin2@test.com", password_hash="hash", role="company_admin", is_active=True, company_id=2)
            # 4. Coordinator of Team Alpha
            u_coord = User(user_id=4, username="coord", email="coord@test.com", password_hash="hash", role="team_coordinator", is_active=True, company_id=1, primary_team_id=1)
            # 5. Agent 101 in Team Alpha
            u_agent1 = User(user_id=5, username="agent101", email="agent101@test.com", password_hash="hash", role="agent", is_active=True, company_id=1, hubspot_owner_id="OWNER_101", primary_team_id=1)
            # 6. Agent 102 in Team Beta
            u_agent2 = User(user_id=6, username="agent102", email="agent102@test.com", password_hash="hash", role="agent", is_active=True, company_id=1, hubspot_owner_id="OWNER_102", primary_team_id=2)

            db.add_all([u_super, u_admin1, u_admin2, u_coord, u_agent1, u_agent2])
            await db.flush()

            # Association Coordinator -> Team Alpha
            db.add(UserTeamAssociation(user_id=4, team_id=1))
            # Association Agent 101 -> Team Alpha
            db.add(AgentTeamAssociation(user_id=5, team_id=1))
            # Association Agent 102 -> Team Beta
            db.add(AgentTeamAssociation(user_id=6, team_id=2))
            await db.flush()

            # Training Agent Setting for Agent 101
            setting1 = TrainingAgentSetting(
                setting_id=1,
                company_id=1,
                hubspot_owner_id="OWNER_101",
                agent_name="Agent One",
                agent_initials="AO",
                is_enabled=True,
            )
            db.add(setting1)
            await db.flush()

            # Training report for Agent 101 (Company 1, Service 1)
            rep1 = TrainingAgentReport(
                training_report_id=100,
                company_id=1,
                service_id=1,
                hubspot_owner_id="OWNER_101",
                agent_name="Agent One",
                agent_initials="AO",
                period_start=now - timedelta(days=30),
                period_end=now,
                status="in_progress",
                is_current=True,
                evaluations_count=2,
                calls_count=2,
            )
            db.add(rep1)
            await db.flush()

            # Simulation prompt
            sim_prompt = TrainingSimulationPrompt(
                simulation_prompt_id=10,
                training_report_id=100,
                hubspot_owner_id="OWNER_101",
                prompt_number=1,
                title="Simulación 1: Consulta",
                scenario_type="roleplay",
                prompt_text="Instrucción de roleplay confidencial",
            )
            db.add(sim_prompt)
            await db.flush()

            # Call Session with recording
            sess_with_rec = TrainingCallSession(
                session_id=500,
                call_sid="CA_CYCLE_500",
                recording_sid="RE_CYCLE_500",
                recording_url="https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/RE500.mp3",
                agent_id="OWNER_101",
                cycle_id=100,
                conversation_id=10,
                status="completed",
                started_at=now - timedelta(hours=2),
                ended_at=now - timedelta(hours=1, minutes=55),
            )
            # Call Session without recording
            sess_no_rec = TrainingCallSession(
                session_id=501,
                call_sid="CA_CYCLE_501",
                recording_sid=None,
                recording_url=None,
                agent_id="OWNER_101",
                cycle_id=100,
                conversation_id=10,
                status="in_progress",
                started_at=now - timedelta(hours=1),
            )
            db.add_all([sess_with_rec, sess_no_rec])
            await db.flush()

            # Evaluation for session 500
            eval_500 = TrainingCallEvaluation(
                evaluation_id=90,
                session_id=500,
                cycle_id=100,
                conversation_id=10,
                agent_id="OWNER_101",
                prompt_version_id=1,
                transcription="Agente: Hola.\nCliente: Buenos días.",
                result_json={"score": 8.5, "result_json": {"escucha_activa": True}},
                score=Decimal("8.50"),
                feedback="Buen desempeño.",
            )
            db.add(eval_500)
            await db.flush()

            # Completion status for session 500
            comp_500 = TrainingCompletionStatus(
                completion_id=10,
                training_report_id=100,
                simulation_prompt_id=10,
                hubspot_owner_id="OWNER_101",
                status="completed",
                completed_at=now - timedelta(hours=1, minutes=50),
                call_session_id=500,
                evaluation_id=90,
            )
            db.add(comp_500)
            await db.commit()

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        if os.path.exists("test_cycle_audio.db"):
            try:
                os.remove("test_cycle_audio.db")
            except Exception:
                pass

    @patch("app.services.twilio_service.TwilioService.download_audio", new_callable=AsyncMock)
    async def test_superadmin_streams_cycle_audio_successfully(self, mock_dl):
        mock_dl.return_value = b"mp3_fake_audio_stream"
        async with AsyncSessionLocal() as db:
            user = await db.get(User, 1) # Superadmin

        app.dependency_overrides[get_current_user] = lambda: user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/bm/training/sessions/500/recording-audio")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content, b"mp3_fake_audio_stream")
            self.assertEqual(res.headers["content-type"], "audio/mpeg")
            self.assertEqual(res.headers["cache-control"], "private, no-store")
            self.assertIn("inline; filename=cycle_session_500.mp3", res.headers.get("content-disposition", ""))

    @patch("app.services.twilio_service.TwilioService.download_audio", new_callable=AsyncMock)
    async def test_stream_via_evaluation_and_simulation_id_routes(self, mock_dl):
        mock_dl.return_value = b"mp3_audio_via_routes"
        async with AsyncSessionLocal() as db:
            user = await db.get(User, 1) # Superadmin

        app.dependency_overrides[get_current_user] = lambda: user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Via evaluations/{evaluation_id}/recording-audio
            res_ev = await ac.get("/bm/training/evaluations/90/recording-audio")
            self.assertEqual(res_ev.status_code, 200)
            self.assertEqual(res_ev.content, b"mp3_audio_via_routes")

            # 2. Via admin/evaluations/{evaluation_id}/recording-audio
            res_admin_ev = await ac.get("/bm/training/admin/evaluations/90/recording-audio")
            self.assertEqual(res_admin_ev.status_code, 200)
            self.assertEqual(res_admin_ev.content, b"mp3_audio_via_routes")

            # 3. Via simulations/{completion_id}/recording-audio
            res_sim = await ac.get("/bm/training/simulations/10/recording-audio")
            self.assertEqual(res_sim.status_code, 200)
            self.assertEqual(res_sim.content, b"mp3_audio_via_routes")

    @patch("app.services.twilio_service.TwilioService.download_audio", new_callable=AsyncMock)
    async def test_agent_can_stream_own_cycle_simulation_audio(self, mock_dl):
        mock_dl.return_value = b"agent_own_audio_data"
        async with AsyncSessionLocal() as db:
            user_agent1 = await db.get(User, 5) # Agent 101

        app.dependency_overrides[get_current_user] = lambda: user_agent1

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/bm/training/sessions/500/recording-audio")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content, b"agent_own_audio_data")

    async def test_agent_blocked_from_streaming_another_agent_audio(self):
        async with AsyncSessionLocal() as db:
            user_agent2 = await db.get(User, 6) # Agent 102

        app.dependency_overrides[get_current_user] = lambda: user_agent2

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Attempt to stream Agent 101's session 500
            res = await ac.get("/bm/training/sessions/500/recording-audio")
            self.assertEqual(res.status_code, 403)
            self.assertIn("No tienes permisos para escuchar la grabaci\u00f3n de este agente", res.json()["detail"])

    @patch("app.services.twilio_service.TwilioService.download_audio", new_callable=AsyncMock)
    async def test_coordinator_allowed_within_team_and_blocked_outside(self, mock_dl):
        mock_dl.return_value = b"coord_team_audio"
        async with AsyncSessionLocal() as db:
            user_coord = await db.get(User, 4) # Coordinator of Team Alpha (has Agent 101)

        app.dependency_overrides[get_current_user] = lambda: user_coord

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Agent 101 is in Team Alpha -> Allowed
            res = await ac.get("/bm/training/sessions/500/recording-audio")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content, b"coord_team_audio")

    async def test_company_admin_blocked_from_other_company_cycle_audio(self):
        async with AsyncSessionLocal() as db:
            user_admin2 = await db.get(User, 3) # Admin of Company 2

        app.dependency_overrides[get_current_user] = lambda: user_admin2

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Attempt to stream Company 1 session 500
            res = await ac.get("/bm/training/sessions/500/recording-audio")
            self.assertEqual(res.status_code, 403)
            self.assertIn("pertenece a otra empresa", res.json()["detail"])

    async def test_missing_recording_url_returns_404(self):
        async with AsyncSessionLocal() as db:
            user_super = await db.get(User, 1)

        app.dependency_overrides[get_current_user] = lambda: user_super

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Session 501 exists but has recording_url=None
            res = await ac.get("/bm/training/sessions/501/recording-audio")
            self.assertEqual(res.status_code, 404)
            self.assertIn("Grabaci\u00f3n no disponible", res.json()["detail"])

    @patch("app.services.twilio_service.TwilioService.download_audio", new_callable=AsyncMock)
    async def test_provider_failure_returns_controlled_error(self, mock_dl):
        req = Request("GET", "https://api.twilio.com/fake/rec.mp3")
        mock_dl.side_effect = HTTPStatusError("Twilio 401 Unauthorized", request=req, response=Response(401, request=req))

        async with AsyncSessionLocal() as db:
            user_super = await db.get(User, 1)

        app.dependency_overrides[get_current_user] = lambda: user_super

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.get("/bm/training/sessions/500/recording-audio")
            self.assertEqual(res.status_code, 502)
            self.assertIn("No se pudo recuperar la grabaci\u00f3n", res.json()["detail"])

    async def test_cycle_report_and_evaluation_detail_exposes_recording_url(self):
        async with AsyncSessionLocal() as db:
            user_agent1 = await db.get(User, 5)

        app.dependency_overrides[get_current_user] = lambda: user_agent1

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Check get_evaluation_detail
            res_eval = await ac.get("/bm/training/admin/evaluations/90")
            self.assertEqual(res_eval.status_code, 200)
            data_eval = res_eval.json()
            self.assertEqual(data_eval["recording_url"], "/bm/training/sessions/500/recording-audio")

            # 2. Check get_evaluation_detail via alias /evaluations/90
            res_alias = await ac.get("/bm/training/evaluations/90")
            self.assertEqual(res_alias.status_code, 200)
            data_alias = res_alias.json()
            self.assertEqual(data_alias["recording_url"], "/bm/training/sessions/500/recording-audio")

            # 3. Check get_my_current_training exposes recording_url in simulations list
            res_me = await ac.get("/bm/training/me/current")
            self.assertEqual(res_me.status_code, 200)
            data_me = res_me.json()
            simulations = data_me.get("simulations", [])
            self.assertTrue(len(simulations) > 0)
            completed_sim = next((s for s in simulations if s["status"] == "completed"), None)
            self.assertIsNotNone(completed_sim)
            self.assertEqual(completed_sim["recording_url"], "/bm/training/sessions/500/recording-audio")


if __name__ == "__main__":
    unittest.main()
