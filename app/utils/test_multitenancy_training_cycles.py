import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///multitenancy_training_test.db"

# Safety Confirmation Check
db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution was blocked because DATABASE_URL points to production!")

# Setup path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# SQLite Type Compilers for Compatibility
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.services import Service
from app.models.personalized_training import (
    TrainingAgentSetting,
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingSchedulerSetting,
    TrainingCallSession,
    TrainingEvaluationPrompt,
    TrainingCallEvaluation,
)
from app.utils.security import create_access_token
from app.main import app

from sqlalchemy.ext.asyncio import AsyncSession


class TestMultitenancyTrainingCycles(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        # Clean old DB file if exists
        if os.path.exists("multitenancy_training_test.db"):
            try:
                os.remove("multitenancy_training_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Populate test data
        self.session_factory = get_engine()
        async with AsyncSession(self.session_factory) as db:
            # 1. Companies
            self.c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c2 = Company(company_id=2, company_name="GesDent", company_key="gesdent", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()

            # 2. Services
            self.s1 = Service(service_id=1, service_name="Front Desk Boston", service_key="front-boston", company_id=self.c1.company_id)
            self.s2 = Service(service_id=2, service_name="Experiencia GesDent", service_key="experiencia-gesdent", company_id=self.c2.company_id)
            db.add_all([self.s1, self.s2])
            await db.flush()

            # 3. Teams
            self.t1 = Team(team_id=1, team_name="Boston A", company_id=self.c1.company_id, service_id=self.s1.service_id)
            self.t2 = Team(team_id=2, team_name="GesDent B", company_id=self.c2.company_id, service_id=self.s2.service_id)
            self.t3 = Team(team_id=3, team_name="Boston B", company_id=self.c1.company_id, service_id=self.s1.service_id)
            db.add_all([self.t1, self.t2, self.t3])
            await db.flush()

            # 4. Users
            self.u_super = User(user_id=1, username="super_admin", email="super@test.com", role="admin", password_hash="dummy")
            self.u_comp1 = User(user_id=2, username="boston_admin", email="boston_admin@test.com", role="company_admin", company_id=self.c1.company_id, password_hash="dummy")
            self.u_mgr1 = User(user_id=3, username="boston_mgr", email="boston_mgr@test.com", role="responsable_servicio", company_id=self.c1.company_id, password_hash="dummy")
            self.u_coor1 = User(user_id=4, username="boston_coor", email="boston_coor@test.com", role="coordinador_equipo", company_id=self.c1.company_id, password_hash="dummy")
            
            # Boston agents
            self.u_agent_boston1 = User(user_id=5, username="agent_b1", email="agent_b1@test.com", role="agente", company_id=self.c1.company_id, hubspot_owner_id="boston_owner_1", password_hash="dummy")
            self.u_agent_boston2 = User(user_id=6, username="agent_b2", email="agent_b2@test.com", role="agente", company_id=self.c1.company_id, hubspot_owner_id="boston_owner_2", password_hash="dummy")
            
            # GesDent agent
            self.u_agent_gesdent = User(user_id=7, username="agent_gesdent", email="agent_gesdent@test.com", role="agente", company_id=self.c2.company_id, hubspot_owner_id="gesdent_owner", password_hash="dummy")

            db.add_all([
                self.u_super, self.u_comp1, self.u_mgr1, self.u_coor1,
                self.u_agent_boston1, self.u_agent_boston2, self.u_agent_gesdent
            ])
            await db.flush()

            # 5. Associations
            # Mgr 1: Assigned only to service 1
            db.add(UserServiceAssociation(user_id=self.u_mgr1.user_id, service_id=self.s1.service_id))
            # Coor 1: Assigned to Team 1 (which belongs to service 1)
            db.add(UserTeamAssociation(user_id=self.u_coor1.user_id, team_id=self.t1.team_id))
            # Agent Boston 1 in Team 1
            db.add(AgentTeamAssociation(user_id=self.u_agent_boston1.user_id, team_id=self.t1.team_id))
            # Agent Boston 2 in Team 3 (belongs to Service 1 but not Team 1)
            db.add(AgentTeamAssociation(user_id=self.u_agent_boston2.user_id, team_id=self.t3.team_id))
            # Agent GesDent in Team 2
            db.add(AgentTeamAssociation(user_id=self.u_agent_gesdent.user_id, team_id=self.t2.team_id))
            await db.flush()

            # 6. Training Agent Settings
            self.set_b1 = TrainingAgentSetting(
                setting_id=1, company_id=self.c1.company_id, hubspot_owner_id="boston_owner_1",
                agent_name="Boston Agent 1", agent_initials="BA1", is_enabled=True
            )
            self.set_b2 = TrainingAgentSetting(
                setting_id=2, company_id=self.c1.company_id, hubspot_owner_id="boston_owner_2",
                agent_name="Boston Agent 2", agent_initials="BA2", is_enabled=True
            )
            self.set_g1 = TrainingAgentSetting(
                setting_id=3, company_id=self.c2.company_id, hubspot_owner_id="gesdent_owner",
                agent_name="GesDent Agent", agent_initials="GDA", is_enabled=True
            )
            db.add_all([self.set_b1, self.set_b2, self.set_g1])
            await db.flush()

            # 7. Training Runs
            self.run1 = TrainingRun(
                training_run_id=1, company_id=self.c1.company_id, service_id=self.s1.service_id,
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="completed", triggered_by="manual"
            )
            self.run2 = TrainingRun(
                training_run_id=2, company_id=self.c2.company_id, service_id=self.s2.service_id,
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="completed", triggered_by="manual"
            )
            db.add_all([self.run1, self.run2])
            await db.flush()

            # 8. Training Agent Reports (Cycles)
            # Boston 1 current
            self.rep_b1_curr = TrainingAgentReport(
                training_report_id=1, training_run_id=self.run1.training_run_id,
                company_id=self.c1.company_id, service_id=self.s1.service_id,
                hubspot_owner_id="boston_owner_1", agent_name="Boston Agent 1", agent_initials="BA1",
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="in_progress", is_current=True, evaluations_count=0, avg_evaluacion_global=Decimal("8.50")
            )
            # Boston 2 current (pending approval)
            self.rep_b2_curr = TrainingAgentReport(
                training_report_id=2, training_run_id=self.run1.training_run_id,
                company_id=self.c1.company_id, service_id=self.s1.service_id,
                hubspot_owner_id="boston_owner_2", agent_name="Boston Agent 2", agent_initials="BA2",
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="pending_approval", is_current=True, evaluations_count=0, avg_evaluacion_global=Decimal("7.00")
            )
            # GesDent current
            self.rep_g1_curr = TrainingAgentReport(
                training_report_id=3, training_run_id=self.run2.training_run_id,
                company_id=self.c2.company_id, service_id=self.s2.service_id,
                hubspot_owner_id="gesdent_owner", agent_name="GesDent Agent", agent_initials="GDA",
                period_start=datetime.now(timezone.utc) - timedelta(days=14),
                period_end=datetime.now(timezone.utc),
                status="in_progress", is_current=True, evaluations_count=0, avg_evaluacion_global=Decimal("9.00")
            )
            # Boston 1 historical report
            self.rep_b1_hist = TrainingAgentReport(
                training_report_id=4, training_run_id=self.run1.training_run_id,
                company_id=self.c1.company_id, service_id=self.s1.service_id,
                hubspot_owner_id="boston_owner_1", agent_name="Boston Agent 1", agent_initials="BA1",
                period_start=datetime.now(timezone.utc) - timedelta(days=28),
                period_end=datetime.now(timezone.utc) - timedelta(days=14),
                status="completed", is_current=False, evaluations_count=4, avg_evaluacion_global=Decimal("8.00")
            )
            db.add_all([self.rep_b1_curr, self.rep_b2_curr, self.rep_g1_curr, self.rep_b1_hist])
            await db.flush()

            # 9. Simulation Prompts
            self.sim_p1 = TrainingSimulationPrompt(
                simulation_prompt_id=1, training_report_id=self.rep_b1_curr.training_report_id,
                hubspot_owner_id="boston_owner_1", prompt_number=1, title="Saludo inicial",
                scenario_type="audio", prompt_text="Instrucciones secretas de simulación..."
            )
            self.sim_p2 = TrainingSimulationPrompt(
                simulation_prompt_id=2, training_report_id=self.rep_g1_curr.training_report_id,
                hubspot_owner_id="gesdent_owner", prompt_number=1, title="Saludo inicial",
                scenario_type="audio", prompt_text="GesDent instrucciones secretas..."
            )
            db.add_all([self.sim_p1, self.sim_p2])
            await db.flush()

            # 10. Scheduler Setting
            self.sched_set = TrainingSchedulerSetting(
                setting_id=1, is_enabled=True, interval_days=14, lookback_days=14
            )
            db.add(self.sched_set)
            await db.flush()

            # 11. Call Sessions
            self.sess_b1 = TrainingCallSession(
                session_id=1, call_sid="CSID_B1", agent_id="boston_owner_1",
                cycle_id=self.rep_b1_curr.training_report_id, conversation_id=self.sim_p1.simulation_prompt_id,
                status="completed"
            )
            self.sess_g1 = TrainingCallSession(
                session_id=2, call_sid="CSID_G1", agent_id="gesdent_owner",
                cycle_id=self.rep_g1_curr.training_report_id, conversation_id=self.sim_p2.simulation_prompt_id,
                status="completed"
            )
            db.add_all([self.sess_b1, self.sess_g1])
            await db.flush()

            # 12. Evaluation Prompts
            self.eval_prompt_1 = TrainingEvaluationPrompt(
                id=1, service_id=self.s1.service_id, company_id=self.c1.company_id,
                prompt_text="Criterios evaluación Boston", is_active=True
            )
            self.eval_prompt_2 = TrainingEvaluationPrompt(
                id=2, service_id=self.s2.service_id, company_id=self.c2.company_id,
                prompt_text="Criterios evaluación GesDent", is_active=True
            )
            db.add_all([self.eval_prompt_1, self.eval_prompt_2])
            await db.flush()

            # 13. Call Evaluations
            self.eval_b1 = TrainingCallEvaluation(
                evaluation_id=1, session_id=self.sess_b1.session_id, cycle_id=self.rep_b1_curr.training_report_id,
                conversation_id=self.sim_p1.simulation_prompt_id, agent_id="boston_owner_1",
                prompt_version_id=self.eval_prompt_1.id, result_json={"empatia": True}, score=Decimal("9.00"),
                feedback="Buen tono"
            )
            self.eval_g1 = TrainingCallEvaluation(
                evaluation_id=2, session_id=self.sess_g1.session_id, cycle_id=self.rep_g1_curr.training_report_id,
                conversation_id=self.sim_p2.simulation_prompt_id, agent_id="gesdent_owner",
                prompt_version_id=self.eval_prompt_2.id, result_json={"empatia": False}, score=Decimal("5.00"),
                feedback="Debe mejorar empatia"
            )
            db.add_all([self.eval_b1, self.eval_g1])
            await db.flush()

            # 14. Completion Status
            self.comp_b1 = TrainingCompletionStatus(
                completion_id=1, training_report_id=self.rep_b1_curr.training_report_id,
                simulation_prompt_id=self.sim_p1.simulation_prompt_id, hubspot_owner_id="boston_owner_1",
                status="completed", call_session_id=self.sess_b1.session_id, evaluation_id=self.eval_b1.evaluation_id
            )
            self.comp_g1 = TrainingCompletionStatus(
                completion_id=2, training_report_id=self.rep_g1_curr.training_report_id,
                simulation_prompt_id=self.sim_p2.simulation_prompt_id, hubspot_owner_id="gesdent_owner",
                status="completed", call_session_id=self.sess_g1.session_id, evaluation_id=self.eval_g1.evaluation_id
            )
            db.add_all([self.comp_b1, self.comp_g1])
            await db.commit()

        # Build tokens with valid database fields (user_id, email)
        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_boston_admin = create_access_token({"user_id": 2, "email": "boston_admin@test.com"})
        self.t_boston_mgr = create_access_token({"user_id": 3, "email": "boston_mgr@test.com"})
        self.t_boston_coor = create_access_token({"user_id": 4, "email": "boston_coor@test.com"})
        self.t_boston_agent1 = create_access_token({"user_id": 5, "email": "agent_b1@test.com"})
        self.t_boston_agent2 = create_access_token({"user_id": 6, "email": "agent_b2@test.com"})
        self.t_gesdent_agent = create_access_token({"user_id": 7, "email": "agent_gesdent@test.com"})

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        # Close engine resources
        engine = get_engine()
        await engine.dispose()
        if os.path.exists("multitenancy_training_test.db"):
            try:
                os.remove("multitenancy_training_test.db")
            except Exception:
                pass

    # ── Test settings /admin/settings Scoping ─────────────────────────────────

    async def test_admin_settings_scoping(self):
        # 1. Super admin sees all 3 settings
        res = await self.client.get("/bm/training/admin/settings", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 3)

        # 2. Boston admin sees only 2 Boston settings
        res = await self.client.get("/bm/training/admin/settings", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        settings = res.json()
        self.assertEqual(len(settings), 2)
        self.assertTrue(all(s["company_id"] == 1 for s in settings))

        # 3. Boston service manager sees agents in service 1 (both Boston 1 & Boston 2 have service_id=1 through report/run structure)
        res = await self.client.get("/bm/training/admin/settings", headers={"Authorization": f"Bearer {self.t_boston_mgr}"})
        self.assertEqual(res.status_code, 200)
        settings_mgr = res.json()
        self.assertEqual(len(settings_mgr), 2)

        # 4. Boston team coordinator sees only agents in t1 (boston_owner_1)
        res = await self.client.get("/bm/training/admin/settings", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 200)
        settings_coor = res.json()
        self.assertEqual(len(settings_coor), 1)
        self.assertEqual(settings_coor[0]["hubspot_owner_id"], "boston_owner_1")

        # 5. Agent gets 403
        res = await self.client.get("/bm/training/admin/settings", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 403)

    # ── Test agents-overview Scoping ──────────────────────────────────────────

    async def test_agents_overview_scoping(self):
        # Super admin overview lists all 3
        res = await self.client.get("/bm/training/admin/agents-overview", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 3)

        # Boston admin overview lists 2
        res = await self.client.get("/bm/training/admin/agents-overview", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        overview = res.json()
        self.assertEqual(len(overview), 2)
        self.assertTrue(all(item["hubspot_owner_id"] in ["boston_owner_1", "boston_owner_2"] for item in overview))

        # Boston team coordinator sees 1 (boston_owner_1)
        res = await self.client.get("/bm/training/admin/agents-overview", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 200)
        overview_coor = res.json()
        self.assertEqual(len(overview_coor), 1)
        self.assertEqual(overview_coor[0]["hubspot_owner_id"], "boston_owner_1")

    # ── Test cycles-summary Scoping ───────────────────────────────────────────

    async def test_cycles_summary_scoping(self):
        # Boston admin sees Boston metrics
        res = await self.client.get("/bm/training/admin/cycles-summary", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        summary = res.json()
        self.assertEqual(summary["monitored_agents"], 2)

        # Boston coordinator sees only 1 monitored agent (boston_owner_1)
        res = await self.client.get("/bm/training/admin/cycles-summary", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["monitored_agents"], 1)

    # ── Test scheduler-settings Scoping ───────────────────────────────────────

    async def test_scheduler_settings_scoping(self):
        # Super admin gets scheduler settings (200 OK)
        res = await self.client.get("/bm/training/admin/scheduler-settings", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)

        # Other roles get 403
        res = await self.client.get("/bm/training/admin/scheduler-settings", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 403)

    # ── Test agents/{hubspot_owner_id} Scoping ────────────────────────────────

    async def test_agent_detail_admin_scoping(self):
        # Boston admin gets Boston 1 detail
        res = await self.client.get("/bm/training/admin/agents/boston_owner_1", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)

        # Boston admin gets GesDent detail -> 403 or 404 (due to company filtering)
        res = await self.client.get("/bm/training/admin/agents/gesdent_owner", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertIn(res.status_code, [403, 404])

        # Boston coordinator gets Boston 1 detail -> 200
        res = await self.client.get("/bm/training/admin/agents/boston_owner_1", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 200)

        # Boston coordinator gets Boston 2 detail (not in his team t1) -> 403
        res = await self.client.get("/bm/training/admin/agents/boston_owner_2", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 403)

    # ── Test /me/current and /me/history Scoping ──────────────────────────────

    async def test_me_current_training(self):
        # Boston agent 1 gets current report
        res = await self.client.get("/bm/training/me/current", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 200)
        report = res.json()
        self.assertEqual(report["hubspot_owner_id"], "boston_owner_1")
        # Sanitize prompt_text check for agents (should be empty string)
        self.assertEqual(report["prompts"][0]["prompt_text"], "")

        # Boston agent 2 gets current report but it's pending_approval, so it's not approved yet -> gets 404
        res = await self.client.get("/bm/training/me/current", headers={"Authorization": f"Bearer {self.t_boston_agent2}"})
        self.assertEqual(res.status_code, 404)

    async def test_me_history_and_report_by_id(self):
        # Boston agent 1 gets history (sees 2 reports: current and historical)
        res = await self.client.get("/bm/training/me/history", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 2)

        # Boston agent 1 gets his own historical report by ID -> 200 OK
        res = await self.client.get("/bm/training/me/reports/4", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 200)

        # Boston agent 1 tries to get GesDent report (report 3) -> 404
        res = await self.client.get("/bm/training/me/reports/3", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 404)

    # ── Test /agents/{id} current & history Scoping ───────────────────────────

    async def test_agents_id_endpoints(self):
        # Boston admin gets Boston 1 current
        res = await self.client.get("/bm/training/agents/boston_owner_1/current", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        # Admins DO see the prompt_text (unsanitized)
        self.assertEqual(res.json()["prompts"][0]["prompt_text"], "Instrucciones secretas de simulación...")

        # Boston agent 1 gets Boston 1 current -> 200 OK (with sanitized prompt_text)
        res = await self.client.get("/bm/training/agents/boston_owner_1/current", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["prompts"][0]["prompt_text"], "")

        # Boston agent 1 gets Boston 2 current -> 403 Forbidden
        res = await self.client.get("/bm/training/agents/boston_owner_2/current", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 403)

    # ── Test /reports/{training_report_id} Scoping ────────────────────────────

    async def test_report_by_id_endpoints(self):
        # Boston agent 1 gets Boston 1 report by ID -> 200 OK (sanitized)
        res = await self.client.get("/bm/training/reports/1", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["prompts"][0]["prompt_text"], "")

        # Boston agent 1 gets report of GesDent (report 3) -> 404
        res = await self.client.get("/bm/training/reports/3", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 404)

        # Boston coordinator gets Boston 1 report -> 200 OK
        res = await self.client.get("/bm/training/reports/1", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 200)

        # Boston coordinator gets Boston 2 report (out of team) -> 404
        res = await self.client.get("/bm/training/reports/2", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 404)

    # ── Test evaluations/{evaluation_id} Scoping ──────────────────────────────

    async def test_get_evaluation_detail_scoping(self):
        # Super admin gets eval 1 -> 200 OK
        res = await self.client.get("/bm/training/admin/evaluations/1", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)

        # Boston coordinator gets eval 1 (boston_owner_1 is in his team) -> 200 OK
        res = await self.client.get("/bm/training/admin/evaluations/1", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 200)

        # Boston agent 1 gets eval 1 -> 200 OK
        res = await self.client.get("/bm/training/admin/evaluations/1", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 200)

        # Boston agent 1 gets eval 2 (GesDent evaluation) -> 403 Forbidden
        res = await self.client.get("/bm/training/admin/evaluations/2", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 403)

        # Boston admin gets eval 2 (GesDent evaluation) -> 403 Forbidden
        res = await self.client.get("/bm/training/admin/evaluations/2", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 403)

    # ── Test WRITE Endpoints Scoping ──────────────────────────────────────────

    async def test_update_agent_setting_scoping(self):
        # Boston admin can modify Boston setting -> 200
        res = await self.client.patch(
            "/bm/training/admin/settings/boston_owner_1",
            json={"is_enabled": False},
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)

        # Boston admin cannot modify GesDent setting -> 403
        res = await self.client.patch(
            "/bm/training/admin/settings/gesdent_owner",
            json={"is_enabled": False},
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

        # Coordinator gets 403
        res = await self.client.patch(
            "/bm/training/admin/settings/boston_owner_1",
            json={"is_enabled": False},
            headers={"Authorization": f"Bearer {self.t_boston_coor}"}
        )
        self.assertEqual(res.status_code, 403)

        # Agent gets 403
        res = await self.client.patch(
            "/bm/training/admin/settings/boston_owner_1",
            json={"is_enabled": False},
            headers={"Authorization": f"Bearer {self.t_boston_agent1}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_update_scheduler_settings_scoping(self):
        # Super admin can update scheduler settings -> 200
        res = await self.client.patch(
            "/bm/training/admin/scheduler-settings",
            json={"is_enabled": False, "interval_days": 7, "lookback_days": 7},
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 200)

        # Boston admin cannot update scheduler settings -> 403
        res = await self.client.patch(
            "/bm/training/admin/scheduler-settings",
            json={"is_enabled": False, "interval_days": 7, "lookback_days": 7},
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_generate_scoping(self):
        # Boston admin can generate for Boston agent -> 200
        res = await self.client.post(
            "/bm/training/admin/generate",
            json={
                "hubspot_owner_ids": ["boston_owner_1"],
                "period_start": (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),
                "period_end": datetime.now(timezone.utc).isoformat(),
                "force_regenerate": True
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)

        # Boston admin cannot generate for GesDent agent -> 403
        res = await self.client.post(
            "/bm/training/admin/generate",
            json={
                "hubspot_owner_ids": ["gesdent_owner"],
                "period_start": (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),
                "period_end": datetime.now(timezone.utc).isoformat(),
                "force_regenerate": True
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

        # Coordinator gets 403
        res = await self.client.post(
            "/bm/training/admin/generate",
            json={
                "hubspot_owner_ids": ["boston_owner_1"]
            },
            headers={"Authorization": f"Bearer {self.t_boston_coor}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_archive_report_scoping(self):
        # Boston admin can archive Boston report 1 -> 200
        res = await self.client.post(
            "/bm/training/admin/reports/1/archive",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)

        # Boston admin cannot archive GesDent report 3 -> 403
        res = await self.client.post(
            "/bm/training/admin/reports/3/archive",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

        # Coordinator gets 403
        res = await self.client.post(
            "/bm/training/admin/reports/1/archive",
            headers={"Authorization": f"Bearer {self.t_boston_coor}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_update_cycle_objectives_scoping(self):
        # Boston admin can patch Boston report 2 objectives (pending approval) -> 200
        res = await self.client.patch(
            "/bm/training/admin/reports/2/objectives",
            json={
                "general_objectives_json": [{"title": "Obj gen", "description": "Desc", "rationale": "Rat", "expected_behavior": "Beh", "success_indicators": ["Ind"]}],
                "specific_objectives_json": [{"title": "Obj spec", "description": "Desc", "related_criteria": ["crit"], "specific_behavior_to_improve": "Beh", "success_indicators": ["Ind"]}]
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)

        # Boston admin cannot patch GesDent report 3 -> 403
        res = await self.client.patch(
            "/bm/training/admin/reports/3/objectives",
            json={
                "general_objectives_json": [],
                "specific_objectives_json": []
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

        # Coordinator gets 403
        res = await self.client.patch(
            "/bm/training/admin/reports/2/objectives",
            json={
                "general_objectives_json": [],
                "specific_objectives_json": []
            },
            headers={"Authorization": f"Bearer {self.t_boston_coor}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_approve_cycle_scoping(self):
        # Boston admin can approve Boston report 2 -> 200
        res = await self.client.post(
            "/bm/training/admin/reports/2/approve",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)

        # Boston admin cannot approve GesDent report 3 -> 403
        res = await self.client.post(
            "/bm/training/admin/reports/3/approve",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

        # Coordinator gets 403
        res = await self.client.post(
            "/bm/training/admin/reports/2/approve",
            headers={"Authorization": f"Bearer {self.t_boston_coor}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_hard_delete_report_scoping(self):
        # Boston admin can hard-delete Boston report 2 -> 200
        res = await self.client.delete(
            "/bm/training/admin/reports/2/hard-delete",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)

        # Boston admin cannot hard-delete GesDent report 3 -> 403
        res = await self.client.delete(
            "/bm/training/admin/reports/3/hard-delete",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

        # Coordinator gets 403
        res = await self.client.delete(
            "/bm/training/admin/reports/2/hard-delete",
            headers={"Authorization": f"Bearer {self.t_boston_coor}"}
        )
        self.assertEqual(res.status_code, 403)

    # ── Manual Cycle Creation Endpoints ─────────────────────────────────────

    async def test_manual_cycle_company_admin_creates_cycles_for_company_agents(self):
        res = await self.client.post(
            "/bm/training/admin/manual-cycle",
            json={
                "hubspot_owner_ids": ["boston_owner_1", "boston_owner_2"],
                "objectives": ["Mejorar cierre de cita", "Manejo de objeciones de precio"],
                "title": "Ciclo Manual Masivo Boston",
                "service_id": 1
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 2)
        owners = {r["hubspot_owner_id"] for r in data}
        self.assertEqual(owners, {"boston_owner_1", "boston_owner_2"})
        for r in data:
            self.assertEqual(r["status"], "in_progress")
            self.assertIn("Ciclo Manual Masivo Boston", r["summary_general"])

    async def test_manual_cycle_team_coordinator_creates_cycle_for_team_agent(self):
        res = await self.client.post(
            "/bm/training/admin/manual-cycle",
            json={
                "hubspot_owner_ids": ["boston_owner_1"],
                "objectives": ["Optimizar empatía inicial"],
                "title": "Ciclo Manual Equipo",
                "service_id": 1
            },
            headers={"Authorization": f"Bearer {self.t_boston_coor}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["hubspot_owner_id"], "boston_owner_1")
        self.assertEqual(data[0]["status"], "in_progress")

    async def test_manual_cycle_team_coordinator_out_of_scope_agent_returns_403_and_no_partial_creation(self):
        # Count reports before request
        async with AsyncSession(self.session_factory) as db:
            from sqlalchemy import select, func
            r_before = (await db.execute(select(func.count(TrainingAgentReport.training_report_id)))).scalar()

        # boston_coor tries to include boston_owner_2 (outside Team 1)
        res = await self.client.post(
            "/bm/training/admin/manual-cycle",
            json={
                "hubspot_owner_ids": ["boston_owner_1", "boston_owner_2"],
                "objectives": ["Objetivo no permitido"],
                "title": "Ciclo Parcial Fallido"
            },
            headers={"Authorization": f"Bearer {self.t_boston_coor}"}
        )
        self.assertEqual(res.status_code, 403)

        # Verify no partial creation in DB
        async with AsyncSession(self.session_factory) as db:
            r_after = (await db.execute(select(func.count(TrainingAgentReport.training_report_id)))).scalar()
            self.assertEqual(r_before, r_after)

    async def test_manual_cycle_agent_role_returns_403(self):
        res = await self.client.post(
            "/bm/training/admin/manual-cycle",
            json={
                "hubspot_owner_ids": ["boston_owner_1"],
                "objectives": ["Self objective"],
                "title": "Intento de Agente"
            },
            headers={"Authorization": f"Bearer {self.t_boston_agent1}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_manual_cycle_creates_report_and_four_prompts_per_agent(self):
        res = await self.client.post(
            "/bm/training/admin/manual-cycle",
            json={
                "hubspot_owner_ids": ["gesdent_owner"],
                "objectives": ["Pauta de objeción clínicas", "Estructura de saludo"],
                "title": "Ciclo Manual GesDent",
                "service_id": 2
            },
            headers={"Authorization": f"Bearer {self.t_super}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        report_id = data[0]["training_report_id"]

        # Verify DB records
        async with AsyncSession(self.session_factory) as db:
            from sqlalchemy import select
            rep = await db.get(TrainingAgentReport, report_id)
            self.assertIsNotNone(rep)
            self.assertEqual(rep.status, "in_progress")
            self.assertEqual(rep.cycle_mode, "manual")

            # Verify exactly 4 simulation prompts were generated
            stmt_p = select(TrainingSimulationPrompt).where(TrainingSimulationPrompt.training_report_id == report_id)
            prompts = list((await db.execute(stmt_p)).scalars().all())
            self.assertEqual(len(prompts), 4)


if __name__ == "__main__":
    import asyncio
    asyncio.run(unittest.main())
