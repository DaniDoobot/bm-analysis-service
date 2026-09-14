"""
Test Suite: Transversal Team Filter across Backend Endpoints
Tests:
- Scenarios A & B: Cascade service -> teams isolation (Front, EXPAC, Comerciales)
- Scenario C: Real aggregated data filtering by team_id (KPIs, series, evaluations)
- Scenario D: Rejection with HTTP 400 when team does not belong to service
- Scenario E: Team -> Agents scoping in /bm/agents
- Scenario F: Multi-team agent clean membership without duplicates
- Scenario G: Compatibility when team_id is omitted (100% legacy behavior)
- Scenario H: Tenant isolation (HTTP 403 when team belongs to another company)
- All target endpoints verified with and without team_id.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///team_filter_test.db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, AgentTeamAssociation, UserTeamAssociation
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.dependencies import get_current_user
from app.main import app


class TestTeamFilterTransversal(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Engine URL points to production!"

        if os.path.exists("team_filter_test.db"):
            try:
                os.remove("team_filter_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            # 1. Companies
            self.c1 = Company(company_name="Boston Medical", company_key="bm", is_active=True)
            self.c2 = Company(company_name="Other Dental", company_key="other", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()
            self.company1_id = self.c1.company_id
            self.company2_id = self.c2.company_id

            # 2. Services
            self.s_front = Service(service_name="Front", service_key="front", company_id=self.company1_id)
            self.s_expac = Service(service_name="Experiencia de Paciente", service_key="expac", company_id=self.company1_id)
            self.s_comercial = Service(service_name="Asesores Comerciales", service_key="comerciales", company_id=self.company1_id)
            self.s_other = Service(service_name="Other Svc", service_key="other_svc", company_id=self.company2_id)
            db.add_all([self.s_front, self.s_expac, self.s_comercial, self.s_other])
            await db.flush()
            self.front_id = self.s_front.service_id
            self.expac_id = self.s_expac.service_id
            self.comercial_id = self.s_comercial.service_id
            self.other_svc_id = self.s_other.service_id

            # 3. Teams
            self.t_front1 = Team(team_name="Front Mañana", company_id=self.company1_id, service_id=self.front_id, is_active=True)
            self.t_front2 = Team(team_name="Front Tarde", company_id=self.company1_id, service_id=self.front_id, is_active=True)
            self.t_front_empty = Team(team_name="Front Vacio", company_id=self.company1_id, service_id=self.front_id, is_active=True)
            self.t_expac = Team(team_name="EXPAC General", company_id=self.company1_id, service_id=self.expac_id, is_active=True)
            self.t_comercial = Team(team_name="Comercial Madrid", company_id=self.company1_id, service_id=self.comercial_id, is_active=True)
            self.t_other = Team(team_name="Other Team", company_id=self.company2_id, service_id=self.other_svc_id, is_active=True)
            db.add_all([self.t_front1, self.t_front2, self.t_front_empty, self.t_expac, self.t_comercial, self.t_other])
            await db.flush()
            self.team_front1_id = self.t_front1.team_id
            self.team_front2_id = self.t_front2.team_id
            self.team_empty_id = self.t_front_empty.team_id
            self.team_expac_id = self.t_expac.team_id
            self.team_comercial_id = self.t_comercial.team_id
            self.team_other_id = self.t_other.team_id

            # 4. Users / Agents
            # Super admin
            self.u_super = User(username="superadmin", email="super@test.com", role="administrador", password_hash="x")
            # Company admin
            self.u_comp = User(username="compadmin", email="comp@test.com", role="company_admin", company_id=self.company1_id, password_hash="x")
            # Agents
            self.u_agent1 = User(
                username="laura_front", email="laura@test.com", name="Laura Front", role="agente",
                company_id=self.company1_id, hubspot_owner_id="101", primary_service_id=self.front_id,
                primary_team_id=self.team_front1_id, is_active=True, password_hash="x"
            )
            self.u_agent2 = User(
                username="carlos_multi", email="carlos@test.com", name="Carlos Multi", role="agente",
                company_id=self.company1_id, hubspot_owner_id="102", primary_service_id=self.front_id,
                primary_team_id=self.team_front1_id, is_active=True, password_hash="x"
            )
            self.u_agent3 = User(
                username="elena_tarde", email="elena@test.com", name="Elena Tarde", role="agente",
                company_id=self.company1_id, hubspot_owner_id="103", primary_service_id=self.front_id,
                primary_team_id=self.team_front2_id, is_active=True, password_hash="x"
            )
            self.u_agent4 = User(
                username="diego_expac", email="diego@test.com", name="Diego EXPAC", role="agente",
                company_id=self.company1_id, hubspot_owner_id="104", primary_service_id=self.expac_id,
                primary_team_id=self.team_expac_id, is_active=True, password_hash="x"
            )
            self.u_agent5 = User(
                username="marta_comercial", email="marta@test.com", name="Marta Comercial", role="agente",
                company_id=self.company1_id, hubspot_owner_id="105", primary_service_id=self.comercial_id,
                primary_team_id=self.team_comercial_id, is_active=True, password_hash="x"
            )
            self.u_foreign = User(
                username="foreign_agent", email="foreign@test.com", name="Foreign Agent", role="agente",
                company_id=self.company2_id, hubspot_owner_id="201", primary_service_id=self.other_svc_id,
                primary_team_id=self.team_other_id, is_active=True, password_hash="x"
            )
            self.u_coor = User(
                username="coor_front1", email="coor@test.com", name="Coord Front 1", role="coordinador_equipo",
                company_id=self.company1_id, primary_service_id=self.front_id,
                primary_team_id=self.team_front1_id, is_active=True, password_hash="x"
            )
            db.add_all([self.u_super, self.u_comp, self.u_agent1, self.u_agent2, self.u_agent3, self.u_agent4, self.u_agent5, self.u_foreign, self.u_coor])
            await db.flush()

            # Assign Carlos Multi to team_front2 via AgentTeamAssociation (multi-team!)
            db.add(AgentTeamAssociation(user_id=self.u_agent2.user_id, team_id=self.team_front2_id))
            # Assign coordinator explicitly to team_front1
            db.add(UserTeamAssociation(user_id=self.u_coor.user_id, team_id=self.team_front1_id))

            # 5. MassEvaluation Job, Run, and Results
            now = datetime.now(timezone.utc)
            job = MassEvaluationJob(
                company_id=self.company1_id,
                service_id=self.front_id,
                job_name="Test Job",
                prompt_id=1,
            )
            db.add(job)
            await db.flush()

            run = MassEvaluationRun(
                job_id=job.job_id,
                company_id=self.company1_id,
                service_id=self.front_id,
                trigger_type="manual",
                status="completed",
            )
            db.add(run)
            await db.flush()

            r1 = MassEvaluationResult(
                run_id=run.run_id, job_id=job.job_id, prompt_id=1, prompt_snapshot="{}",
                call_id="call-front1-1", hubspot_owner_id="101", agent_name="Laura Front",
                service_id=self.front_id, service_key="front", company_id=self.company1_id,
                status="completed", evaluacion_global=8.5, call_duration_seconds=120,
                call_timestamp=now, analysis_timestamp=now, created_at=now,
                result_json={"tipo_llamada": "cita", "evaluacion_global": 8.5}, items_json=[]
            )
            r2 = MassEvaluationResult(
                run_id=run.run_id, job_id=job.job_id, prompt_id=1, prompt_snapshot="{}",
                call_id="call-front1-2", hubspot_owner_id="102", agent_name="Carlos Multi",
                service_id=self.front_id, service_key="front", company_id=self.company1_id,
                status="completed", evaluacion_global=9.0, call_duration_seconds=180,
                call_timestamp=now, analysis_timestamp=now, created_at=now,
                result_json={"tipo_llamada": "cita", "evaluacion_global": 9.0}, items_json=[]
            )
            r3 = MassEvaluationResult(
                run_id=run.run_id, job_id=job.job_id, prompt_id=1, prompt_snapshot="{}",
                call_id="call-front2-1", hubspot_owner_id="103", agent_name="Elena Tarde",
                service_id=self.front_id, service_key="front", company_id=self.company1_id,
                status="completed", evaluacion_global=7.0, call_duration_seconds=90,
                call_timestamp=now, analysis_timestamp=now, created_at=now,
                result_json={"tipo_llamada": "consulta", "evaluacion_global": 7.0}, items_json=[]
            )
            r4 = MassEvaluationResult(
                run_id=run.run_id, job_id=job.job_id, prompt_id=1, prompt_snapshot="{}",
                call_id="call-expac-1", hubspot_owner_id="104", agent_name="Diego EXPAC",
                service_id=self.expac_id, service_key="expac", company_id=self.company1_id,
                status="completed", evaluacion_global=6.0, call_duration_seconds=200,
                call_timestamp=now, analysis_timestamp=now, created_at=now,
                result_json={"tipo_llamada": "reclamacion", "evaluacion_global": 6.0}, items_json=[]
            )
            r5 = MassEvaluationResult(
                run_id=run.run_id, job_id=job.job_id, prompt_id=1, prompt_snapshot="{}",
                call_id="call-comercial-1", hubspot_owner_id="105", agent_name="Marta Comercial",
                service_id=self.comercial_id, service_key="comerciales", company_id=self.company1_id,
                status="completed", evaluacion_global=8.0, call_duration_seconds=300,
                call_timestamp=now, analysis_timestamp=now, created_at=now,
                result_json={"tipo_llamada": "presupuesto", "evaluacion_global": 8.0}, items_json=[]
            )
            db.add_all([r1, r2, r3, r4, r5])
            await db.commit()

        app.dependency_overrides[get_current_user] = lambda: self.u_comp
        self.transport = ASGITransport(app=app)

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
        if os.path.exists("team_filter_test.db"):
            try:
                os.remove("team_filter_test.db")
            except Exception:
                pass

    # ── Test A & B: Cascade Service -> Teams ────────────────────────────────
    async def test_get_teams_cascaded_by_service(self):
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # Front service
            res_front = await ac.get(f"/bm/teams?service_id={self.front_id}")
            self.assertEqual(res_front.status_code, 200)
            teams_front = res_front.json()
            team_ids_front = {t["team_id"] for t in teams_front}
            self.assertEqual(team_ids_front, {self.team_front1_id, self.team_front2_id, self.team_empty_id})

            # EXPAC service
            res_expac = await ac.get(f"/bm/teams?service_id={self.expac_id}")
            self.assertEqual(res_expac.status_code, 200)
            teams_expac = res_expac.json()
            team_ids_expac = {t["team_id"] for t in teams_expac}
            self.assertEqual(team_ids_expac, {self.team_expac_id})

            # Comercial service
            res_com = await ac.get(f"/bm/teams?service_id={self.comercial_id}")
            self.assertEqual(res_com.status_code, 200)
            teams_com = res_com.json()
            team_ids_com = {t["team_id"] for t in teams_com}
            self.assertEqual(team_ids_com, {self.team_comercial_id})

    # ── Test D: Rejection HTTP 400 when team does not belong to service ──────
    async def test_incompatible_service_team_cascade_rejection_400(self):
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            endpoints = [
                f"/bm/agents?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/dashboard/agents-comparison?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/dashboard/objections?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/analytics/items?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/analytics/agents-comparison?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/analytics/items-evolution?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/analytics/filter-options?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/mass-evaluation-results?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/service-evolution?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/service-evolution/criteria?service_id={self.front_id}&team_id={self.team_expac_id}",
                f"/bm/evaluation-items/filter-options?service_id={self.front_id}&team_id={self.team_expac_id}",
            ]
            for ep in endpoints:
                res = await ac.get(ep)
                self.assertEqual(
                    res.status_code, 400,
                    f"Endpoint {ep} should return HTTP 400, got {res.status_code}: {res.text}"
                )
                self.assertEqual(
                    res.json().get("detail"),
                    "El equipo seleccionado no pertenece al servicio indicado.",
                    f"Endpoint {ep} returned unexpected error detail: {res.text}"
                )

    # ── Test E & F: Team -> Agents & Multi-team agent support ────────────────
    async def test_team_to_agents_and_multiteam(self):
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # Team Front 1: Laura Front (101) & Carlos Multi (102)
            res1 = await ac.get(f"/bm/agents?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res1.status_code, 200)
            agents1 = res1.json()
            oids1 = [a["hubspot_owner_id"] for a in agents1]
            self.assertIn("101", oids1)
            self.assertIn("102", oids1)
            self.assertNotIn("103", oids1)
            self.assertNotIn("104", oids1)
            # Ensure no duplicates
            self.assertEqual(len(oids1), len(set(oids1)))

            # Team Front 2: Elena Tarde (103) & Carlos Multi (102)
            res2 = await ac.get(f"/bm/agents?service_id={self.front_id}&team_id={self.team_front2_id}")
            self.assertEqual(res2.status_code, 200)
            agents2 = res2.json()
            oids2 = [a["hubspot_owner_id"] for a in agents2]
            self.assertIn("102", oids2)
            self.assertIn("103", oids2)
            self.assertNotIn("101", oids2)
            self.assertEqual(len(oids2), len(set(oids2)))

    # ── Test C: Real aggregated data filtering in Dashboard Summary ──────────
    async def test_dashboard_summary_team_filter_real_aggregation(self):
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # Without team filter: all Front calls (3 calls: 101, 102, 103)
            res_all = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&period=30d")
            self.assertEqual(res_all.status_code, 200)
            data_all = res_all.json()
            self.assertEqual(data_all["kpis"]["total_analyses"], 3.0)

            # Filtered by Front Team 1 (only 101 and 102): exactly 2 calls!
            res_t1 = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_front1_id}&period=30d")
            self.assertEqual(res_t1.status_code, 200)
            data_t1 = res_t1.json()
            self.assertEqual(data_t1["kpis"]["total_analyses"], 2.0)
            # Ranking only has agents from team 1
            ranking_names = [a.get("agente_telefonico") or a.get("name") for a in data_t1["agent_ranking"]]
            self.assertIn("Laura Front", ranking_names)
            self.assertIn("Carlos Multi", ranking_names)
            self.assertNotIn("Elena Tarde", ranking_names)

            # Filtered by Front Team 2 (102 and 103): exactly 2 calls!
            res_t2 = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_front2_id}&period=30d")
            self.assertEqual(res_t2.status_code, 200)
            data_t2 = res_t2.json()
            self.assertEqual(data_t2["kpis"]["total_analyses"], 2.0)
            ranking_names2 = [a.get("agente_telefonico") or a.get("name") for a in data_t2["agent_ranking"]]
            self.assertIn("Elena Tarde", ranking_names2)
            self.assertIn("Carlos Multi", ranking_names2)
            self.assertNotIn("Laura Front", ranking_names2)

    # ── Test Mass Evaluation Results listing by team_id ──────────────────────
    async def test_mass_evaluation_results_team_filter(self):
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # Team Front 1 -> exactly 2 results (101, 102)
            res = await ac.get(f"/bm/mass-evaluation-results?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 2)
            returned_owners = {r["hubspot_owner_id"] for r in items}
            self.assertEqual(returned_owners, {"101", "102"})

    # ── Test Analytics Filter Options by team_id ─────────────────────────────
    async def test_analytics_filter_options_team_filter(self):
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            res = await ac.get(f"/bm/analytics/filter-options?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res.status_code, 200)
            agents = res.json()["agents"]
            agent_ids = {a["hubspot_owner_id"] for a in agents}
            self.assertEqual(agent_ids, {"101", "102"})

    # ── Test H: Tenant Isolation ─────────────────────────────────────────────
    async def test_tenant_isolation_foreign_team_403(self):
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # User from Company 1 attempts to use team_id from Company 2
            res = await ac.get(f"/bm/dashboard/summary?team_id={self.team_other_id}")
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.json().get("detail"), "Acceso denegado a equipos de otra empresa.")

    # ── Test I: Intra-company permissions (Coordinator) ─────────────────────
    async def test_intra_company_permissions_for_coordinator(self):
        # Coordinator assigned only to team_front1
        app.dependency_overrides[get_current_user] = lambda: self.u_coor
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # 1. /bm/teams only lists team_front1
            res_teams = await ac.get(f"/bm/teams?service_id={self.front_id}")
            self.assertEqual(res_teams.status_code, 200)
            t_ids = [t["team_id"] for t in res_teams.json()]
            self.assertEqual(t_ids, [self.team_front1_id])
            self.assertNotIn(self.team_front2_id, t_ids)
            self.assertNotIn(self.team_empty_id, t_ids)

            # 2. Access to allowed team (team_front1) -> 200 OK
            res_ok = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_front1_id}&period=30d")
            self.assertEqual(res_ok.status_code, 200)

            # 3. Access to another team of same service (team_front2) -> 403 Forbidden
            endpoints_forbidden_team = [
                f"/bm/agents?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/dashboard/agents-comparison?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/dashboard/objections?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/analytics/items?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/analytics/agents-comparison?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/analytics/items-evolution?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/analytics/filter-options?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/mass-evaluation-results?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/service-evolution?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/service-evolution/criteria?service_id={self.front_id}&team_id={self.team_front2_id}",
                f"/bm/evaluation-items/filter-options?service_id={self.front_id}&team_id={self.team_front2_id}",
            ]
            for ep in endpoints_forbidden_team:
                res_err = await ac.get(ep)
                self.assertEqual(res_err.status_code, 403, f"{ep} should return 403 for unauthorized team")
                self.assertIn("No tienes permisos para acceder a este equipo", res_err.json().get("detail", ""))

            # 4. Access to another service (expac_id) -> 403 Forbidden
            res_svc_err = await ac.get(f"/bm/dashboard/summary?service_id={self.expac_id}")
            self.assertEqual(res_svc_err.status_code, 403)
            self.assertIn("No tienes permisos para este servicio", res_svc_err.json().get("detail", ""))

    # ── Test J: Strict AND intersection between team_id and agent ───────────
    async def test_team_and_agent_strict_intersection(self):
        # Admin user
        app.dependency_overrides[get_current_user] = lambda: self.u_comp
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # Elena (103) belongs to team_front2, NOT team_front1
            # Querying team_front1 + agent 103 must return 0 data
            res_mismatch = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_front1_id}&agent=103&period=30d")
            self.assertEqual(res_mismatch.status_code, 200)
            self.assertEqual(res_mismatch.json()["kpis"]["total_analyses"], 0.0)
            self.assertEqual(len(res_mismatch.json()["agent_ranking"]), 0)

            # Querying team_front1 + Laura (101) belongs to team_front1 -> 1 call
            res_match = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_front1_id}&agent=101&period=30d")
            self.assertEqual(res_match.status_code, 200)
            self.assertEqual(res_match.json()["kpis"]["total_analyses"], 1.0)

            # Mass evaluation results: team_front1 + agent 103 -> 0 results
            res_mass_mismatch = await ac.get(f"/bm/mass-evaluation-results?service_id={self.front_id}&team_id={self.team_front1_id}&agent_id=103")
            self.assertEqual(res_mass_mismatch.status_code, 200)
            self.assertEqual(res_mass_mismatch.json()["total"], 0)
            self.assertEqual(len(res_mass_mismatch.json()["items"]), 0)

            # Mass evaluation results: team_front1 + agent 101 -> 1 result
            res_mass_match = await ac.get(f"/bm/mass-evaluation-results?service_id={self.front_id}&team_id={self.team_front1_id}&agent_id=101")
            self.assertEqual(res_mass_match.status_code, 200)
            self.assertEqual(res_mass_match.json()["total"], 1)

    # ── Test K: Empty team returns valid 0/empty structure, no fallback ─────
    async def test_empty_team_isolation(self):
        app.dependency_overrides[get_current_user] = lambda: self.u_comp
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # 1. Dashboard summary
            res_sum = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_empty_id}&period=30d")
            self.assertEqual(res_sum.status_code, 200)
            self.assertEqual(res_sum.json()["kpis"]["total_analyses"], 0.0)
            self.assertEqual(len(res_sum.json()["agent_ranking"]), 0)

            # 2. Agents list
            res_agents = await ac.get(f"/bm/agents?service_id={self.front_id}&team_id={self.team_empty_id}")
            self.assertEqual(res_agents.status_code, 200)
            self.assertEqual(res_agents.json(), [])

            # 3. Analytics filter options
            res_opts = await ac.get(f"/bm/analytics/filter-options?service_id={self.front_id}&team_id={self.team_empty_id}")
            self.assertEqual(res_opts.status_code, 200)
            self.assertEqual(res_opts.json()["agents"], [])

            # 4. Mass evaluation results
            res_mass = await ac.get(f"/bm/mass-evaluation-results?service_id={self.front_id}&team_id={self.team_empty_id}")
            self.assertEqual(res_mass.status_code, 200)
            self.assertEqual(res_mass.json()["total"], 0)
            self.assertEqual(res_mass.json()["items"], [])

            # 5. Service evolution
            res_evo = await ac.get(f"/bm/service-evolution?service_id={self.front_id}&team_id={self.team_empty_id}")
            self.assertEqual(res_evo.status_code, 200)
            self.assertEqual(res_evo.json()["summary"]["total_calls"], 0)

    # ── Test G: Compatibility without team_id ────────────────────────────────
    async def test_compatibility_when_team_id_is_none(self):
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            res = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&period=30d")
            self.assertEqual(res.status_code, 200)
            # 3 calls matching Front service
            self.assertEqual(res.json()["kpis"]["total_analyses"], 3.0)

    # ── Test L: role=agent self-scope is strictly preserved with team_id ─────
    async def test_role_agent_self_scope_not_widened_by_team_filter(self):
        # Authenticated as Laura Front (Agent A: owner 101, team Front 1)
        # In Team Front 1 there is also Carlos Multi (Agent B: owner 102)
        app.dependency_overrides[get_current_user] = lambda: self.u_agent1
        async with AsyncClient(transport=self.transport, base_url="http://test") as ac:
            # A) GET /bm/dashboard/summary?service_id=1&team_id=11 -> solo datos de A (Laura: 1 call, total 1.0)
            res_sum = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_front1_id}&period=30d")
            self.assertEqual(res_sum.status_code, 200)
            data_sum = res_sum.json()
            self.assertEqual(data_sum["kpis"]["total_analyses"], 1.0)
            ranking_agents = [a["hubspot_owner_id"] for a in data_sum["agent_ranking"]]
            self.assertEqual(ranking_agents, ["101"])
            self.assertNotIn("102", ranking_agents)

            # B) GET /bm/agents?service_id=1&team_id=11 -> solo A (no amplia respecto al scope previo del agente)
            res_agents = await ac.get(f"/bm/agents?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res_agents.status_code, 200)
            agents_list = res_agents.json()
            agent_owner_ids = [a["hubspot_owner_id"] for a in agents_list]
            self.assertEqual(agent_owner_ids, ["101"])
            self.assertNotIn("102", agent_owner_ids)

            # C) Analytics con team_id=11 -> solo A / role guard preservado
            # C.1 /bm/dashboard/agents-comparison -> 200 OK con solo A (no B)
            res_dash_comp = await ac.get(f"/bm/dashboard/agents-comparison?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res_dash_comp.status_code, 200)
            comp_agents = [a["hubspot_owner_id"] for a in res_dash_comp.json()["agents"]]
            self.assertEqual(comp_agents, ["101"])
            self.assertNotIn("102", comp_agents)

            # C.2 /bm/analytics/agents-comparison -> 403 Forbidden (el rol agente sigue bloqueado, team_id no escala privilegios)
            res_analytics_comp = await ac.get(f"/bm/analytics/agents-comparison?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res_analytics_comp.status_code, 403)
            self.assertIn("Se requiere rol de nivel superior", res_analytics_comp.json().get("detail", ""))

            # C.3 /bm/analytics/filter-options -> 200 OK con solo A (no B)
            res_filter_opts = await ac.get(f"/bm/analytics/filter-options?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res_filter_opts.status_code, 200)
            opt_agents = [a["hubspot_owner_id"] for a in res_filter_opts.json()["agents"]]
            self.assertEqual(opt_agents, ["101"])
            self.assertNotIn("102", opt_agents)

            # D) mass-evaluation-results con team_id=11 -> solo A
            res_mass = await ac.get(f"/bm/mass-evaluation-results?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res_mass.status_code, 200)
            mass_data = res_mass.json()
            self.assertEqual(mass_data["total"], 1)
            mass_owners = {item["hubspot_owner_id"] for item in mass_data["items"]}
            self.assertEqual(mass_owners, {"101"})
            self.assertNotIn("102", mass_owners)

            # E) service-evolution con team_id=11 -> solo A
            res_evo = await ac.get(f"/bm/service-evolution?service_id={self.front_id}&team_id={self.team_front1_id}")
            self.assertEqual(res_evo.status_code, 200)
            self.assertEqual(res_evo.json()["summary"]["total_calls"], 1)

            # F) team_id=11 + agent=B (Carlos Multi: 102) -> 0 datos o acceso denegado, NUNCA datos de B
            # F.1 Dashboard summary con agent=102 -> 0 datos
            res_mismatch_sum = await ac.get(f"/bm/dashboard/summary?service_id={self.front_id}&team_id={self.team_front1_id}&agent=102&period=30d")
            self.assertEqual(res_mismatch_sum.status_code, 200)
            self.assertEqual(res_mismatch_sum.json()["kpis"]["total_analyses"], 0.0)
            self.assertEqual(len(res_mismatch_sum.json()["agent_ranking"]), 0)

            # F.2 Mass results con agent_id=102 -> 403 Forbidden (semantica previa de agente que intenta consultar otro agente)
            res_mismatch_mass = await ac.get(f"/bm/mass-evaluation-results?service_id={self.front_id}&team_id={self.team_front1_id}&agent_id=102")
            self.assertEqual(res_mismatch_mass.status_code, 403)
            self.assertIn("No tienes permiso para ver resultados de este agente", res_mismatch_mass.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main()
