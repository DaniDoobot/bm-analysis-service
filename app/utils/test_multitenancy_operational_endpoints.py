import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///multitenancy_ops_test.db"

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
from app.models.typologies import Typology
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.models.analyses import Analysis, CallAnalysisCurrent
from app.dependencies import get_current_user, get_db
from fastapi import Depends
from app.utils.security import create_access_token
from app.main import app

from sqlalchemy.ext.asyncio import AsyncSession


class TestMultitenancyOperationalEndpoints(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        # Clean old DB file if exists
        if os.path.exists("multitenancy_ops_test.db"):
            try:
                os.remove("multitenancy_ops_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Populate test data
        self.session_factory = get_engine()
        async with AsyncSession(self.session_factory) as db:
            # 1. Companies
            self.c1 = Company(company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c2 = Company(company_name="GesDent", company_key="gesdent", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()

            # 2. Services
            self.s1 = Service(service_name="Front Desk Boston", service_key="front-boston", company_id=self.c1.company_id)
            self.s2 = Service(service_name="Experiencia GesDent", service_key="experiencia-gesdent", company_id=self.c2.company_id)
            db.add_all([self.s1, self.s2])
            await db.flush()

            # 3. Typologies
            self.ty1 = Typology(typology_name="Cita Boston", typology_key="cita-boston", service_id=self.s1.service_id, is_active=True)
            self.ty2 = Typology(typology_name="Cita GesDent", typology_key="cita-gesdent", service_id=self.s2.service_id, is_active=True)
            db.add_all([self.ty1, self.ty2])
            await db.flush()

            # 4. Teams
            self.t1 = Team(team_name="Boston A", company_id=self.c1.company_id, service_id=self.s1.service_id)
            self.t2 = Team(team_name="GesDent B", company_id=self.c2.company_id, service_id=self.s2.service_id)
            db.add_all([self.t1, self.t2])
            await db.flush()

            # 5. Users
            self.u_super = User(username="super_admin", email="super@test.com", role="admin", password_hash="dummy")
            self.u_comp1 = User(username="boston_admin", email="boston_admin@test.com", role="company_admin", company_id=self.c1.company_id, password_hash="dummy")
            self.u_mgr1 = User(username="boston_mgr", email="boston_mgr@test.com", role="responsable_servicio", company_id=self.c1.company_id, password_hash="dummy")
            self.u_coor1 = User(username="boston_coor", email="boston_coor@test.com", role="coordinador_equipo", company_id=self.c1.company_id, password_hash="dummy")
            self.u_agent_boston = User(username="agent_boston", email="agent_boston@test.com", role="agente", company_id=self.c1.company_id, hubspot_owner_id="boston_owner", password_hash="dummy")
            self.u_agent_gesdent = User(username="agent_gesdent", email="agent_gesdent@test.com", role="agente", company_id=self.c2.company_id, hubspot_owner_id="gesdent_owner", password_hash="dummy")

            db.add_all([self.u_super, self.u_comp1, self.u_mgr1, self.u_coor1, self.u_agent_boston, self.u_agent_gesdent])
            await db.flush()

            # 6. Associations
            db.add(UserServiceAssociation(user_id=self.u_mgr1.user_id, service_id=self.s1.service_id))
            db.add(UserTeamAssociation(user_id=self.u_coor1.user_id, team_id=self.t1.team_id))
            db.add(AgentTeamAssociation(user_id=self.u_agent_boston.user_id, team_id=self.t1.team_id))
            db.add(AgentTeamAssociation(user_id=self.u_agent_gesdent.user_id, team_id=self.t2.team_id))
            await db.flush()

            # 7. Mass Job & Run (needed for results FKs/logical consistency)
            job = MassEvaluationJob(job_name="Ops Job", prompt_id=1, is_active=True, schedule_enabled=False, created_by="test")
            db.add(job)
            await db.flush()
            run = MassEvaluationRun(job_id=job.job_id, trigger_type="test", status="completed", started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc))
            db.add(run)
            await db.flush()

            # 8. Seed Mass Evaluation Results
            # Result 1: Boston Medical (Luci)
            self.res1 = MassEvaluationResult(
                run_id=run.run_id, job_id=job.job_id, call_id="call_boston_1",
                company_id=self.c1.company_id, service_id=self.s1.service_id, service_key=self.s1.service_key,
                typology_id=self.ty1.typology_id, typology_key=self.ty1.typology_key, typology_name=self.ty1.typology_name,
                hubspot_owner_id="boston_owner", agent_name="Boston Agent", status="completed",
                call_timestamp=datetime.now(timezone.utc) - timedelta(days=2),
                analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=2),
                prompt_id=1, evaluacion_global=8.0, result_json={"tipo_llamada": "cita", "evaluacion_global": 8.0}, items_json=[],
                prompt_snapshot="dummy"
            )
            # Result 2: GesDent (Cristina)
            self.res2 = MassEvaluationResult(
                run_id=run.run_id, job_id=job.job_id, call_id="call_gesdent_2",
                company_id=self.c2.company_id, service_id=self.s2.service_id, service_key=self.s2.service_key,
                typology_id=self.ty2.typology_id, typology_key=self.ty2.typology_key, typology_name=self.ty2.typology_name,
                hubspot_owner_id="gesdent_owner", agent_name="GesDent Agent", status="completed",
                call_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
                analysis_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
                prompt_id=1, evaluacion_global=9.0, result_json={"tipo_llamada": "soporte", "evaluacion_global": 9.0}, items_json=[],
                prompt_snapshot="dummy"
            )
            db.add_all([self.res1, self.res2])
            await db.flush()

            # 9. Seed Mass Evaluation Criterion Results
            self.crit1 = MassEvaluationCriterionResult(
                id=1,
                mass_analysis_id=self.res1.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
                call_id="call_boston_1", criterion_key="empatia", criterion_type="score_1_10",
                numeric_value=8.0, is_applicable=True
            )
            self.crit2 = MassEvaluationCriterionResult(
                id=2,
                mass_analysis_id=self.res2.mass_analysis_id, run_id=run.run_id, job_id=job.job_id,
                call_id="call_gesdent_2", criterion_key="empatia", criterion_type="score_1_10",
                numeric_value=9.0, is_applicable=True
            )
            db.add_all([self.crit1, self.crit2])
            await db.flush()

            # 10. Seed Legacy Individual Analyses
            self.a1 = Analysis(
                analysis_id=1,
                call_id="call_boston_1", company_id=self.c1.company_id, service_id=self.s1.service_id,
                hubspot_owner_id="boston_owner", agente_telefonico="Boston Agent", evaluacion_global=8.0,
                analysis_type="audio", status="completed", prompt_id=1
            )
            self.a2 = Analysis(
                analysis_id=2,
                call_id="call_gesdent_2", company_id=self.c2.company_id, service_id=self.s2.service_id,
                hubspot_owner_id="gesdent_owner", agente_telefonico="GesDent Agent", evaluacion_global=9.0,
                analysis_type="audio", status="completed", prompt_id=1
            )
            db.add_all([self.a1, self.a2])
            await db.flush()

            # Seed CallAnalysisCurrent
            self.cur1 = CallAnalysisCurrent(
                call_id="call_boston_1", company_id=self.c1.company_id, service_id=self.s1.service_id,
                hubspot_owner_id="boston_owner", agente_telefonico="Boston Agent", evaluacion_global=8.0,
                analysis_type="audio", latest_analysis_id=self.a1.analysis_id
            )
            self.cur2 = CallAnalysisCurrent(
                call_id="call_gesdent_2", company_id=self.c2.company_id, service_id=self.s2.service_id,
                hubspot_owner_id="gesdent_owner", agente_telefonico="GesDent Agent", evaluacion_global=9.0,
                analysis_type="audio", latest_analysis_id=self.a2.analysis_id
            )
            db.add_all([self.cur1, self.cur2])
            await db.flush()

            # Save objects/IDs
            self.super_user_id = self.u_super.user_id
            self.boston_admin_id = self.u_comp1.user_id
            self.boston_mgr_id = self.u_mgr1.user_id
            self.boston_coor_id = self.u_coor1.user_id
            self.agent_boston_id = self.u_agent_boston.user_id
            self.agent_gesdent_id = self.u_agent_gesdent.user_id

            self.boston_company_id = self.c1.company_id
            self.gesdent_company_id = self.c2.company_id
            self.boston_service_id = self.s1.service_id
            self.gesdent_service_id = self.s2.service_id

            await db.commit()

    async def asyncTearDown(self):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        if os.path.exists("multitenancy_ops_test.db"):
            try:
                os.remove("multitenancy_ops_test.db")
            except Exception:
                pass
        app.dependency_overrides.clear()

    def _override_user(self, user_id: int):
        async def mock_get_current_user(db: AsyncSession = Depends(get_db)):
            return await db.get(User, user_id)
        app.dependency_overrides[get_current_user] = mock_get_current_user

    async def test_analyses_endpoints_scoping(self):
        """Test analyses listing endpoints scoping per role."""
        # 1. Super Admin sees all
        self._override_user(self.super_user_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/analyses")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(len(res.json()), 2)

            res_hist = await ac.get("/bm/analyses/history")
            self.assertEqual(res_hist.status_code, 200)
            self.assertEqual(len(res_hist.json()), 2)

            # Can access details of both
            res_det1 = await ac.get(f"/bm/analysis-detail?call_id=call_boston_1&type=audio")
            self.assertEqual(res_det1.status_code, 200)
            res_det2 = await ac.get(f"/bm/analysis-detail?call_id=call_gesdent_2&type=audio")
            self.assertEqual(res_det2.status_code, 200)

        # 2. Boston Company Admin only sees Boston Medical
        self._override_user(self.boston_admin_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/analyses")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(len(res.json()), 1)
            self.assertEqual(res.json()[0]["call_id"], "call_boston_1")

            # Cannot access GesDent details (404/NotFound or Empty because of scoping)
            res_det2 = await ac.get(f"/bm/analysis-detail?call_id=call_gesdent_2&type=audio")
            self.assertEqual(res_det2.status_code, 404)

        # 3. Boston Service Manager only sees Boston Medical Front Desk
        self._override_user(self.boston_mgr_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/analyses")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(len(res.json()), 1)
            self.assertEqual(res.json()[0]["call_id"], "call_boston_1")

        # 4. Agent Boston only sees agent's own data
        self._override_user(self.agent_boston_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/analyses")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(len(res.json()), 1)
            self.assertEqual(res.json()[0]["call_id"], "call_boston_1")

        # 5. Agent GesDent only sees GesDent data
        self._override_user(self.agent_gesdent_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/analyses")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(len(res.json()), 1)
            self.assertEqual(res.json()[0]["call_id"], "call_gesdent_2")

    async def test_dashboard_endpoints_scoping(self):
        """Test dashboard summary and comparison scoping per role."""
        # 1. Super Admin
        self._override_user(self.super_user_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Summary sees both
            res = await ac.get("/bm/dashboard/summary?period=30d")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["kpis"]["total_analyses"], 2)

            # Agents comparison sees both agent names
            res_comp = await ac.get("/bm/dashboard/agents-comparison?period=30d")
            self.assertEqual(res_comp.status_code, 200)
            agents = [a["agent_name"] for a in res_comp.json()["agents"]]
            self.assertIn("Boston Agent", agents)
            self.assertIn("GesDent Agent", agents)

            # /agents returns both
            res_ag = await ac.get("/bm/agents")
            self.assertEqual(res_ag.status_code, 200)
            self.assertTrue(any(a["hubspot_owner_id"] == "boston_owner" for a in res_ag.json()))
            self.assertTrue(any(a["hubspot_owner_id"] == "gesdent_owner" for a in res_ag.json()))

        # 2. Boston Company Admin
        self._override_user(self.boston_admin_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Summary only sees Boston (total_calls = 1)
            res = await ac.get("/bm/dashboard/summary?period=30d")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["kpis"]["total_analyses"], 1)

            # /agents only returns Boston Agent
            res_ag = await ac.get("/bm/agents")
            self.assertEqual(res_ag.status_code, 200)
            self.assertTrue(any(a["hubspot_owner_id"] == "boston_owner" for a in res_ag.json()))
            self.assertFalse(any(a["hubspot_owner_id"] == "gesdent_owner" for a in res_ag.json()))

            # Trying to bypass service_id to GesDent results in empty summary
            res_bypass = await ac.get(f"/bm/dashboard/summary?period=30d&service_id={self.gesdent_service_id}")
            self.assertEqual(res_bypass.status_code, 200)
            self.assertEqual(res_bypass.json()["kpis"]["total_analyses"], 0)

        # 3. Agent Boston
        self._override_user(self.agent_boston_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Summary only sees Boston
            res = await ac.get("/bm/dashboard/summary?period=30d")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["kpis"]["total_analyses"], 1)

            # Evolution for self is fine
            res_evo = await ac.get(f"/bm/agents/boston_owner/evolution?period=30d")
            self.assertEqual(res_evo.status_code, 200)

            # Evolution for GesDent agent is Forbidden (403)
            res_evo2 = await ac.get(f"/bm/agents/gesdent_owner/evolution?period=30d")
            self.assertEqual(res_evo2.status_code, 403)

    async def test_analytics_v2_endpoints_scoping(self):
        """Test Analytics V2 comparison and evolution scoping."""
        # 1. Super Admin
        self._override_user(self.super_user_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_items = await ac.get("/bm/analytics/items")
            self.assertEqual(res_items.status_code, 200)

            # Comparison shows both
            res_comp = await ac.get("/bm/analytics/agents-comparison")
            self.assertEqual(res_comp.status_code, 200)
            agents = [a["hubspot_owner_id"] for a in res_comp.json()["agents"]]
            self.assertIn("boston_owner", agents)
            self.assertIn("gesdent_owner", agents)

            # Filter options shows both typologies
            res_filters = await ac.get("/bm/filter-options")
            self.assertEqual(res_filters.status_code, 200)
            typos = [t["typology_key"] for t in res_filters.json()["typologies"]]
            self.assertIn("cita-boston", typos)
            self.assertIn("cita-gesdent", typos)

        # 2. Boston Company Admin
        self._override_user(self.boston_admin_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Comparison only shows Boston
            res_comp = await ac.get("/bm/analytics/agents-comparison")
            self.assertEqual(res_comp.status_code, 200)
            agents = [a["hubspot_owner_id"] for a in res_comp.json()["agents"]]
            self.assertIn("boston_owner", agents)
            self.assertFalse(any(oid == "gesdent_owner" for oid in agents))

            # Filter options only shows Boston typology
            res_filters = await ac.get("/bm/filter-options")
            self.assertEqual(res_filters.status_code, 200)
            typos = [t["typology_key"] for t in res_filters.json()["typologies"]]
            self.assertIn("cita-boston", typos)
            self.assertNotIn("cita-gesdent", typos)

    async def test_service_evolution_endpoints_scoping(self):
        """Test Service Evolution endpoints scoping."""
        # 1. Super Admin
        self._override_user(self.super_user_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # List services returns both
            res_svcs = await ac.get("/bm/service-evolution/services")
            self.assertEqual(res_svcs.status_code, 200)
            self.assertEqual(len(res_svcs.json()), 2)

            # List criteria returns both
            res_crit = await ac.get("/bm/service-evolution/criteria")
            self.assertEqual(res_crit.status_code, 200)
            criteria_keys = [c["criterion_key"] for c in res_crit.json()]
            self.assertIn("empatia", criteria_keys)

            # Evolution shows both
            res_evo = await ac.get("/bm/service-evolution")
            self.assertEqual(res_evo.status_code, 200)
            self.assertEqual(res_evo.json()["summary"]["total_calls"], 2)

        # 2. Boston Company Admin
        self._override_user(self.boston_admin_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # List services only returns Boston Front Desk
            res_svcs = await ac.get("/bm/service-evolution/services")
            self.assertEqual(res_svcs.status_code, 200)
            self.assertEqual(len(res_svcs.json()), 1)
            self.assertEqual(res_svcs.json()[0]["service_id"], self.boston_service_id)

            # Evolution only returns Boston Medical calls
            res_evo = await ac.get("/bm/service-evolution")
            self.assertEqual(res_evo.status_code, 200)
            self.assertEqual(res_evo.json()["summary"]["total_calls"], 1)

            # Trying to bypass service_id to GesDent results in empty evolution
            res_evo_ges = await ac.get(f"/bm/service-evolution?service_id={self.gesdent_service_id}")
            self.assertEqual(res_evo_ges.status_code, 200)
            self.assertEqual(res_evo_ges.json()["summary"]["total_calls"], 0)


if __name__ == "__main__":
    unittest.main()
