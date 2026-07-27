"""
Comprehensive Backend API Contract Audit Suite for Multitenancy and Role-Based Permissions.

Tests all 5 roles:
- super_admin
- company_admin
- service_manager / responsable_servicio
- team_coordinator / coordinador_equipo
- agent / agente

Across 2 Companies (Boston Medical, Empresa Demo), Services, Teams, Base Structures, Active Prompts,
Mass Evaluations, Automations, Training Tracking, and Trainer HTTP.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

# Force local sqlite test database
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///multitenant_api_contract_test.db"

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
from sqlalchemy import select

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.typologies import Typology
from app.models.prompts import Prompt, PromptVersion, PromptBaseStructure
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult
from app.models.personalized_training import TrainingAgentSetting, TrainingAgentReport
from app.utils.security import create_access_token, hash_password
from app.main import app


class TestMultitenantApiContract(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        engine = get_engine()
        db_url_str = str(engine.url)
        assert "91.98.230.119" not in db_url_str, "CRITICAL: Database engine URL points to production host!"

        if os.path.exists("multitenant_api_contract_test.db"):
            try:
                os.remove("multitenant_api_contract_test.db")
            except Exception:
                pass

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        self.engine = engine

        async with AsyncSession(engine, expire_on_commit=False) as db:
            pwd_hash = hash_password("Pass1234!")

            # 1. Companies
            c10 = Company(company_id=10, company_name="Boston Medical", company_key="boston-medical", is_active=True)
            c20 = Company(company_id=20, company_name="Empresa Demo", company_key="empresa-demo", is_active=True)
            db.add_all([c10, c20])
            await db.flush()

            # 2. Services
            s101 = Service(service_id=101, service_name="Front", service_key="front", company_id=10, is_active=True)
            s102 = Service(service_id=102, service_name="Experiencia de Paciente", service_key="exppa", company_id=10, is_active=True)
            s103 = Service(service_id=103, service_name="Comerciales", service_key="comerciales", company_id=10, is_active=True)
            s201 = Service(service_id=201, service_name="Demo General", service_key="demo", company_id=20, is_active=True)
            db.add_all([s101, s102, s103, s201])
            await db.flush()

            # 3. Teams
            t1001 = Team(team_id=1001, team_name="Equipo Front Principal", service_id=101, company_id=10, is_active=True)
            t1002 = Team(team_id=1002, team_name="Equipo ExpPa Principal", service_id=102, company_id=10, is_active=True)
            t1003 = Team(team_id=1003, team_name="Equipo Comerciales Principal", service_id=103, company_id=10, is_active=True)
            t2001 = Team(team_id=2001, team_name="Equipo Demo Principal", service_id=201, company_id=20, is_active=True)
            db.add_all([t1001, t1002, t1003, t2001])
            await db.flush()

            # 4. Users (All 5 roles across Company 10 & 20)
            u_super = User(user_id=1001, username="super_admin", email="super@boston.com", name="Super Admin", role="super_admin", password_hash=pwd_hash, is_active=True)
            
            u_admin_boston = User(user_id=1010, username="adminboston", email="adminboston@boston.com", name="Admin Boston", role="company_admin", company_id=10, password_hash=pwd_hash, is_active=True)
            u_admin_demo = User(user_id=2010, username="admindemo", email="admindemo@demo.com", name="Admin Demo", role="company_admin", company_id=20, password_hash=pwd_hash, is_active=True)

            u_sm_front = User(user_id=1011, username="resp_front", email="resp_front@boston.com", name="Responsable Front", role="responsable_servicio", company_id=10, primary_service_id=101, password_hash=pwd_hash, is_active=True)
            u_sm_exppa = User(user_id=1012, username="jrodriguez", email="jrodriguez@boston.com", name="Juanjo Rodriguez", role="responsable_servicio", company_id=10, primary_service_id=102, password_hash=pwd_hash, is_active=True)
            u_sm_com = User(user_id=1013, username="jbermejo", email="jbermejo@boston.com", name="Jorge Bermejo", role="responsable_servicio", company_id=10, primary_service_id=103, password_hash=pwd_hash, is_active=True)

            u_tc_exppa = User(user_id=1020, username="jcerdan", email="jcerdan@boston.com", name="Jordi Cerdan", role="coordinador_equipo", company_id=10, primary_service_id=102, primary_team_id=1002, password_hash=pwd_hash, is_active=True)
            u_tc_com = User(user_id=1021, username="bpena", email="bpena@boston.com", name="Braulio Pena", role="coordinador_equipo", company_id=10, primary_service_id=103, primary_team_id=1003, password_hash=pwd_hash, is_active=True)

            u_ag_front = User(user_id=1030, username="ag_front", email="ag_front@boston.com", name="Agente Front", role="agente", company_id=10, primary_service_id=101, primary_team_id=1001, hubspot_owner_id="hs_front_1", password_hash=pwd_hash, is_active=True)
            u_ag_exppa_1 = User(user_id=1031, username="varellano", email="varellano@boston.com", name="Victoria Arellano", role="agente", company_id=10, primary_service_id=102, primary_team_id=1002, hubspot_owner_id="31499194", password_hash=pwd_hash, is_active=True)
            u_ag_exppa_2 = User(user_id=1032, username="molvera", email="molvera@boston.com", name="Maria Olvera", role="agente", company_id=10, primary_service_id=102, primary_team_id=1002, hubspot_owner_id="76997586", password_hash=pwd_hash, is_active=True)
            u_ag_com = User(user_id=1033, username="ag_com", email="ag_com@boston.com", name="Agente Comerciales", role="agente", company_id=10, primary_service_id=103, primary_team_id=1003, hubspot_owner_id="hs_com_1", password_hash=pwd_hash, is_active=True)

            u_ag_demo = User(user_id=2030, username="ag_demo", email="ag_demo@demo.com", name="Agente Demo", role="agente", company_id=20, primary_service_id=201, primary_team_id=2001, hubspot_owner_id="hs_demo_1", password_hash=pwd_hash, is_active=True)

            db.add_all([
                u_super, u_admin_boston, u_admin_demo,
                u_sm_front, u_sm_exppa, u_sm_com,
                u_tc_exppa, u_tc_com,
                u_ag_front, u_ag_exppa_1, u_ag_exppa_2, u_ag_com,
                u_ag_demo
            ])
            await db.flush()

            # Service / Team Associations
            db.add_all([
                UserServiceAssociation(user_id=1011, service_id=101),
                UserServiceAssociation(user_id=1012, service_id=102),
                UserServiceAssociation(user_id=1013, service_id=103),
                UserTeamAssociation(user_id=1020, team_id=1002),
                UserTeamAssociation(user_id=1021, team_id=1003),
                AgentTeamAssociation(user_id=1030, team_id=1001),
                AgentTeamAssociation(user_id=1031, team_id=1002),
                AgentTeamAssociation(user_id=1032, team_id=1002),
                AgentTeamAssociation(user_id=1033, team_id=1003),
                AgentTeamAssociation(user_id=2030, team_id=2001),
            ])
            await db.flush()

            # 5. Typologies
            typ_exppa = Typology(typology_id=101, typology_key="primera_consulta", typology_name="Primera Consulta ExpPa", service_id=102, company_id=10, is_active=True)
            db.add(typ_exppa)
            await db.flush()

            # 6. Base & Specific Prompt Structures
            base_exppa = PromptBaseStructure(id=101, structure_key="base_exppa", structure_name="Base ExpPa V1", base_prompt="Prompt base content", service_id=102, company_id=10, is_active=True)
            db.add(base_exppa)
            await db.flush()

            prompt_exppa = Prompt(prompt_id=101, prompt_name="Prompt ExpPa Audio V1", prompt_type="audio", service_id=102, company_id=10, is_active=True)
            db.add(prompt_exppa)
            await db.flush()

            ver_exppa = PromptVersion(id=1001, prompt_id=101, prompt="Contenido Prompt ExpPa", version_label="v1", is_current=True)
            db.add(ver_exppa)
            await db.flush()

            # 7. Mass Evaluation Job & Result for ExpPa
            job_exppa = MassEvaluationJob(
                job_id=101, job_name="Evaluación Masiva ExpPa", company_id=10, service_id=102, prompt_id=101,
                agent_owner_ids=["31499194", "76997586"], is_active=True, execution_source="on_demand"
            )
            db.add(job_exppa)
            await db.flush()

            res_exppa = MassEvaluationResult(
                mass_analysis_id=1001, run_id=1, job_id=101, company_id=10, service_id=102, prompt_id=101,
                prompt_snapshot="{}", hubspot_owner_id="31499194", call_id="call_exppa_1001", status="completed", evaluacion_global=9.0
            )
            db.add(res_exppa)

            await db.commit()

        # Generate JWT Bearer Tokens
        self.t_super = create_access_token({"user_id": 1001, "email": "super@boston.com"})
        self.t_admin_boston = create_access_token({"user_id": 1010, "email": "adminboston@boston.com"})
        self.t_admin_demo = create_access_token({"user_id": 2010, "email": "admindemo@demo.com"})
        self.t_sm_exppa = create_access_token({"user_id": 1012, "email": "jrodriguez@boston.com"})
        self.t_tc_exppa = create_access_token({"user_id": 1020, "email": "jcerdan@boston.com"})
        self.t_ag_exppa = create_access_token({"user_id": 1031, "email": "varellano@boston.com"})

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        if os.path.exists("multitenant_api_contract_test.db"):
            try:
                os.remove("multitenant_api_contract_test.db")
            except Exception:
                pass

    # ── Task 3: Auth & Tenant Context Validation ─────────────────────────────

    async def test_01_tenant_context_for_all_roles(self):
        """Verify /bm/me and /bm/me/tenant-context per role."""
        # 1. Super Admin
        r_super = await self.client.get("/bm/me/tenant-context", headers={"Authorization": f"Bearer {self.t_super}"})
        self.assertEqual(r_super.status_code, 200)
        d_super = r_super.json()
        self.assertEqual(d_super["normalized_role"], "super_admin")
        self.assertTrue(d_super["is_super_admin"])
        self.assertTrue(d_super["can_manage_users"])

        # 2. Company Admin Boston
        r_admin = await self.client.get("/bm/me/tenant-context", headers={"Authorization": f"Bearer {self.t_admin_boston}"})
        self.assertEqual(r_admin.status_code, 200)
        d_admin = r_admin.json()
        self.assertEqual(d_admin["normalized_role"], "company_admin")
        self.assertEqual(d_admin["company_id"], 10)
        self.assertTrue(d_admin["can_manage_users"])
        self.assertTrue(d_admin["can_manage_teams"])

        # 3. Service Manager ExpPa
        r_sm = await self.client.get("/bm/me/tenant-context", headers={"Authorization": f"Bearer {self.t_sm_exppa}"})
        self.assertEqual(r_sm.status_code, 200)
        d_sm = r_sm.json()
        self.assertEqual(d_sm["normalized_role"], "service_manager")
        self.assertIn(102, d_sm["allowed_service_ids"])
        self.assertTrue(d_sm["can_manage_training"])

        # 4. Team Coordinator ExpPa
        r_tc = await self.client.get("/bm/me/tenant-context", headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_tc.status_code, 200)
        d_tc = r_tc.json()
        self.assertEqual(d_tc["normalized_role"], "team_coordinator")
        self.assertIn(1002, d_tc["allowed_team_ids"])
        self.assertTrue(d_tc["can_manage_users"])

        # 5. Agent
        r_ag = await self.client.get("/bm/me/tenant-context", headers={"Authorization": f"Bearer {self.t_ag_exppa}"})
        self.assertEqual(r_ag.status_code, 200)
        d_ag = r_ag.json()
        self.assertEqual(d_ag["normalized_role"], "agent")
        self.assertFalse(d_ag["can_manage_users"])
        self.assertFalse(d_ag["can_manage_teams"])

    # ── Task 4: Users Endpoint Scoping & Flags ───────────────────────────────

    async def test_02_users_scoping_and_flags(self):
        """Verify GET /bm/users scoping and user management flags."""
        # 1. Company Admin Boston: sees only Company 10 users
        r_adm = await self.client.get("/bm/users", headers={"Authorization": f"Bearer {self.t_admin_boston}"})
        self.assertEqual(r_adm.status_code, 200)
        users_adm = r_adm.json()["users"]
        comp_ids_adm = {u["company_id"] for u in users_adm if u["company_id"]}
        self.assertEqual(comp_ids_adm, {10})
        self.assertNotIn(2030, {u["user_id"] for u in users_adm})

        # 2. Team Coordinator ExpPa (jcerdan):
        r_tc = await self.client.get("/bm/users", headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_tc.status_code, 200)
        users_tc = r_tc.json()["users"]
        tc_uids = {u["user_id"] for u in users_tc}
        
        # Sees self, ExpPa agents, and immediate SM (Juanjo Rodriguez)
        self.assertIn(1020, tc_uids)  # jcerdan (self)
        self.assertIn(1031, tc_uids)  # Victoria Arellano
        self.assertIn(1032, tc_uids)  # Maria Olvera
        self.assertIn(1012, tc_uids)  # Juanjo Rodriguez (immediate SM)

        # Does NOT see unrelated agents, coordinators, or Company 20
        self.assertNotIn(1030, tc_uids)  # ag_front
        self.assertNotIn(1033, tc_uids)  # ag_com
        self.assertNotIn(2030, tc_uids)  # ag_demo

        # Check flags for team agents vs immediate SM
        v_arellano = next(u for u in users_tc if u["user_id"] == 1031)
        self.assertFalse(v_arellano["is_readonly"])
        self.assertTrue(v_arellano["can_edit"])
        self.assertTrue(v_arellano["can_reset_password"])

        j_rodriguez = next(u for u in users_tc if u["user_id"] == 1012)
        self.assertTrue(j_rodriguez["is_readonly"])
        self.assertFalse(j_rodriguez["can_edit"])

    # ── Task 5: Teams Scoping & Permissions ─────────────────────────────────

    async def test_03_teams_scoping_and_permissions(self):
        """Verify GET/POST/PATCH /bm/admin/teams per role."""
        # 1. Company Admin Boston: sees all 3 teams in Company 10
        r_adm = await self.client.get("/bm/admin/teams", headers={"Authorization": f"Bearer {self.t_admin_boston}"})
        self.assertEqual(r_adm.status_code, 200)
        t_ids_adm = [t["team_id"] for t in r_adm.json()]
        self.assertEqual(set(t_ids_adm), {1001, 1002, 1003})

        # 2. Team Coordinator ExpPa: sees only Team 1002
        r_tc = await self.client.get("/bm/admin/teams", headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_tc.status_code, 200)
        t_ids_tc = [t["team_id"] for t in r_tc.json()]
        self.assertEqual(t_ids_tc, [1002])

        # Team Coordinator creating team -> 403
        r_create_tc = await self.client.post("/bm/admin/teams", json={
            "team_name": "Nuevo Equipo Invalido",
            "service_id": 102,
            "company_id": 10
        }, headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_create_tc.status_code, 403)

        # 3. Agent -> 403
        r_ag = await self.client.get("/bm/admin/teams", headers={"Authorization": f"Bearer {self.t_ag_exppa}"})
        self.assertEqual(r_ag.status_code, 403)

    # ── Task 6: Services Scoping ─────────────────────────────────────────────

    async def test_04_services_scoping(self):
        """Verify GET /bm/services per role."""
        # Company Admin Boston sees Company 10 services only
        r_adm = await self.client.get("/bm/services", headers={"Authorization": f"Bearer {self.t_admin_boston}"})
        self.assertEqual(r_adm.status_code, 200)
        svc_ids = [s["service_id"] for s in r_adm.json()]
        self.assertIn(101, svc_ids)
        self.assertIn(102, svc_ids)
        self.assertNotIn(201, svc_ids)

    # ── Task 7: Typologies Scoping ───────────────────────────────────────────

    async def test_05_typologies_scoping(self):
        """Verify GET /bm/typologies per role."""
        r_tc = await self.client.get("/bm/typologies?service_id=102", headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_tc.status_code, 200)

    # ── Task 8: Base Structure Scoping ───────────────────────────────────────

    async def test_06_base_structures_scoping(self):
        """Verify /bm/prompt-base-structures endpoints."""
        r_sm = await self.client.get("/bm/prompt-base-structures", headers={"Authorization": f"Bearer {self.t_sm_exppa}"})
        self.assertEqual(r_sm.status_code, 200)

    # ── Task 9: Specific Prompts & Active Prompts ─────────────────────────────

    async def test_07_prompts_and_active_scoping(self):
        """Verify GET /bm/prompts/active scoping per service."""
        r_active = await self.client.get(
            "/bm/prompts/active?type=audio&service_id=102",
            headers={"Authorization": f"Bearer {self.t_tc_exppa}"}
        )
        self.assertEqual(r_active.status_code, 200)
        data = r_active.json()
        self.assertEqual(data["prompt_id"], 101)
        self.assertEqual(data["service_id"], 102)

    # ── Task 10: Test Analysis Scoping ───────────────────────────────────────

    async def test_08_test_analysis_scoping(self):
        """Verify test analysis request validation."""
        # Service 101 with prompt_id=101 (Service 102) mismatch -> 422
        r_mismatch = await self.client.post("/bm/test-analysis/by-call-id", json={
            "call_id": "call_test_mismatch",
            "service_id": 101,
            "prompt_id": 101
        }, headers={"Authorization": f"Bearer {self.t_admin_boston}"})
        self.assertEqual(r_mismatch.status_code, 422)

    # ── Task 11: Mass On-Demand Scoping ──────────────────────────────────────

    async def test_09_mass_evaluation_jobs_and_results(self):
        """Verify /bm/mass-evaluation-jobs and results per role."""
        # Team Coordinator ExpPa lists jobs
        r_jobs = await self.client.get("/bm/mass-evaluation-jobs", headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_jobs.status_code, 200)
        jobs = r_jobs.json()
        job_ids = [j["job_id"] for j in jobs]
        self.assertIn(101, job_ids)

        # Team Coordinator ExpPa creates job for Victoria Arellano -> 201
        r_create = await self.client.post("/bm/mass-evaluation-jobs", json={
            "job_name": "Nuevo Job Victoria",
            "service_id": 102,
            "prompt_id": 101,
            "company_id": 10,
            "agent_owner_ids": ["31499194"]
        }, headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_create.status_code, 201)

        # Team Coordinator ExpPa creates job for Front agent -> 403
        r_err = await self.client.post("/bm/mass-evaluation-jobs", json={
            "job_name": "Job Front Invalido",
            "service_id": 102,
            "prompt_id": 101,
            "company_id": 10,
            "agent_owner_ids": ["hs_front_1"]
        }, headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_err.status_code, 403)

    # ── Task 12: Automations Scoping ─────────────────────────────────────────

    async def test_10_automations_scoping(self):
        """Verify /bm/mass-analysis/automations per role."""
        r_auto = await self.client.get("/bm/mass-analysis/automations", headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_auto.status_code, 200)

        # Agent -> 403
        r_ag = await self.client.get("/bm/mass-analysis/automations", headers={"Authorization": f"Bearer {self.t_ag_exppa}"})
        self.assertEqual(r_ag.status_code, 403)

    # ── Task 13: Personalized Training Scoping ────────────────────────────────

    async def test_11_personalized_training_scoping(self):
        """Verify /bm/training/admin/agents-overview and settings per role."""
        # Team Coordinator ExpPa overview: returns Victoria & Maria, excludes Front/Com/Demo agents
        r_ov = await self.client.get("/bm/training/admin/agents-overview", headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_ov.status_code, 200)
        ov_agents = [a["hubspot_owner_id"] for a in r_ov.json()]
        self.assertIn("31499194", ov_agents)
        self.assertIn("76997586", ov_agents)
        self.assertNotIn("hs_front_1", ov_agents)
        self.assertNotIn("hs_com_1", ov_agents)

        # Team Coordinator ExpPa creates manual cycle for Victoria -> 200
        r_manual = await self.client.post("/bm/training/admin/manual-cycle", json={
            "hubspot_owner_ids": ["31499194"],
            "title": "Ciclo Victoria",
            "objectives": ["Mejorar empatia"]
        }, headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_manual.status_code, 200)

        # Team Coordinator ExpPa creates manual cycle for Front agent -> 403
        r_manual_err = await self.client.post("/bm/training/admin/manual-cycle", json={
            "hubspot_owner_ids": ["hs_front_1"],
            "title": "Ciclo Invalido Front",
            "objectives": ["Objetivo"]
        }, headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_manual_err.status_code, 403)

        # Non-super-admin accessing scheduler settings -> 403
        r_sched = await self.client.get("/bm/training/admin/scheduler-settings", headers={"Authorization": f"Bearer {self.t_admin_boston}"})
        self.assertEqual(r_sched.status_code, 403)

    # ── Task 14: Trainer HTTP Scoping ─────────────────────────────────────────

    async def test_12_trainer_http_scoping(self):
        """Verify GET /bm/trainer/simulations per role."""
        r_sim = await self.client.get("/bm/trainer/simulations", headers={"Authorization": f"Bearer {self.t_tc_exppa}"})
        self.assertEqual(r_sim.status_code, 200)


if __name__ == "__main__":
    asyncio.run(unittest.main())
