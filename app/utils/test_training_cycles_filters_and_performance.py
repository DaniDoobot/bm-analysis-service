import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from sqlalchemy import event

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///training_filters_perf_test.db"

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
)
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.services.personalized_training_service import PersonalizedTrainingService
from app.utils.security import create_access_token
from app.main import app
from sqlalchemy.ext.asyncio import AsyncSession


class TestTrainingCyclesFiltersAndPerformance(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("training_filters_perf_test.db"):
            try:
                os.remove("training_filters_perf_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = get_engine()
        async with AsyncSession(self.session_factory) as db:
            # 1. Company
            self.c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            self.c2 = Company(company_id=2, company_name="Other Clinic", company_key="other-clinic", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()

            # 2. Services
            self.s1 = Service(service_id=1, service_name="Front Desk Boston", service_key="front-boston", company_id=self.c1.company_id)
            self.s2 = Service(service_id=2, service_name="Commercial Boston", service_key="comm-boston", company_id=self.c1.company_id)
            self.s3 = Service(service_id=3, service_name="Other Service", service_key="other-srv", company_id=self.c2.company_id)
            db.add_all([self.s1, self.s2, self.s3])
            await db.flush()

            # 3. Teams
            self.t1 = Team(team_id=1, team_name="Team Alpha (Service 1)", company_id=self.c1.company_id, service_id=self.s1.service_id)
            self.t2 = Team(team_id=2, team_name="Team Beta (Service 2)", company_id=self.c1.company_id, service_id=self.s2.service_id)
            self.t_empty = Team(team_id=3, team_name="Empty Team (Service 1)", company_id=self.c1.company_id, service_id=self.s1.service_id)
            self.t_other = Team(team_id=4, team_name="Other Team (Company 2)", company_id=self.c2.company_id, service_id=self.s3.service_id)
            db.add_all([self.t1, self.t2, self.t_empty, self.t_other])
            await db.flush()

            # 4. Users (Admin, Manager, Coordinator)
            self.u_admin = User(user_id=1, username="admin_bm", email="admin@bm.com", role="company_admin", company_id=self.c1.company_id, password_hash="x")
            self.u_mgr1 = User(user_id=2, username="mgr_s1", email="mgr@bm.com", role="responsable_servicio", company_id=self.c1.company_id, primary_service_id=self.s1.service_id, password_hash="x")
            self.u_coord1 = User(user_id=3, username="coord_t1", email="coord@bm.com", role="coordinador_equipo", company_id=self.c1.company_id, primary_team_id=self.t1.team_id, password_hash="x")

            # Agents in Team 1 (Service 1)
            self.agent_t1_a = User(user_id=10, username="agent_t1_a", email="a_t1_a@bm.com", role="agente", company_id=self.c1.company_id, primary_service_id=self.s1.service_id, primary_team_id=self.t1.team_id, hubspot_owner_id="hs_t1_a", is_active=True, password_hash="x")
            self.agent_t1_b = User(user_id=11, username="agent_t1_b", email="a_t1_b@bm.com", role="agente", company_id=self.c1.company_id, primary_service_id=self.s1.service_id, primary_team_id=self.t1.team_id, hubspot_owner_id="hs_t1_b", is_active=True, password_hash="x")

            # Agent in Team 2 (Service 2)
            self.agent_t2_a = User(user_id=12, username="agent_t2_a", email="a_t2_a@bm.com", role="agente", company_id=self.c1.company_id, primary_service_id=self.s2.service_id, primary_team_id=self.t2.team_id, hubspot_owner_id="hs_t2_a", is_active=True, password_hash="x")

            # 20 Benchmark Agents for Performance Testing
            self.benchmark_agents = []
            for i in range(1, 21):
                u = User(
                    user_id=100 + i,
                    username=f"bench_agent_{i}",
                    email=f"bench_{i}@bm.com",
                    role="agente",
                    company_id=self.c1.company_id,
                    primary_service_id=self.s1.service_id,
                    primary_team_id=self.t1.team_id,
                    hubspot_owner_id=f"hs_bench_{i}",
                    is_active=True,
                    password_hash="x"
                )
                self.benchmark_agents.append(u)

            db.add_all([self.u_admin, self.u_mgr1, self.u_coord1, self.agent_t1_a, self.agent_t1_b, self.agent_t2_a, *self.benchmark_agents])
            await db.flush()

            # Associate coordinator to team 1 via UserTeamAssociation
            db.add(UserTeamAssociation(user_id=self.u_coord1.user_id, team_id=self.t1.team_id))
            # Associate manager to service 1 via UserServiceAssociation
            db.add(UserServiceAssociation(user_id=self.u_mgr1.user_id, service_id=self.s1.service_id))

            # Associate agents
            db.add(UserTeamAssociation(user_id=self.agent_t1_a.user_id, team_id=self.t1.team_id))
            db.add(UserTeamAssociation(user_id=self.agent_t1_b.user_id, team_id=self.t1.team_id))
            db.add(UserTeamAssociation(user_id=self.agent_t2_a.user_id, team_id=self.t2.team_id))
            for ba in self.benchmark_agents:
                db.add(UserTeamAssociation(user_id=ba.user_id, team_id=self.t1.team_id))

            # Settings
            s_t1_a = TrainingAgentSetting(hubspot_owner_id="hs_t1_a", agent_name="Agent T1 A", agent_initials="AA", is_enabled=True, company_id=self.c1.company_id)
            s_t1_b = TrainingAgentSetting(hubspot_owner_id="hs_t1_b", agent_name="Agent T1 B", agent_initials="AB", is_enabled=True, company_id=self.c1.company_id)
            s_t2_a = TrainingAgentSetting(hubspot_owner_id="hs_t2_a", agent_name="Agent T2 A", agent_initials="BA", is_enabled=True, company_id=self.c1.company_id)
            bench_settings = [
                TrainingAgentSetting(hubspot_owner_id=f"hs_bench_{i}", agent_name=f"Bench Agent {i}", agent_initials=f"B{i}", is_enabled=True, company_id=self.c1.company_id)
                for i in range(1, 21)
            ]
            db.add_all([s_t1_a, s_t1_b, s_t2_a, *bench_settings])
            await db.flush()

            # Add sample reports and evaluations
            now = datetime.now(timezone.utc)
            r1 = TrainingAgentReport(
                training_report_id=1,
                hubspot_owner_id="hs_t1_a",
                agent_name="Agent T1 A",
                agent_initials="AA",
                status="completed",
                is_current=True,
                period_start=now - timedelta(days=14),
                period_end=now,
                avg_evaluacion_global=Decimal("8.50")
            )
            r2 = TrainingAgentReport(
                training_report_id=2,
                hubspot_owner_id="hs_t2_a",
                agent_name="Agent T2 A",
                agent_initials="BA",
                status="completed",
                is_current=True,
                period_start=now - timedelta(days=14),
                period_end=now,
                avg_evaluacion_global=Decimal("6.00")
            )
            db.add_all([r1, r2])

            for i in range(1, 21):
                rep = TrainingAgentReport(
                    training_report_id=100 + i,
                    hubspot_owner_id=f"hs_bench_{i}",
                    agent_name=f"Bench Agent {i}",
                    agent_initials=f"B{i}",
                    status="completed",
                    is_current=True,
                    period_start=now - timedelta(days=14),
                    period_end=now,
                    avg_evaluacion_global=Decimal("7.50")
                )
                db.add(rep)
                db.add(TrainingCompletionStatus(completion_id=100 + i, training_report_id=100 + i, simulation_prompt_id=1, hubspot_owner_id=f"hs_bench_{i}", status="completed"))

            await db.commit()

        # Generate tokens
        self.t_admin = create_access_token({"sub": "admin_bm", "user_id": 1, "company_id": 1, "role": "company_admin"})
        self.t_mgr1 = create_access_token({"sub": "mgr_s1", "user_id": 2, "company_id": 1, "role": "responsable_servicio"})
        self.t_coord1 = create_access_token({"sub": "coord_t1", "user_id": 3, "company_id": 1, "role": "coordinador_equipo"})

        transport = ASGITransport(app=app)
        self.client = AsyncClient(transport=transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        if os.path.exists("training_filters_perf_test.db"):
            try:
                os.remove("training_filters_perf_test.db")
            except Exception:
                pass

    async def test_filter_service_id(self):
        """Filtering by service_id scopes results to that service only."""
        # Service 2 has only hs_t2_a
        res = await self.client.get(
            "/bm/training/admin/agents-overview?service_id=2",
            headers={"Authorization": f"Bearer {self.t_admin}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["hubspot_owner_id"], "hs_t2_a")

        # cycles-summary with service_id=2
        res_sum = await self.client.get(
            "/bm/training/admin/cycles-summary?service_id=2",
            headers={"Authorization": f"Bearer {self.t_admin}"}
        )
        self.assertEqual(res_sum.status_code, 200)
        sum_data = res_sum.json()
        self.assertEqual(sum_data["monitored_agents"], 1)

    async def test_filter_team_id(self):
        """Filtering by team_id scopes results to that team only."""
        # Team 2 has hs_t2_a
        res = await self.client.get(
            "/bm/training/admin/agents-overview?team_id=2",
            headers={"Authorization": f"Bearer {self.t_admin}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["hubspot_owner_id"], "hs_t2_a")

    async def test_mismatch_team_service_returns_400(self):
        """Team 2 belongs to Service 2. Requesting service_id=1 and team_id=2 must return 400."""
        res = await self.client.get(
            "/bm/training/admin/agents-overview?service_id=1&team_id=2",
            headers={"Authorization": f"Bearer {self.t_admin}"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("El equipo seleccionado no pertenece al servicio indicado", res.json()["detail"])

        res_sum = await self.client.get(
            "/bm/training/admin/cycles-summary?service_id=1&team_id=2",
            headers={"Authorization": f"Bearer {self.t_admin}"}
        )
        self.assertEqual(res_sum.status_code, 400)
        self.assertIn("El equipo seleccionado no pertenece al servicio indicado", res_sum.json()["detail"])

    async def test_empty_team_returns_zero_results(self):
        """Empty team (team_id=3) must return 0 results and NEVER fallback to all agents."""
        res = await self.client.get(
            "/bm/training/admin/agents-overview?team_id=3",
            headers={"Authorization": f"Bearer {self.t_admin}"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data, [], "Empty team must return 0 items, never fallback to all")

        res_sum = await self.client.get(
            "/bm/training/admin/cycles-summary?team_id=3",
            headers={"Authorization": f"Bearer {self.t_admin}"}
        )
        self.assertEqual(res_sum.status_code, 200)
        sum_data = res_sum.json()
        self.assertEqual(sum_data["monitored_agents"], 0)
        self.assertEqual(sum_data["total_cycles"], 0)

    async def test_role_scoping(self):
        """Coordinators and Managers cannot bypass permissions using filters."""
        # Coordinator for Team 1 cannot filter by Team 2 (403)
        res = await self.client.get(
            "/bm/training/admin/agents-overview?team_id=2",
            headers={"Authorization": f"Bearer {self.t_coord1}"}
        )
        self.assertEqual(res.status_code, 403)

        # Service Manager for Service 1 cannot filter by Service 2 (403)
        res2 = await self.client.get(
            "/bm/training/admin/agents-overview?service_id=2",
            headers={"Authorization": f"Bearer {self.t_mgr1}"}
        )
        self.assertEqual(res2.status_code, 403)

    async def test_query_count_o1_performance(self):
        """Query count must be O(1) and not grow linearly between 2 agents vs 20 agents."""
        engine = get_engine()
        sync_engine = engine.sync_engine

        # Helper to count SQL statements executed during a coroutine
        async def measure_queries(fn):
            queries = []
            def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                queries.append(statement)

            event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
            try:
                await fn()
            finally:
                event.remove(sync_engine, "before_cursor_execute", before_cursor_execute)
            return len(queries)

        async with AsyncSession(self.session_factory) as db:
            # 1. get_agent_overview with 2 agents
            async def run_overview_2():
                await PersonalizedTrainingService.get_agent_overview(
                    db, company_ids=[1], allowed_agent_ids=["hs_t1_a", "hs_t1_b"]
                )
            count_overview_2 = await measure_queries(run_overview_2)

            # 2. get_agent_overview with 20 agents
            bench_20_ids = [f"hs_bench_{i}" for i in range(1, 21)]
            async def run_overview_20():
                await PersonalizedTrainingService.get_agent_overview(
                    db, company_ids=[1], allowed_agent_ids=bench_20_ids
                )
            count_overview_20 = await measure_queries(run_overview_20)

            # Assert O(1) query count for overview (must be identical)
            self.assertEqual(
                count_overview_2, count_overview_20,
                f"get_agent_overview query count grew with N agents: {count_overview_2} vs {count_overview_20}"
            )

            # 3. get_cycles_team_summary with 2 agents
            async def run_summary_2():
                await PersonalizedTrainingService.get_cycles_team_summary(
                    db, company_ids=[1], allowed_agent_ids=["hs_t1_a", "hs_t1_b"]
                )
            count_summary_2 = await measure_queries(run_summary_2)

            # 4. get_cycles_team_summary with 20 agents
            async def run_summary_20():
                await PersonalizedTrainingService.get_cycles_team_summary(
                    db, company_ids=[1], allowed_agent_ids=bench_20_ids
                )
            count_summary_20 = await measure_queries(run_summary_20)

            # Assert O(1) query count for team summary (must be identical)
            self.assertEqual(
                count_summary_2, count_summary_20,
                f"get_cycles_team_summary query count grew with N agents: {count_summary_2} vs {count_summary_20}"
            )


if __name__ == "__main__":
    unittest.main()
