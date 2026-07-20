import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from httpx import AsyncClient, ASGITransport

# Force DATABASE_URL to a safe local SQLite DB before any app modules are loaded
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///multitenancy_trainer_test.db"

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
from app.models.prompts import Prompt
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSession,
    TrainerEvaluation,
)
from app.utils.security import create_access_token
from app.main import app

from sqlalchemy.ext.asyncio import AsyncSession


class TestMultitenancyTrainer(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        # Clean old DB file if exists
        if os.path.exists("multitenancy_trainer_test.db"):
            try:
                os.remove("multitenancy_trainer_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
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
            # Coor 1: Assigned to Team 1
            db.add(UserTeamAssociation(user_id=self.u_coor1.user_id, team_id=self.t1.team_id))
            # Agent Boston 1 in Team 1
            db.add(AgentTeamAssociation(user_id=self.u_agent_boston1.user_id, team_id=self.t1.team_id))
            # Agent Boston 2 in Team 3
            db.add(AgentTeamAssociation(user_id=self.u_agent_boston2.user_id, team_id=self.t3.team_id))
            # Agent GesDent in Team 2
            db.add(AgentTeamAssociation(user_id=self.u_agent_gesdent.user_id, team_id=self.t2.team_id))
            await db.flush()

            # 6. Prompts (Speech Structures)
            self.prompt1 = Prompt(prompt_id=1, prompt_name="Estructura Boston", prompt_type="audio", is_active=True, service_id=self.s1.service_id, company_id=self.c1.company_id)
            self.prompt2 = Prompt(prompt_id=2, prompt_name="Estructura GesDent", prompt_type="audio", is_active=True, service_id=self.s2.service_id, company_id=self.c2.company_id)
            db.add_all([self.prompt1, self.prompt2])
            await db.flush()

            # 7. Trainer Evaluation Configs
            self.cfg1 = TrainerEvaluationConfig(config_id=1, company_id=self.c1.company_id, name="Config Boston", service_id=self.s1.service_id, speech_structure_id=self.prompt1.prompt_id, is_active=True)
            self.cfg2 = TrainerEvaluationConfig(config_id=2, company_id=self.c2.company_id, name="Config GesDent", service_id=self.s2.service_id, speech_structure_id=self.prompt2.prompt_id, is_active=True)
            db.add_all([self.cfg1, self.cfg2])
            await db.flush()

            # 8. Trainer Simulations
            self.sim1 = TrainerSimulation(simulation_id=1, company_id=self.c1.company_id, name="Sim Boston", code="SIM_B1", service_id=self.s1.service_id, evaluation_config_id=self.cfg1.config_id, roleplay_prompt="Prompt Boston", status="published")
            self.sim2 = TrainerSimulation(simulation_id=2, company_id=self.c2.company_id, name="Sim GesDent", code="SIM_G1", service_id=self.s2.service_id, evaluation_config_id=self.cfg2.config_id, roleplay_prompt="Prompt GesDent", status="published")
            db.add_all([self.sim1, self.sim2])
            await db.flush()

            # 9. Trainer Sessions
            self.sess_b1 = TrainerSession(session_id=1, simulation_id=self.sim1.simulation_id, agent_id="boston_owner_1", agent_code="BA1", company_id=self.c1.company_id, service_id=self.s1.service_id, call_id="call_boston_1", status="completed", evaluation_status="evaluated")
            self.sess_b2 = TrainerSession(session_id=2, simulation_id=self.sim1.simulation_id, agent_id="boston_owner_2", agent_code="BA2", company_id=self.c1.company_id, service_id=self.s1.service_id, call_id="call_boston_2", status="completed", evaluation_status="evaluated")
            self.sess_g1 = TrainerSession(session_id=3, simulation_id=self.sim2.simulation_id, agent_id="gesdent_owner", agent_code="GDA", company_id=self.c2.company_id, service_id=self.s2.service_id, call_id="call_gesdent_1", status="completed", evaluation_status="evaluated")
            db.add_all([self.sess_b1, self.sess_b2, self.sess_g1])
            await db.flush()

            # 10. Trainer Evaluations
            self.eval_b1 = TrainerEvaluation(evaluation_id=1, session_id=self.sess_b1.session_id, evaluation_config_id=self.cfg1.config_id, prompt_snapshot="...", result_json={"score": 8.5}, score=Decimal("8.5"))
            self.eval_b2 = TrainerEvaluation(evaluation_id=2, session_id=self.sess_b2.session_id, evaluation_config_id=self.cfg1.config_id, prompt_snapshot="...", result_json={"score": 7.0}, score=Decimal("7.0"))
            self.eval_g1 = TrainerEvaluation(evaluation_id=3, session_id=self.sess_g1.session_id, evaluation_config_id=self.cfg2.config_id, prompt_snapshot="...", result_json={"score": 6.5}, score=Decimal("6.5"))
            db.add_all([self.eval_b1, self.eval_b2, self.eval_g1])
            await db.commit()

        # Build tokens
        self.t_super = create_access_token({"user_id": 1, "email": "super@test.com"})
        self.t_boston_admin = create_access_token({"user_id": 2, "email": "boston_admin@test.com"})
        self.t_boston_mgr = create_access_token({"user_id": 3, "email": "boston_mgr@test.com"})
        self.t_boston_coor = create_access_token({"user_id": 4, "email": "boston_coor@test.com"})
        self.t_boston_agent1 = create_access_token({"user_id": 5, "email": "agent_b1@test.com"})
        self.t_boston_agent2 = create_access_token({"user_id": 6, "email": "agent_b2@test.com"})
        self.t_gesdent_agent = create_access_token({"user_id": 7, "email": "agent_gesdent@test.com"})

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        # Clean DB file
        if os.path.exists("multitenancy_trainer_test.db"):
            try:
                os.remove("multitenancy_trainer_test.db")
            except Exception:
                pass

    # ── Test Simulations Scoping ──────────────────────────────────────────────

    async def test_list_simulations(self):
        # Super admin sees both simulations
        res = await self.client.get("/bm/trainer/simulations", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 2)

        # Boston admin sees only Boston simulation
        res = await self.client.get("/bm/trainer/simulations", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)
        self.assertEqual(res.json()[0]["simulation_id"], 1)

        # Boston manager sees only Boston simulation
        res = await self.client.get("/bm/trainer/simulations", headers={"Authorization": f"Bearer {self.t_boston_mgr}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)
        self.assertEqual(res.json()[0]["simulation_id"], 1)

    async def test_get_simulation_detail(self):
        # Boston admin gets Boston simulation
        res = await self.client.get("/bm/trainer/simulations/1", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)

        # Boston admin gets GesDent simulation -> 403 Forbidden
        res = await self.client.get("/bm/trainer/simulations/2", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 403)

    async def test_create_simulation_scoping(self):
        # Boston admin creates simulation for Service 1 -> 201 Created
        res = await self.client.post(
            "/bm/trainer/simulations",
            json={
                "name": "Nueva Sim Boston",
                "code": "SIM_B2",
                "service_id": 1,
                "roleplay_prompt": "Prompt text",
                "evaluation_config_id": 1,
                "objective": "Objective",
                "difficulty": "media"
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 201)

        # Boston admin tries to create simulation for Service 2 (GesDent) -> 403 Forbidden
        res = await self.client.post(
            "/bm/trainer/simulations",
            json={
                "name": "Nueva Sim GesDent",
                "code": "SIM_G2",
                "service_id": 2,
                "roleplay_prompt": "Prompt text",
                "evaluation_config_id": 2,
                "objective": "Objective",
                "difficulty": "media"
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

        # Agent tries to create simulation -> 403 Forbidden
        res = await self.client.post(
            "/bm/trainer/simulations",
            json={
                "name": "Nueva Sim",
                "code": "SIM_B3",
                "service_id": 1,
                "roleplay_prompt": "Prompt text",
                "evaluation_config_id": 1
            },
            headers={"Authorization": f"Bearer {self.t_boston_agent1}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_update_simulation_scoping(self):
        # Boston admin updates Boston simulation -> 200 OK
        res = await self.client.patch(
            "/bm/trainer/simulations/1",
            json={"name": "Sim Boston Modificado"},
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)

        # Boston admin tries to update GesDent simulation -> 403 Forbidden
        res = await self.client.patch(
            "/bm/trainer/simulations/2",
            json={"name": "Sim GesDent Modificado"},
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

    # ── Test Evaluation Configs Scoping ────────────────────────────────────────

    async def test_list_evaluation_configs(self):
        # Super admin sees both configs
        res = await self.client.get("/bm/trainer/evaluation-configs", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 2)

        # Boston admin sees only Boston config
        res = await self.client.get("/bm/trainer/evaluation-configs", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)
        self.assertEqual(res.json()[0]["config_id"], 1)

    async def test_get_evaluation_config_detail(self):
        # Boston admin gets Boston config -> 200
        res = await self.client.get("/bm/trainer/evaluation-configs/1", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)

        # Boston admin gets GesDent config -> 403 Forbidden
        res = await self.client.get("/bm/trainer/evaluation-configs/2", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 403)

    async def test_create_evaluation_config_scoping(self):
        # Boston admin creates config for Service 1 -> 201 Created
        res = await self.client.post(
            "/bm/trainer/evaluation-configs",
            json={
                "name": "Nuevo Config Boston",
                "service_id": 1,
                "speech_structure_id": 1,
                "extra_instructions": "None",
                "is_active": True
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 201)

        # Boston admin tries to create config for Service 2 (GesDent) -> 403 Forbidden
        res = await self.client.post(
            "/bm/trainer/evaluation-configs",
            json={
                "name": "Nuevo Config GesDent",
                "service_id": 2,
                "speech_structure_id": 2,
                "extra_instructions": "None",
                "is_active": True
            },
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

    async def test_available_evaluation_structures_scoping(self):
        # Boston admin gets Service 1 structures -> 200 OK
        res = await self.client.get(
            "/bm/trainer/services/1/available-evaluation-structures",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 1)
        self.assertEqual(res.json()[0]["prompt_id"], 1)

        # Boston admin tries to get Service 2 structures -> 403 Forbidden
        res = await self.client.get(
            "/bm/trainer/services/2/available-evaluation-structures",
            headers={"Authorization": f"Bearer {self.t_boston_admin}"}
        )
        self.assertEqual(res.status_code, 403)

    # ── Test Sessions Scoping ──────────────────────────────────────────────────

    async def test_list_sessions(self):
        # Super admin sees all 3 sessions
        res = await self.client.get("/bm/trainer/sessions", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total_count"], 3)

        # Boston admin sees both Boston sessions (boston_owner_1, boston_owner_2)
        res = await self.client.get("/bm/trainer/sessions", headers={"Authorization": f"Bearer {self.t_boston_admin}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total_count"], 2)

        # Boston manager sees both Boston sessions (service 1)
        res = await self.client.get("/bm/trainer/sessions", headers={"Authorization": f"Bearer {self.t_boston_mgr}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total_count"], 2)

        # Boston coordinator sees only Team 1 sessions (agent 1)
        res = await self.client.get("/bm/trainer/sessions", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total_count"], 1)
        self.assertEqual(res.json()["sessions"][0]["agent_id"], "boston_owner_1")

        # Agent 1 sees only their own session
        res = await self.client.get("/bm/trainer/sessions", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total_count"], 1)
        self.assertEqual(res.json()["sessions"][0]["agent_id"], "boston_owner_1")

    async def test_get_session_detail_scoping(self):
        # Agent 1 gets their own session -> 200 OK
        res = await self.client.get("/bm/trainer/sessions/1", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 200)

        # Agent 1 tries to get Agent 2 session -> 403 Forbidden
        res = await self.client.get("/bm/trainer/sessions/2", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 403)

        # Agent 1 tries to get GesDent session -> 404 Not Found
        res = await self.client.get("/bm/trainer/sessions/3", headers={"Authorization": f"Bearer {self.t_boston_agent1}"})
        self.assertEqual(res.status_code, 404)

        # Boston coordinator gets Agent 1 session (in Team 1) -> 200 OK
        res = await self.client.get("/bm/trainer/sessions/1", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 200)

        # Boston coordinator tries to get Agent 2 session (in Team 3 - not coordinated) -> 403 Forbidden
        res = await self.client.get("/bm/trainer/sessions/2", headers={"Authorization": f"Bearer {self.t_boston_coor}"})
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    import asyncio
    asyncio.run(unittest.main())
