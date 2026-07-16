"""
Test Suite: Admin Teams Hierarchical Endpoints
Tests: GET/POST/PATCH /bm/admin/teams and member management endpoints.
Uses SQLite in-memory with JSONB compatibility shim.
"""
import os
import sys
import unittest
from datetime import datetime

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///admin_teams_test.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.services import Service
from app.dependencies import get_current_user, get_db
from app.main import app


class TestAdminTeams(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Engine URL points to production!"

        if os.path.exists("admin_teams_test.db"):
            try:
                os.remove("admin_teams_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(engine) as db:
            # Companies
            self.c1 = Company(company_name="Boston Medical", company_key="bm", is_active=True)
            self.c2 = Company(company_name="GesDent", company_key="gesdent", is_active=True)
            db.add_all([self.c1, self.c2])
            await db.flush()
            self.company1_id = self.c1.company_id
            self.company2_id = self.c2.company_id

            # Services
            self.s1 = Service(service_name="Front", service_key="front", company_id=self.company1_id)
            self.s2 = Service(service_name="Urgencias", service_key="urgencias", company_id=self.company2_id)
            db.add_all([self.s1, self.s2])
            await db.flush()
            self.service1_id = self.s1.service_id
            self.service2_id = self.s2.service_id

            # Teams
            self.t1 = Team(team_name="Equipo Manana", company_id=self.company1_id, service_id=self.service1_id, is_active=True)
            self.t2 = Team(team_name="Equipo Tarde", company_id=self.company1_id, service_id=self.service1_id, is_active=True)
            self.t3 = Team(team_name="Equipo Gesdent", company_id=self.company2_id, service_id=self.service2_id, is_active=True)
            db.add_all([self.t1, self.t2, self.t3])
            await db.flush()
            self.team1_id = self.t1.team_id
            self.team2_id = self.t2.team_id
            self.team3_id = self.t3.team_id

            # Users
            self.u_super = User(username="super", email="super@test.com", role="administrador", password_hash="x")
            self.u_comp = User(username="comp_admin", email="comp@test.com", role="company_admin", company_id=self.company1_id, password_hash="x")
            self.u_mgr = User(username="svc_mgr", email="mgr@test.com", role="responsable_servicio", company_id=self.company1_id, password_hash="x")
            self.u_coor = User(username="team_coor", email="coor@test.com", role="coordinador_equipo", company_id=self.company1_id, password_hash="x")
            self.u_agent = User(username="agente1", email="agent1@test.com", role="agente", company_id=self.company1_id, hubspot_owner_id="h1", password_hash="x")
            self.u_agent2 = User(username="agente2", email="agent2@test.com", role="agente", company_id=self.company2_id, hubspot_owner_id="h2", password_hash="x")
            db.add_all([self.u_super, self.u_comp, self.u_mgr, self.u_coor, self.u_agent, self.u_agent2])
            await db.flush()
            self.super_id = self.u_super.user_id
            self.comp_id = self.u_comp.user_id
            self.mgr_id = self.u_mgr.user_id
            self.coor_id = self.u_coor.user_id
            self.agent_id = self.u_agent.user_id
            self.agent2_id = self.u_agent2.user_id

            # Assign svc_mgr to service1
            from app.models.teams import UserServiceAssociation
            db.add(UserServiceAssociation(user_id=self.mgr_id, service_id=self.service1_id))
            # Assign team_coor to team1
            db.add(UserTeamAssociation(user_id=self.coor_id, team_id=self.team1_id))
            await db.commit()

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ── TEST 1: super_admin lists all teams ──────────────────────────────────

    async def test_1_super_admin_lists_all_teams(self):
        """super_admin sees all teams from all companies."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.super_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/admin/teams")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(len(data), 3, f"Expected 3 teams, got {len(data)}: {data}")
            # Verify enriched response fields
            for team in data:
                self.assertIn("company_name", team)
                self.assertIn("service_name", team)
                self.assertIn("agent_count", team)
                self.assertIn("coordinator_count", team)
                self.assertIn("is_active", team)
                self.assertNotIn("password_hash", team)

    # ── TEST 2: company_admin lists only their company's teams ───────────────

    async def test_2_company_admin_lists_own_company(self):
        """company_admin sees only teams of their company."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/admin/teams")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            # Only 2 teams for Boston Medical
            self.assertEqual(len(data), 2)
            for team in data:
                self.assertEqual(team["company_id"], self.company1_id)

            # Cross-company filter returns 403
            res_bad = await ac.get(f"/bm/admin/teams?company_id={self.company2_id}")
            self.assertEqual(res_bad.status_code, 403)

    # ── TEST 3: service_manager lists only their service's teams ─────────────

    async def test_3_service_manager_lists_own_service_teams(self):
        """service_manager sees only teams in their allowed services."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.mgr_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/admin/teams")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            # svc_mgr is assigned to service1 which has team1 and team2
            self.assertEqual(len(data), 2)
            for team in data:
                self.assertEqual(team["service_id"], self.service1_id)

    # ── TEST 4: team_coordinator lists only their assigned teams ─────────────

    async def test_4_team_coordinator_lists_own_teams(self):
        """team_coordinator sees only teams they are assigned to."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.coor_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/admin/teams")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            # Coordinator is assigned only to team1
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["team_id"], self.team1_id)

    # ── TEST 5: agent cannot list teams ──────────────────────────────────────

    async def test_5_agent_cannot_list_teams(self):
        """Agents get 403 when trying to access admin teams list."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.agent_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.get("/bm/admin/teams")
            self.assertEqual(res.status_code, 403)

    # ── TEST 6: create team in same service/company → 201 ────────────────────

    async def test_6_create_team_same_company(self):
        """company_admin can create team in their own company's service."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            payload = {"team_name": "Equipo Nuevo", "company_id": self.company1_id, "service_id": self.service1_id}
            res = await ac.post("/bm/admin/teams", json=payload)
            self.assertEqual(res.status_code, 201)
            data = res.json()
            self.assertEqual(data["team_name"], "Equipo Nuevo")
            self.assertEqual(data["company_id"], self.company1_id)
            self.assertEqual(data["service_id"], self.service1_id)
            self.assertTrue(data["is_active"])
            self.assertIn("company_name", data)
            self.assertIn("service_name", data)

    # ── TEST 7: create team cross-company → 403 ───────────────────────────────

    async def test_7_create_team_cross_company_forbidden(self):
        """company_admin cannot create team in another company."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            payload = {"team_name": "Equipo Infiltrado", "company_id": self.company2_id, "service_id": self.service2_id}
            res = await ac.post("/bm/admin/teams", json=payload)
            self.assertEqual(res.status_code, 403)

            # Also test wrong service (from another company)
            payload2 = {"team_name": "Equipo Mala Svc", "company_id": self.company1_id, "service_id": self.service2_id}
            res2 = await ac.post("/bm/admin/teams", json=payload2)
            self.assertEqual(res2.status_code, 400)  # service not in company

    # ── TEST 8: update team (name and is_active) ──────────────────────────────

    async def test_8_update_team(self):
        """company_admin can update team name and is_active; cross-company edit blocked."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Update name
            res = await ac.patch(f"/bm/admin/teams/{self.team1_id}", json={"team_name": "Equipo Renombrado"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["team_name"], "Equipo Renombrado")

            # Deactivate
            res2 = await ac.patch(f"/bm/admin/teams/{self.team1_id}", json={"is_active": False})
            self.assertEqual(res2.status_code, 200)
            self.assertFalse(res2.json()["is_active"])

            # Cross-company edit
            res_bad = await ac.patch(f"/bm/admin/teams/{self.team3_id}", json={"team_name": "Hackeo"})
            self.assertEqual(res_bad.status_code, 403)

    # ── TEST 9: add agent to team (same company) → 201 ───────────────────────

    async def test_9_add_agent_same_company(self):
        """company_admin can add agent to team within same company."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post(f"/bm/admin/teams/{self.team1_id}/agents/{self.agent_id}")
            self.assertEqual(res.status_code, 201)

            # Idempotent — second call OK
            res2 = await ac.post(f"/bm/admin/teams/{self.team1_id}/agents/{self.agent_id}")
            self.assertEqual(res2.status_code, 201)

            # Verify in list
            res_list = await ac.get(f"/bm/admin/teams/{self.team1_id}/agents")
            self.assertEqual(res_list.status_code, 200)
            agent_ids = [a["user_id"] for a in res_list.json()]
            self.assertIn(self.agent_id, agent_ids)

    # ── TEST 10: add agent cross-company → 403 ───────────────────────────────

    async def test_10_add_agent_cross_company_forbidden(self):
        """Cannot add agent from another company to a team."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u = await db.get(User, self.comp_id)
        app.dependency_overrides[get_current_user] = lambda: u
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # agent2 is from company2 → adding to company1 team → 403
            res = await ac.post(f"/bm/admin/teams/{self.team1_id}/agents/{self.agent2_id}")
            self.assertEqual(res.status_code, 403)

    # ── TEST 11: add coordinator to team → 201; cross-company → 403 ──────────

    async def test_11_add_coordinator_same_company(self):
        """super_admin can add coordinator; cross-company blocked."""
        engine = get_engine()
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)
            u_comp = await db.get(User, self.comp_id)

        # Add coordinator to team1 (same company)
        app.dependency_overrides[get_current_user] = lambda: u_super
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post(f"/bm/admin/teams/{self.team1_id}/coordinators/{self.coor_id}")
            self.assertEqual(res.status_code, 201)

            # Verify
            res_list = await ac.get(f"/bm/admin/teams/{self.team1_id}/coordinators")
            self.assertEqual(res_list.status_code, 200)
            coord_ids = [c["user_id"] for c in res_list.json()]
            self.assertIn(self.coor_id, coord_ids)

        # Cross-company coordinator → 403
        app.dependency_overrides[get_current_user] = lambda: u_comp
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            res_bad = await ac.post(f"/bm/admin/teams/{self.team3_id}/coordinators/{self.coor_id}")
            self.assertEqual(res_bad.status_code, 403)

    # ── TEST 12: remove agent and coordinator ────────────────────────────────

    async def test_12_remove_agent_and_coordinator(self):
        """Removing agent and coordinator works correctly."""
        engine = get_engine()
        # First: pre-assign in one session
        async with AsyncSession(engine) as db:
            db.add(AgentTeamAssociation(user_id=self.agent_id, team_id=self.team1_id))
            db.add(UserTeamAssociation(user_id=self.mgr_id, team_id=self.team1_id))
            await db.commit()
        # Then: load user in a fresh session so it is not expired
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)

        app.dependency_overrides[get_current_user] = lambda: u_super
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Remove agent
            res_del_agent = await ac.delete(f"/bm/admin/teams/{self.team1_id}/agents/{self.agent_id}")
            self.assertEqual(res_del_agent.status_code, 200)

            # Verify agent gone
            res_agents = await ac.get(f"/bm/admin/teams/{self.team1_id}/agents")
            agent_ids = [a["user_id"] for a in res_agents.json()]
            self.assertNotIn(self.agent_id, agent_ids)

            # Remove coordinator (mgr assigned above)
            res_del_coord = await ac.delete(f"/bm/admin/teams/{self.team1_id}/coordinators/{self.mgr_id}")
            self.assertEqual(res_del_coord.status_code, 200)

            # Idempotent delete (already removed)
            res_del_again = await ac.delete(f"/bm/admin/teams/{self.team1_id}/agents/{self.agent_id}")
            self.assertEqual(res_del_again.status_code, 200)

    # ── TEST 13: response does not expose sensitive data ─────────────────────

    async def test_13_response_no_sensitive_fields(self):
        """API responses never expose password_hash or other sensitive fields."""
        engine = get_engine()
        # First: write in one session
        async with AsyncSession(engine) as db:
            db.add(AgentTeamAssociation(user_id=self.agent_id, team_id=self.team1_id))
            await db.commit()
        # Then: load user in a fresh session
        async with AsyncSession(engine) as db:
            u_super = await db.get(User, self.super_id)

        app.dependency_overrides[get_current_user] = lambda: u_super
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Team list
            res = await ac.get("/bm/admin/teams")
            for t in res.json():
                self.assertNotIn("password_hash", t)

            # Agent list
            res_agents = await ac.get(f"/bm/admin/teams/{self.team1_id}/agents")
            for a in res_agents.json():
                self.assertNotIn("password_hash", a)


if __name__ == "__main__":
    unittest.main()
