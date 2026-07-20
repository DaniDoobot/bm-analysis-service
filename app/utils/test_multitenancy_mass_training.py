import os
import sys
import unittest
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///multitenancy_mass_test.db"

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
from app.models.prompts import Prompt, PromptVersion
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassAnalysisAutomation
from app.dependencies import get_current_user, get_db
from fastapi import Depends
from app.utils.security import create_access_token
from app.main import app

from sqlalchemy.ext.asyncio import AsyncSession


class TestMultitenancyMassTraining(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        # Clean old DB file if exists
        if os.path.exists("multitenancy_mass_test.db"):
            try:
                os.remove("multitenancy_mass_test.db")
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
            db.add_all([self.t1, self.t2])
            await db.flush()

            # 4. Users
            self.u_super = User(user_id=1, username="super_admin", email="super@test.com", role="admin", password_hash="dummy")
            self.u_comp1 = User(user_id=2, username="boston_admin", email="boston_admin@test.com", role="company_admin", company_id=self.c1.company_id, password_hash="dummy")
            self.u_mgr1 = User(user_id=3, username="boston_mgr", email="boston_mgr@test.com", role="responsable_servicio", company_id=self.c1.company_id, password_hash="dummy")
            self.u_coor1 = User(user_id=4, username="boston_coor", email="boston_coor@test.com", role="coordinador_equipo", company_id=self.c1.company_id, password_hash="dummy")
            self.u_agent_boston = User(user_id=5, username="agent_boston", email="agent_boston@test.com", role="agente", company_id=self.c1.company_id, hubspot_owner_id="boston_owner", password_hash="dummy")
            self.u_agent_gesdent = User(user_id=6, username="agent_gesdent", email="agent_gesdent@test.com", role="agente", company_id=self.c2.company_id, hubspot_owner_id="gesdent_owner", password_hash="dummy")

            db.add_all([self.u_super, self.u_comp1, self.u_mgr1, self.u_coor1, self.u_agent_boston, self.u_agent_gesdent])
            await db.flush()

            # 5. Associations
            db.add(UserServiceAssociation(user_id=self.u_mgr1.user_id, service_id=self.s1.service_id))
            db.add(UserTeamAssociation(user_id=self.u_coor1.user_id, team_id=self.t1.team_id))
            db.add(AgentTeamAssociation(user_id=self.u_agent_boston.user_id, team_id=self.t1.team_id))
            db.add(AgentTeamAssociation(user_id=self.u_agent_gesdent.user_id, team_id=self.t2.team_id))
            await db.flush()

            # 6. Prompts (Structures)
            self.p1 = Prompt(
                prompt_id=1,
                prompt_name="Prompt Boston", 
                prompt_type="audio",
                company_id=self.c1.company_id, 
                service_id=self.s1.service_id, 
                created_by="boston_admin@test.com"
            )
            self.p2 = Prompt(
                prompt_id=2,
                prompt_name="Prompt GesDent", 
                prompt_type="audio",
                company_id=self.c2.company_id, 
                service_id=self.s2.service_id, 
                created_by="gesdent_admin@test.com"
            )
            db.add_all([self.p1, self.p2])
            await db.flush()

            self.pv1 = PromptVersion(id=1, prompt_id=self.p1.prompt_id, version_name="v1", is_current=True, prompt="System Prompt content")
            self.pv2 = PromptVersion(id=2, prompt_id=self.p2.prompt_id, version_name="v1", is_current=True, prompt="System Prompt content")
            db.add_all([self.pv1, self.pv2])
            await db.flush()

            self.p1.current_version_id = self.pv1.id
            self.p2.current_version_id = self.pv2.id
            await db.flush()

            # 7. Mass Jobs
            self.j1 = MassEvaluationJob(
                job_id=1,
                job_name="Job Boston A",
                prompt_id=self.p1.prompt_id,
                prompt_version_id=self.pv1.id,
                company_id=self.c1.company_id,
                service_id=self.s1.service_id,
                is_active=True,
                execution_source="on_demand",
                date_mode="relative",
                timezone="Europe/Madrid",
                direction="inbound",
                only_with_recording=True,
                max_calls=10
            )
            self.j2 = MassEvaluationJob(
                job_id=2,
                job_name="Job GesDent A",
                prompt_id=self.p2.prompt_id,
                prompt_version_id=self.pv2.id,
                company_id=self.c2.company_id,
                service_id=self.s2.service_id,
                is_active=True,
                execution_source="on_demand",
                date_mode="relative",
                timezone="Europe/Madrid",
                direction="inbound",
                only_with_recording=True,
                max_calls=10
            )
            db.add_all([self.j1, self.j2])
            await db.flush()

            # 8. Mass Runs
            self.r1 = MassEvaluationRun(
                run_id=1,
                job_id=self.j1.job_id,
                company_id=self.c1.company_id,
                service_id=self.s1.service_id,
                status="running",
                trigger_type="manual",
                effective_filters={"date_from": None, "date_to": None}
            )
            self.r2 = MassEvaluationRun(
                run_id=2,
                job_id=self.j2.job_id,
                company_id=self.c2.company_id,
                service_id=self.s2.service_id,
                status="completed",
                trigger_type="manual",
                effective_filters={"date_from": None, "date_to": None}
            )
            db.add_all([self.r1, self.r2])
            await db.flush()

            # 9. Mass Results
            self.res1 = MassEvaluationResult(
                mass_analysis_id=1,
                run_id=self.r1.run_id,
                job_id=self.j1.job_id,
                call_id="call_boston_1",
                hs_object_id="hs_1",
                recording_url="http://fake.url/1",
                hubspot_owner_id="boston_owner",
                agent_name="Boston Agent",
                call_timestamp=datetime.now(timezone.utc),
                analysis_timestamp=datetime.now(timezone.utc),
                prompt_id=self.p1.prompt_id,
                prompt_version_id=self.pv1.id,
                prompt_snapshot="Snapshot",
                company_id=self.c1.company_id,
                service_id=self.s1.service_id,
                status="completed",
                result_json={},
                items_json=[]
            )
            self.res2 = MassEvaluationResult(
                mass_analysis_id=2,
                run_id=self.r2.run_id,
                job_id=self.j2.job_id,
                call_id="call_gesdent_1",
                hs_object_id="hs_2",
                recording_url="http://fake.url/2",
                hubspot_owner_id="gesdent_owner",
                agent_name="GesDent Agent",
                call_timestamp=datetime.now(timezone.utc),
                analysis_timestamp=datetime.now(timezone.utc),
                prompt_id=self.p2.prompt_id,
                prompt_version_id=self.pv2.id,
                prompt_snapshot="Snapshot",
                company_id=self.c2.company_id,
                service_id=self.s2.service_id,
                status="completed",
                result_json={},
                items_json=[]
            )
            db.add_all([self.res1, self.res2])
            await db.flush()

            # 10. Automations
            self.auto1 = MassAnalysisAutomation(
                automation_id=1,
                name="Auto Boston",
                description="Auto Boston Desc",
                is_active=True,
                interval_minutes=60,
                lookback_minutes=60,
                delay_minutes=5,
                service_id=self.s1.service_id,
                prompt_id=self.p1.prompt_id,
                prompt_version_id=self.pv1.id,
                job_id=self.j1.job_id
            )
            self.auto2 = MassAnalysisAutomation(
                automation_id=2,
                name="Auto GesDent",
                description="Auto GesDent Desc",
                is_active=True,
                interval_minutes=60,
                lookback_minutes=60,
                delay_minutes=5,
                service_id=self.s2.service_id,
                prompt_id=self.p2.prompt_id,
                prompt_version_id=self.pv2.id,
                job_id=self.j2.job_id
            )
            db.add_all([self.auto1, self.auto2])
            await db.commit()

        # Build tokens with valid database fields (user_id, email)
        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_boston_admin = create_access_token({"user_id": 2, "email": "boston_admin@test.com"})
        self.t_boston_mgr = create_access_token({"user_id": 3, "email": "boston_mgr@test.com"})
        self.t_boston_coor = create_access_token({"user_id": 4, "email": "boston_coor@test.com"})
        self.t_boston_agent = create_access_token({"user_id": 5, "email": "agent_boston@test.com"})
        self.t_gesdent_agent = create_access_token({"user_id": 6, "email": "agent_gesdent@test.com"})

        # Save ID constants for test calls
        self.j1_id = 1
        self.j2_id = 2
        self.r1_id = 1
        self.r2_id = 2
        self.res1_id = 1
        self.res2_id = 2
        self.auto1_id = 1
        self.auto2_id = 2
        self.p1_id = 1
        self.p2_id = 2
        self.s1_id = 1
        self.s2_id = 2

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        # Close engine resources
        engine = get_engine()
        await engine.dispose()
        if os.path.exists("multitenancy_mass_test.db"):
            try:
                os.remove("multitenancy_mass_test.db")
            except Exception:
                pass

    # ── Test Jobs Scoping ─────────────────────────────────────────────────────

    async def test_list_jobs_scoping(self):
        # Super admin sees both
        res = await self.client.get("/bm/mass-evaluation-jobs", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)
        jobs = res.json()
        self.assertEqual(len(jobs), 2)

        # Boston Admin sees only Boston
        res = await self.client.get("/bm/mass-evaluation-jobs", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        jobs = res.json()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_name"], "Job Boston A")

        # Boston Agent is forbidden (403)
        res = await self.client.get("/bm/mass-evaluation-jobs", headers={"Authorization": f"Bearer {self.t_boston_agent}"})
        self.assertEqual(res.status_code, 403)

    async def test_get_job_detail_scoping(self):
        # Boston Admin gets Boston Job
        res = await self.client.get(f"/bm/mass-evaluation-jobs/{self.j1_id}", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["job_name"], "Job Boston A")

        # Boston Admin gets GesDent Job -> 403
        res = await self.client.get(f"/bm/mass-evaluation-jobs/{self.j2_id}", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 403)

    async def test_create_job_scoping(self):
        payload = {
            "job_name": "New Job Boston",
            "prompt_id": self.p1_id,
            "is_active": True,
            "date_mode": "relative",
            "timezone": "Europe/Madrid",
            "direction": "inbound",
            "only_with_recording": True,
            "max_calls": 10
        }

        # Boston Admin creates Job with Boston Prompt -> 201
        res = await self.client.post("/bm/mass-evaluation-jobs", json=payload, headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 201)

        # Boston Admin creates Job with GesDent Prompt -> 403
        payload["prompt_id"] = self.p2_id
        res = await self.client.post("/bm/mass-evaluation-jobs", json=payload, headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 403)

    # ── Test Runs Scoping ─────────────────────────────────────────────────────

    async def test_list_runs_scoping(self):
        # Super admin sees both
        res = await self.client.get("/bm/mass-evaluation-runs", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 2)

        # Boston Admin sees only Boston Run
        res = await self.client.get("/bm/mass-evaluation-runs", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        runs = res.json()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], self.r1_id)

    async def test_cancel_run_scoping(self):
        # Boston Admin cancels Boston Run
        res = await self.client.post(f"/bm/mass-evaluation-runs/{self.r1_id}/cancel", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)

        # Boston Admin cancels GesDent Run -> 403
        res = await self.client.post(f"/bm/mass-evaluation-runs/{self.r2_id}/cancel", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 403)

    # ── Test Results Scoping ──────────────────────────────────────────────────

    async def test_list_results_scoping(self):
        # Boston Admin sees Boston Result
        res = await self.client.get("/bm/mass-evaluation-results", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)
        self.assertEqual(res.json()[0]["call_id"], "call_boston_1")

        # Boston Agent sees Boston Result (has same owner_id)
        res = await self.client.get("/bm/mass-evaluation-results", headers={"Authorization": f"Bearer {self.t_boston_agent}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)

        # GesDent Agent sees GesDent Result
        res = await self.client.get("/bm/mass-evaluation-results", headers={"Authorization": f"Bearer {self.t_gesdent_agent}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)
        self.assertEqual(res.json()[0]["call_id"], "call_gesdent_1")

        # Boston Agent queries GesDent Result -> 403 / filtered
        res = await self.client.get("/bm/mass-evaluation-results", params={"agent_owner_id": "gesdent_owner"}, headers={"Authorization": f"Bearer {self.t_boston_agent}"})
        self.assertEqual(res.status_code, 403)

    async def test_get_result_scoping(self):
        # Boston Agent views Boston Result -> 200
        res = await self.client.get(f"/bm/mass-evaluation-results/{self.res1_id}", headers={"Authorization": f"Bearer {self.t_boston_agent}"})
        self.assertEqual(res.status_code, 200)

        # Boston Agent views GesDent Result -> 403
        res = await self.client.get(f"/bm/mass-evaluation-results/{self.res2_id}", headers={"Authorization": f"Bearer {self.t_boston_agent}"})
        self.assertEqual(res.status_code, 403)

    # ── Test Automations Scoping ──────────────────────────────────────────────

    async def test_list_automations_scoping(self):
        # Boston Admin lists automations -> sees Boston
        res = await self.client.get("/bm/mass-analysis/automations", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        autos = res.json()
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0]["name"], "Auto Boston")

        # Boston Agent is forbidden
        res = await self.client.get("/bm/mass-analysis/automations", headers={"Authorization": f"Bearer {self.t_boston_agent}"})
        self.assertEqual(res.status_code, 403)

    async def test_create_automation_scoping(self):
        payload = {
            "name": "New Auto",
            "description": "Desc",
            "is_active": True,
            "interval_minutes": 30,
            "lookback_minutes": 30,
            "delay_minutes": 5,
            "service_id": self.s1_id,
            "prompt_id": self.p1_id
        }

        # Boston Admin creates Boston Automation -> 201
        res = await self.client.post("/bm/mass-analysis/automations", json=payload, headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 201)

        # Boston Admin creates GesDent Automation -> 403
        payload["service_id"] = self.s2_id
        res = await self.client.post("/bm/mass-analysis/automations", json=payload, headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    import asyncio
    asyncio.run(unittest.main())
