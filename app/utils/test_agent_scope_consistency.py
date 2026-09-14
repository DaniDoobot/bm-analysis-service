"""
Unit and integration test suite for Agent Scope Consistency.
============================================================
Validates:
A) test_agents_comparison_name_resolution_from_bm_users
B) test_available_agents_service_isolation
C) test_available_agents_team_isolation
D) test_selected_agent_without_data_is_preserved
E) test_selected_agent_with_data
F) test_comparison_selected_and_with_data_counts
G) test_explicit_agent_cannot_escape_scope
H) test_empty_team_does_not_fallback
I) test_agent_role_self_scope_preserved
J) test_agents_endpoint_and_filter_options_same_scope
"""
import os
import sys
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///agent_scope_consistency_test.db"

db_url = os.environ.get("DATABASE_URL", "")
if "91.98.230.119" in db_url or "n8n" in db_url.lower():
    raise RuntimeError("CRITICAL: Test execution blocked because DATABASE_URL points to production!")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.main import app
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.mass_evaluations import MassEvaluationResult, MassEvaluationCriterionResult
from app.utils.security import create_access_token


class TestAgentScopeConsistency(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        if os.path.exists("agent_scope_consistency_test.db"):
            try:
                os.remove("agent_scope_consistency_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston_medical", is_active=True)
            db.add(c1)
            await db.flush()

            # Services
            s1 = Service(service_id=1, service_name="Front", service_key="front", company_id=1)
            s2 = Service(service_id=2, service_name="Experiencia de Paciente", service_key="expac", company_id=1)
            db.add_all([s1, s2])
            await db.flush()

            # Teams
            t_a = Team(team_id=1, team_name="Team Front A", service_id=1, company_id=1)
            t_b = Team(team_id=2, team_name="Team Expac B", service_id=2, company_id=1)
            t_empty = Team(team_id=3, team_name="Team Empty", service_id=2, company_id=1)
            db.add_all([t_a, t_b, t_empty])
            await db.flush()

            # SuperAdmin user
            u_admin = User(
                user_id=1,
                username="admin",
                email="admin@test.com",
                role="superadmin",
                company_id=1,
                is_active=True,
                password_hash="dummy"
            )
            db.add(u_admin)

            # Service 1 / Team A Agents
            u_a1 = User(
                user_id=10, username="agent_a1", email="a1@test.com", name="Agent A1",
                role="agent", company_id=1, primary_service_id=1, primary_team_id=1,
                hubspot_owner_id="owner_a1", agent_initials="A1", is_active=True, password_hash="dummy"
            )
            u_a2 = User(
                user_id=11, username="agent_a2", email="a2@test.com", name="Agent A2",
                role="agent", company_id=1, primary_service_id=1, primary_team_id=1,
                hubspot_owner_id="owner_a2", agent_initials="A2", is_active=True, password_hash="dummy"
            )

            # Service 2 / Team B Agents:
            # - Victoria Arellano: 31499194 (has evaluations)
            # - Flavio: owner_fl (has evaluations)
            # - Ludmila: owner_lud (assigned, NO evaluations in period)
            u_va = User(
                user_id=20, username="victoria", email="va@test.com", name="Victoria Arellano",
                role="agent", company_id=1, primary_service_id=2, primary_team_id=2,
                hubspot_owner_id="31499194", agent_initials="VA", is_active=True, password_hash="dummy"
            )
            u_fl = User(
                user_id=21, username="flavio", email="fl@test.com", name="Flavio Lucich",
                role="agent", company_id=1, primary_service_id=2, primary_team_id=2,
                hubspot_owner_id="owner_fl", agent_initials="FL", is_active=True, password_hash="dummy"
            )
            u_lud = User(
                user_id=22, username="ludmila", email="ludmila@test.com", name="Ludmila Gomez",
                role="agent", company_id=1, primary_service_id=2, primary_team_id=2,
                hubspot_owner_id="owner_lud", agent_initials="LG", is_active=True, password_hash="dummy"
            )

            db.add_all([u_a1, u_a2, u_va, u_fl, u_lud])
            await db.flush()

            # Associations
            assoc_a1 = UserTeamAssociation(user_id=10, team_id=1)
            assoc_a2 = UserTeamAssociation(user_id=11, team_id=1)
            assoc_va = UserTeamAssociation(user_id=20, team_id=2)
            assoc_fl = UserTeamAssociation(user_id=21, team_id=2)
            assoc_lud = UserTeamAssociation(user_id=22, team_id=2)
            db.add_all([assoc_a1, assoc_a2, assoc_va, assoc_fl, assoc_lud])
            await db.flush()

            # Mass Evaluation Results:
            now = datetime.now(timezone.utc)
            # Service 1 evaluations for A1 and A2
            db.add(MassEvaluationResult(
                mass_analysis_id=101, run_id=1, job_id=1, call_id=1001, company_id=1, service_id=1, service_key="front",
                prompt_id=1, prompt_snapshot="{}",
                hubspot_owner_id="owner_a1", agent_name="Agent A1", evaluacion_global=8.5,
                status="completed", analysis_timestamp=now, call_timestamp=now,
                result_json={}, items_json=[]
            ))
            db.add(MassEvaluationResult(
                mass_analysis_id=102, run_id=1, job_id=1, call_id=1002, company_id=1, service_id=1, service_key="front",
                prompt_id=1, prompt_snapshot="{}",
                hubspot_owner_id="owner_a2", agent_name="Agent A2", evaluacion_global=7.0,
                status="completed", analysis_timestamp=now, call_timestamp=now,
                result_json={}, items_json=[]
            ))
            # Historical unassigned agent: has completed evaluations in Service 1, but is NOT assigned to Service 1
            db.add(MassEvaluationResult(
                mass_analysis_id=103, run_id=1, job_id=1, call_id=1003, company_id=1, service_id=1, service_key="front",
                prompt_id=1, prompt_snapshot="{}",
                hubspot_owner_id="owner_hist_s1", agent_name="Historical Agent S1", evaluacion_global=6.5,
                status="completed", analysis_timestamp=now, call_timestamp=now,
                result_json={}, items_json=[]
            ))

            # Service 2 evaluations:
            # Victoria: agent_name stored in DB was an old placeholder "Agente no identificado (31499194)"
            db.add(MassEvaluationResult(
                mass_analysis_id=201, run_id=1, job_id=1, call_id=2001, company_id=1, service_id=2, service_key="expac",
                prompt_id=1, prompt_snapshot="{}",
                hubspot_owner_id="31499194", agent_name="Agente no identificado (31499194)", evaluacion_global=9.0,
                status="completed", analysis_timestamp=now, call_timestamp=now,
                result_json={}, items_json=[]
            ))
            # Flavio:
            db.add(MassEvaluationResult(
                mass_analysis_id=202, run_id=1, job_id=1, call_id=2002, company_id=1, service_id=2, service_key="expac",
                prompt_id=1, prompt_snapshot="{}",
                hubspot_owner_id="owner_fl", agent_name="Flavio Lucich", evaluacion_global=8.0,
                status="completed", analysis_timestamp=now, call_timestamp=now,
                result_json={}, items_json=[]
            ))
            # Ludmila has NO evaluations.

            await db.commit()

        self.token_admin = create_access_token({"sub": "admin", "role": "superadmin", "company_id": 1, "user_id": 1})
        self.token_agent_va = create_access_token({"sub": "victoria", "role": "agent", "company_id": 1, "user_id": 20})

    async def asyncTearDown(self):
        if hasattr(self, "engine"):
            await self.engine.dispose()
        if os.path.exists("agent_scope_consistency_test.db"):
            try:
                os.remove("agent_scope_consistency_test.db")
            except Exception:
                pass

    async def test_agents_comparison_name_resolution_from_bm_users(self):
        """A) Victoria 31499194 resolves canonically to bm_users name 'Victoria Arellano', never placeholder."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=2",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            va_agents = [a for a in data["agents"] if a["hubspot_owner_id"] == "31499194"]
            self.assertEqual(len(va_agents), 1)
            self.assertEqual(va_agents[0]["agent_name"], "Victoria Arellano")
            self.assertFalse(va_agents[0]["agent_name"].startswith("Agente no identificado"))

            va_rows = [r for r in data["comparison"] if r["hubspot_owner_id"] == "31499194"]
            self.assertTrue(len(va_rows) > 0)
            for r in va_rows:
                self.assertEqual(r["agent_name"], "Victoria Arellano")

    async def test_available_agents_service_isolation(self):
        """B) Service 2 available agents never returns agents exclusive to Service 1."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/filter-options?service_id=2",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            agent_ids = [a["hubspot_owner_id"] for a in data.get("agents", [])]
            self.assertIn("31499194", agent_ids)
            self.assertIn("owner_fl", agent_ids)
            self.assertIn("owner_lud", agent_ids)
            # Service 1 agents must NOT appear
            self.assertNotIn("owner_a1", agent_ids)
            self.assertNotIn("owner_a2", agent_ids)

    async def test_available_agents_team_isolation(self):
        """C) Team B available agents only returns Team B agents."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/filter-options?service_id=2&team_id=2",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            agent_ids = [a["hubspot_owner_id"] for a in data.get("agents", [])]
            self.assertIn("31499194", agent_ids)
            self.assertIn("owner_fl", agent_ids)
            self.assertIn("owner_lud", agent_ids)
            self.assertNotIn("owner_a1", agent_ids)
            self.assertNotIn("owner_a2", agent_ids)

    async def test_selected_agent_without_data_is_preserved(self):
        """D) Explicitly requested agent with 0 calls is preserved with has_data=False, count=0, score=None."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=2&agent_owner_ids=owner_lud",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data["agents"]), 1)
            lud_agent = data["agents"][0]
            self.assertEqual(lud_agent["hubspot_owner_id"], "owner_lud")
            self.assertEqual(lud_agent["has_data"], False)
            self.assertEqual(lud_agent["analysis_count"], 0)

            # Rows for Ludmila must have value=None and count=0 (NEVER 0.0)
            lud_rows = data["comparison"]
            self.assertTrue(len(lud_rows) > 0)
            for row in lud_rows:
                self.assertIsNone(row["value"])
                self.assertEqual(row["count"], 0)

    async def test_selected_agent_with_data(self):
        """E) Agent with data has has_data=True, analysis_count > 0, and non-null values."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=2&agent_owner_ids=31499194",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data["agents"]), 1)
            va_agent = data["agents"][0]
            self.assertEqual(va_agent["hubspot_owner_id"], "31499194")
            self.assertEqual(va_agent["has_data"], True)
            self.assertEqual(va_agent["analysis_count"], 1)

            va_rows = [r for r in data["comparison"] if r["item_key"] == "evaluacion_global"]
            self.assertEqual(len(va_rows), 1)
            self.assertEqual(va_rows[0]["value"], 9.0)
            self.assertEqual(va_rows[0]["count"], 1)

    async def test_comparison_selected_and_with_data_counts(self):
        """F) 3 agents requested (2 with data, 1 without): selected_agents_count=3, agents_with_data_count=2."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=2&agent_owner_ids=31499194&agent_owner_ids=owner_fl&agent_owner_ids=owner_lud",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["selected_agents_count"], 3)
            self.assertEqual(data["agents_with_data_count"], 2)
            self.assertEqual(len(data["agents"]), 3)

            # Check agent flags
            agents_map = {a["hubspot_owner_id"]: a for a in data["agents"]}
            self.assertTrue(agents_map["31499194"]["has_data"])
            self.assertTrue(agents_map["owner_fl"]["has_data"])
            self.assertFalse(agents_map["owner_lud"]["has_data"])

    async def test_explicit_agent_cannot_escape_scope(self):
        """G) Passing an agent from another service/team is rejected/excluded from comparison."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Query Service 2 while asking for Service 1 agent owner_a1
            res = await client.get(
                "/bm/analytics/agents-comparison?service_id=2&agent_owner_ids=owner_a1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data["agents"]), 0)
            self.assertEqual(len(data["comparison"]), 0)
            self.assertEqual(data["selected_agents_count"], 0)
            self.assertEqual(data["agents_with_data_count"], 0)

    async def test_empty_team_does_not_fallback(self):
        """H) Empty team returns zero agents and zero results, NEVER falling back to all agents."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/filter-options?service_id=2&team_id=3",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data.get("agents", [])), 0)

            res_comp = await client.get(
                "/bm/analytics/agents-comparison?service_id=2&team_id=3",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_comp.status_code, 200)
            comp_data = res_comp.json()
            self.assertEqual(len(comp_data["agents"]), 0)
            self.assertEqual(len(comp_data["comparison"]), 0)

    async def test_agent_role_self_scope_preserved(self):
        """I) AGENT role accessing filter-options only sees their own owner ID."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get(
                "/bm/analytics/filter-options",
                headers={"Authorization": f"Bearer {self.token_agent_va}"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            agent_ids = [a["hubspot_owner_id"] for a in data.get("agents", [])]
            self.assertEqual(agent_ids, ["31499194"])

    async def test_agents_endpoint_and_filter_options_same_scope(self):
        """J) /bm/agents and /bm/analytics/filter-options return the same available agent universe."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res_agents = await client.get(
                "/bm/agents?service_id=2",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_agents.status_code, 200)
            agents_list = res_agents.json()
            agents_ids = sorted([a["hubspot_owner_id"] for a in agents_list])

            res_opts = await client.get(
                "/bm/analytics/filter-options?service_id=2",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_opts.status_code, 200)
            opts_list = res_opts.json().get("agents", [])
            opts_ids = sorted([a["hubspot_owner_id"] for a in opts_list])

            self.assertEqual(agents_ids, opts_ids)
            self.assertEqual(agents_ids, ["31499194", "owner_fl", "owner_lud"])

    async def test_service_historical_unassigned_agent_excluded(self):
        """K) Historical unassigned agent with evaluations in Service 1 is EXCLUDED from current scope."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. /bm/agents?service_id=1
            res_agents = await client.get(
                "/bm/agents?service_id=1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_agents.status_code, 200)
            agents_list = res_agents.json()
            agent_ids = [a["hubspot_owner_id"] for a in agents_list]
            self.assertIn("owner_a1", agent_ids)
            self.assertIn("owner_a2", agent_ids)
            self.assertNotIn("owner_hist_s1", agent_ids)

            # 2. /bm/analytics/filter-options?service_id=1
            res_opts = await client.get(
                "/bm/analytics/filter-options?service_id=1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_opts.status_code, 200)
            opts_list = res_opts.json().get("agents", [])
            opt_ids = [a["hubspot_owner_id"] for a in opts_list]
            self.assertIn("owner_a1", opt_ids)
            self.assertIn("owner_a2", opt_ids)
            self.assertNotIn("owner_hist_s1", opt_ids)

            # 3. Explicit request in agents-comparison must exclude Historical A from target scope
            res_comp = await client.get(
                "/bm/analytics/agents-comparison?service_id=1&agent_owner_ids=owner_hist_s1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_comp.status_code, 200)
            comp_data = res_comp.json()
            comp_agent_ids = [a["hubspot_owner_id"] for a in comp_data.get("agents", [])]
            self.assertNotIn("owner_hist_s1", comp_agent_ids)
            self.assertEqual(comp_data.get("selected_agents_count"), 0)

            # 4. Historical evaluations remain intact in DB
            from sqlalchemy import select
            async with AsyncSession(self.engine) as db:
                stmt = select(MassEvaluationResult).where(MassEvaluationResult.hubspot_owner_id == "owner_hist_s1")
                res_db = await db.execute(stmt)
                db_record = res_db.scalars().first()
                self.assertIsNotNone(db_record)
                self.assertEqual(db_record.mass_analysis_id, 103)
                self.assertEqual(db_record.service_id, 1)

    async def test_team_historical_unassigned_agent_excluded(self):
        """L) Historical agent with evaluations in service does NOT appear when selecting a team they don't belong to."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. /bm/agents?service_id=1&team_id=1
            res_agents = await client.get(
                "/bm/agents?service_id=1&team_id=1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_agents.status_code, 200)
            agent_ids = [a["hubspot_owner_id"] for a in res_agents.json()]
            self.assertIn("owner_a1", agent_ids)
            self.assertIn("owner_a2", agent_ids)
            self.assertNotIn("owner_hist_s1", agent_ids)

            # 2. /bm/analytics/filter-options?service_id=1&team_id=1
            res_opts = await client.get(
                "/bm/analytics/filter-options?service_id=1&team_id=1",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_opts.status_code, 200)
            opt_ids = [a["hubspot_owner_id"] for a in res_opts.json().get("agents", [])]
            self.assertIn("owner_a1", opt_ids)
            self.assertIn("owner_a2", opt_ids)
            self.assertNotIn("owner_hist_s1", opt_ids)

            # 3. Selecting empty team (team_id=3) in Service 2 does not pull in any agents with historical evaluations
            res_opts_empty = await client.get(
                "/bm/analytics/filter-options?service_id=2&team_id=3",
                headers={"Authorization": f"Bearer {self.token_admin}"}
            )
            self.assertEqual(res_opts_empty.status_code, 200)
            self.assertEqual(len(res_opts_empty.json().get("agents", [])), 0)


if __name__ == "__main__":
    unittest.main()
