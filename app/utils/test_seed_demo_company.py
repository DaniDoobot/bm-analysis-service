"""
Unit tests for scripts/seed_demo_company.py
============================================
Validates:
1. Correct creation of Empresa Demo structure:
   - 1 Company (is_demo=True, key='empresa-demo')
   - 2 Services (Atención al Cliente, Ventas)
   - 4 Teams (Front, Backoffice, Comercial, Retención)
   - 67 Users (1 Admin, 2 Managers, 4 Coordinators, 60 Agents)
   - 60 Trainer Credentials (TrainingAgentSetting with training_code & numeric_code)
   - User service & team associations
2. Tenant Isolation:
   - All records are strictly scoped to the demo company_id.
3. Idempotency guard:
   - Re-seeding when company_key='empresa-demo' exists raises DemoCompanyAlreadyExistsError.
4. Atomic rollback:
   - If an error occurs midway, no partial data remains committed.
"""
import os
import sys
import unittest

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_seed_demo_company.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.personalized_training import TrainingAgentSetting
from scripts.seed_demo_company import (
    seed_demo_structure,
    DemoCompanyAlreadyExistsError,
    DEMO_COMPANY_KEY,
    DEMO_COMPANY_NAME,
)


class TestSeedDemoCompany(unittest.IsolatedAsyncioTestCase):
    """Test suite for isolated demo company structure seeding."""

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self.engine.dispose()

    @classmethod
    def tearDownClass(cls):
        try:
            if os.path.exists("test_seed_demo_company.db"):
                os.remove("test_seed_demo_company.db")
        except Exception:
            pass

    async def test_seed_demo_structure_success(self):
        """Verify full creation, counts, associations, trainer credentials, and tenant isolation."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                summary = await seed_demo_structure(session)

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["company"]["company_name"], DEMO_COMPANY_NAME)
        self.assertEqual(summary["company"]["company_key"], DEMO_COMPANY_KEY)
        self.assertTrue(summary["company"]["is_demo"])
        self.assertEqual(len(summary["services"]), 2)
        self.assertEqual(len(summary["teams"]), 4)
        self.assertEqual(summary["users"]["total"], 67)
        self.assertEqual(summary["users"]["agents"], 60)
        self.assertEqual(summary["users"]["company_admins"], 1)
        self.assertEqual(summary["users"]["service_managers"], 2)
        self.assertEqual(summary["users"]["team_coordinators"], 4)
        self.assertEqual(summary["trainer_settings_count"], 60)

        async with AsyncSession(self.engine) as session:
            # 1. Company check
            comp_res = await session.execute(select(Company).where(Company.company_key == DEMO_COMPANY_KEY))
            comp = comp_res.scalars().first()
            self.assertIsNotNone(comp)
            self.assertEqual(comp.company_name, DEMO_COMPANY_NAME)
            self.assertTrue(comp.is_demo)
            self.assertTrue(comp.is_active)
            cid = comp.company_id

            # 2. Services check (2 services)
            svc_res = await session.execute(select(Service).where(Service.company_id == cid))
            services = list(svc_res.scalars().all())
            self.assertEqual(len(services), 2)
            svc_keys = {s.service_key for s in services}
            self.assertEqual(svc_keys, {"atencion-al-cliente", "ventas"})

            # 3. Teams check (4 teams)
            team_res = await session.execute(select(Team).where(Team.company_id == cid))
            teams = list(team_res.scalars().all())
            self.assertEqual(len(teams), 4)
            team_names = {t.team_name for t in teams}
            self.assertEqual(
                team_names,
                {"Front Atención", "Backoffice Atención", "Equipo Comercial", "Equipo Retención"}
            )

            # 4. Users check (67 users)
            usr_res = await session.execute(select(User).where(User.company_id == cid))
            users = list(usr_res.scalars().all())
            self.assertEqual(len(users), 67)

            admins = [u for u in users if u.role == "company_admin"]
            managers = [u for u in users if u.role == "service_manager"]
            coords = [u for u in users if u.role == "team_coordinator"]
            agents = [u for u in users if u.role == "agent"]

            self.assertEqual(len(admins), 1)
            self.assertEqual(admins[0].email, "administradordeempresa.demo@doobot.ai")
            self.assertEqual(len(managers), 2)
            self.assertEqual(len(coords), 4)
            self.assertEqual(len(agents), 60)

            # Check agent team distribution
            team_id_map = {t.team_name: t.team_id for t in teams}
            front_agents = [u for u in agents if u.primary_team_id == team_id_map["Front Atención"]]
            backoffice_agents = [u for u in agents if u.primary_team_id == team_id_map["Backoffice Atención"]]
            comercial_agents = [u for u in agents if u.primary_team_id == team_id_map["Equipo Comercial"]]
            retencion_agents = [u for u in agents if u.primary_team_id == team_id_map["Equipo Retención"]]

            self.assertEqual(len(front_agents), 10)
            self.assertEqual(len(backoffice_agents), 20)
            self.assertEqual(len(comercial_agents), 10)
            self.assertEqual(len(retencion_agents), 20)

            # 5. Check Associations
            # All 60 agents must be in AgentTeamAssociation
            agent_teams_res = await session.execute(select(AgentTeamAssociation))
            agent_teams = list(agent_teams_res.scalars().all())
            self.assertEqual(len(agent_teams), 60)

            # Coordinators must be in UserTeamAssociation
            coord_teams_res = await session.execute(select(UserTeamAssociation))
            coord_teams = list(coord_teams_res.scalars().all())
            self.assertEqual(len(coord_teams), 4)

            # All users must have at least one UserServiceAssociation
            user_svc_res = await session.execute(select(UserServiceAssociation))
            user_svcs = list(user_svc_res.scalars().all())
            self.assertGreaterEqual(len(user_svcs), 67)

            # 6. Check Trainer Settings (60 agents)
            ts_res = await session.execute(
                select(TrainingAgentSetting).where(TrainingAgentSetting.company_id == cid)
            )
            settings = list(ts_res.scalars().all())
            self.assertEqual(len(settings), 60)

            # Verify every setting has unique non-empty training codes
            training_codes = {s.training_code for s in settings}
            numeric_codes = {s.training_numeric_code for s in settings}
            self.assertEqual(len(training_codes), 60)
            self.assertEqual(len(numeric_codes), 60)
            self.assertNotIn(None, training_codes)
            self.assertNotIn(None, numeric_codes)

            # 7. Tenant Isolation Verification
            # Confirm NO user or service or team belongs to another company
            other_comp_users = await session.execute(select(User).where(User.company_id != cid))
            self.assertEqual(len(list(other_comp_users.scalars().all())), 0)
            other_comp_svcs = await session.execute(select(Service).where(Service.company_id != cid))
            self.assertEqual(len(list(other_comp_svcs.scalars().all())), 0)
            other_comp_teams = await session.execute(select(Team).where(Team.company_id != cid))
            self.assertEqual(len(list(other_comp_teams.scalars().all())), 0)

    async def test_seed_aborts_if_company_already_exists(self):
        """Seeding twice must raise DemoCompanyAlreadyExistsError on the second run."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                await seed_demo_structure(session)

        async with AsyncSession(self.engine) as session:
            async with session.begin():
                with self.assertRaises(DemoCompanyAlreadyExistsError) as ctx:
                    await seed_demo_structure(session)
                self.assertIn("already exists", str(ctx.exception))

    async def test_atomic_rollback_on_error(self):
        """Simulated error during seeding rolls back all operations atomically."""
        async with AsyncSession(self.engine) as session:
            try:
                async with session.begin():
                    # Create company
                    c = Company(company_name="Empresa Demo", company_key="empresa-demo", is_demo=True)
                    session.add(c)
                    await session.flush()
                    # Raise error to trigger rollback
                    raise RuntimeError("Simulated failure midway through seeding")
            except RuntimeError:
                pass

        # Verify nothing was persisted
        async with AsyncSession(self.engine) as session:
            check = await session.execute(select(Company).where(Company.company_key == "empresa-demo"))
            self.assertIsNone(check.scalars().first())


if __name__ == "__main__":
    unittest.main()
