"""
Test suite verifying team_coordinator permissions in mass automations and agent training tracking.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

# Force local sqlite test database
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///team_coordinator_permissions_test.db"

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
from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion
from app.utils.security import create_access_token
from app.main import app
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class TestTeamCoordinatorPermissions(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("team_coordinator_permissions_test.db"):
            try:
                os.remove("team_coordinator_permissions_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine) as db:
            # 1. Company
            c1 = Company(company_id=1, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            db.add(c1)
            await db.flush()

            # 2. Services: Service 1 (Front) & Service 2 (Dental)
            s1 = Service(service_id=1, service_name="Front Desk", service_key="front", company_id=1)
            s2 = Service(service_id=2, service_name="Dental Care", service_key="dental", company_id=1)
            db.add_all([s1, s2])
            await db.flush()

            # 3. Teams: Team A (id=10) & Team B (id=20)
            t_a = Team(team_id=10, team_name="Equipo A", service_id=1, company_id=1, is_active=True)
            t_b = Team(team_id=20, team_name="Equipo B", service_id=1, company_id=1, is_active=True)
            db.add_all([t_a, t_b])
            await db.flush()

            # 4. Users:
            # - Super Admin
            # - Company Admin
            # - Team Coordinator (Team A)
            # - Agent A (Team A)
            # - Agent B (Team B)
            u_super = User(user_id=1, username="super_admin", email="super@test.com", role="admin", password_hash="dummy")
            u_comp_admin = User(user_id=2, username="comp_admin", email="comp_admin@test.com", role="company_admin", company_id=1, password_hash="dummy")
            u_coord = User(user_id=3, username="coord_a", email="coord_a@test.com", role="coordinador_equipo", company_id=1, primary_team_id=10, primary_service_id=1, password_hash="dummy")
            u_agent_a = User(user_id=101, username="agent_a", email="agent_a@test.com", role="agente", company_id=1, primary_team_id=10, hubspot_owner_id="owner_A", password_hash="dummy")
            u_agent_b = User(user_id=102, username="agent_b", email="agent_b@test.com", role="agente", company_id=1, primary_team_id=20, hubspot_owner_id="owner_B", password_hash="dummy")
            
            db.add_all([u_super, u_comp_admin, u_coord, u_agent_a, u_agent_b])
            await db.flush()

            # Team associations
            assoc_coord = UserTeamAssociation(user_id=3, team_id=10)
            assoc_agent_a = AgentTeamAssociation(user_id=101, team_id=10)
            assoc_agent_b = AgentTeamAssociation(user_id=102, team_id=20)
            db.add_all([assoc_coord, assoc_agent_a, assoc_agent_b])
            await db.flush()

            # 5. Prompt for Service 1
            p1 = Prompt(prompt_id=1, prompt_name="Prompt Front V1", prompt_type="audio", service_id=1, company_id=1, is_active=True)
            v1 = PromptVersion(id=1, prompt_id=1, prompt="Test prompt content", version_label="v1", is_current=True)
            db.add_all([p1, v1])

            await db.commit()

        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_coord = create_access_token({"user_id": 3, "email": "coord_a@test.com"})
        self.t_agent = create_access_token({"user_id": 101, "email": "agent_a@test.com"})

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        if os.path.exists("team_coordinator_permissions_test.db"):
            try:
                os.remove("team_coordinator_permissions_test.db")
            except Exception:
                pass

    async def test_tenant_context_response_for_team_coordinator(self):
        """GET /bm/me/tenant-context for team coordinator returns correct flags and scoped IDs."""
        res = await self.client.get(
            "/bm/me/tenant-context",
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["normalized_role"], "team_coordinator")
        self.assertTrue(data["can_manage_users"])
        self.assertTrue(data["can_manage_teams"])
        self.assertTrue(data["can_manage_training"])
        self.assertTrue(data["can_manage_trainer"])
        self.assertTrue(data["can_manage_structures"])
        self.assertIn(10, data["allowed_team_ids"])
        self.assertNotIn(20, data["allowed_team_ids"])

    async def test_team_coordinator_can_list_and_create_automations(self):
        """Team Coordinator can list and create mass automations for allowed service_id=1."""
        # 1. GET automations as coordinator -> 200 OK
        res = await self.client.get(
            "/bm/mass-analysis/automations",
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res.status_code, 200, res.text)

        # 2. GET automations as agent -> 403 Forbidden
        res_agent = await self.client.get(
            "/bm/mass-analysis/automations",
            headers={"Authorization": f"Bearer {self.t_agent}"}
        )
        self.assertEqual(res_agent.status_code, 403)

        # 3. POST automation in service_id=1 (allowed) -> 201 Created
        res_create = await self.client.post(
            "/bm/mass-analysis/automations",
            json={
                "name": "Automatización Equipo A",
                "service_id": 1,
                "prompt_id": 1,
                "interval_minutes": 60,
                "lookback_minutes": 60,
                "agent_owner_ids": ["owner_A"]
            },
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res_create.status_code, 201, res_create.text)

        # 4. POST automation with agent_owner_ids=["owner_B"] (out of team scope) -> 403 Forbidden
        res_create_err = await self.client.post(
            "/bm/mass-analysis/automations",
            json={
                "name": "Automatización Invalida",
                "service_id": 1,
                "prompt_id": 1,
                "interval_minutes": 60,
                "lookback_minutes": 60,
                "agent_owner_ids": ["owner_B"]
            },
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res_create_err.status_code, 403)

        # 5. POST automation in service_id=2 (unassigned service) -> 403 Forbidden
        res_svc_err = await self.client.post(
            "/bm/mass-analysis/automations",
            json={
                "name": "Automatización Servicio 2",
                "service_id": 2,
                "prompt_id": 1,
                "interval_minutes": 60,
                "lookback_minutes": 60
            },
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res_svc_err.status_code, 403)

    @patch("app.services.personalized_training_service.PersonalizedTrainingService.approve_training_cycle")
    async def test_team_coordinator_agent_tracking_and_manual_cycles(self, mock_approve):
        """Team Coordinator can view overview/settings and create manual cycles for team agents."""
        async def fake_approve(db, report_id, approved_by_user_id=1):
            from app.models.personalized_training import TrainingAgentReport
            stmt = select(TrainingAgentReport).where(TrainingAgentReport.training_report_id == report_id)
            res = await db.execute(stmt)
            rep = res.scalars().first()
            if rep:
                rep.status = "in_progress"
            return rep

        mock_approve.side_effect = fake_approve
        # 1. GET /bm/training/admin/settings as coordinator -> 200 OK
        res = await self.client.get(
            "/bm/training/admin/settings",
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res.status_code, 200, res.text)

        # 2. GET /bm/training/admin/agents-overview as coordinator -> 200 OK
        res_ov = await self.client.get(
            "/bm/training/admin/agents-overview",
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res_ov.status_code, 200, res_ov.text)

        # 3. GET /bm/training/admin/cycles-summary as coordinator -> 200 OK
        res_sum = await self.client.get(
            "/bm/training/admin/cycles-summary",
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res_sum.status_code, 200, res_sum.text)

        # 4. POST /bm/training/admin/manual-cycle for owner_A (Team A) -> 200 OK
        res_manual = await self.client.post(
            "/bm/training/admin/manual-cycle",
            json={
                "hubspot_owner_ids": ["owner_A"],
                "title": "Ciclo Manual Equipo A",
                "objectives": ["Objetivo 1"]
            },
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res_manual.status_code, 200, res_manual.text)

        # 5. POST /bm/training/admin/manual-cycle for owner_B (Team B, out of scope) -> 403 Forbidden
        res_manual_err = await self.client.post(
            "/bm/training/admin/manual-cycle",
            json={
                "hubspot_owner_ids": ["owner_B"],
                "title": "Ciclo Manual Equipo B",
                "objectives": ["Objetivo 1"]
            },
            headers={"Authorization": f"Bearer {self.t_coord}"}
        )
        self.assertEqual(res_manual_err.status_code, 403)


if __name__ == "__main__":
    asyncio.run(unittest.main())
