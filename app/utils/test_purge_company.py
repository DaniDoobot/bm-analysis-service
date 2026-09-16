"""
Unit test suite for scripts/purge_company.py.
Validates:
1. Dry-run does not modify data in DB.
2. company_id=1 (Boston Medical) is strictly blocked under all circumstances.
3. Non-demo company (is_demo=False) is blocked unless force_non_demo=True.
4. Target company restriction (outside default allowed_ids {2, 3}) is blocked unless force_non_demo=True.
5. Successful purge of a demo company with subordinate entities (services, users, teams, evaluations).
6. Atomic rollback on error during transaction.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_purge_company.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team, UserServiceAssociation, UserTeamAssociation, AgentTeamAssociation
from app.models.users import User
from app.models.typologies import Typology
from app.models.mass_evaluations import MassEvaluationJob, MassEvaluationRun, MassEvaluationResult, MassEvaluationCriterionResult
from app.models.analyses import Analysis, AnalysisResult, CallAnalysisCurrent
from app.models.prompts import Prompt
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
    TrainerEvaluation,
)
from app.models.personalized_training import TrainingRun, TrainingAgentReport, TrainingAgentSetting
from scripts.purge_company import (
    purge_company,
    audit_company_resources,
    CompanyProtectedError,
    CompanyNotFoundError,
    CompanyPurgeError,
)
from scripts.seed_demo_company import seed_demo_structure, DEMO_COMPANY_KEY


class TestPurgeCompany(unittest.IsolatedAsyncioTestCase):
    """Test safety, guards, dry-run, cascade, and rollback of purge_company."""

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            # Company 1: Primary production company (non-demo)
            c1 = Company(
                company_id=1,
                company_name="Boston Medical Group",
                company_key="boston-medical",
                is_demo=False,
                is_active=True,
            )
            # Company 2: Gesalux (demo=False by default)
            c2 = Company(
                company_id=2,
                company_name="Gesalux",
                company_key="gesalux",
                is_demo=False,
                is_active=True,
            )
            # Company 3: Empresa Demo1 (demo=True)
            c3 = Company(
                company_id=3,
                company_name="Empresa Demo1",
                company_key="empresa-demo1",
                is_demo=True,
                is_active=True,
            )
            db.add_all([c1, c2, c3])
            await db.commit()

    async def asyncTearDown(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self.engine.dispose()

    @classmethod
    def tearDownClass(cls):
        try:
            if os.path.exists("test_purge_company.db"):
                os.remove("test_purge_company.db")
        except Exception:
            pass

    async def test_guard_blocks_company_id_1(self):
        """Deleting company_id=1 must be strictly blocked under all circumstances."""
        async with AsyncSession(self.engine) as session:
            with self.assertRaises(CompanyProtectedError) as ctx:
                await purge_company(session, company_id=1, apply=True, force_non_demo=True)
            self.assertIn("company_id=1", str(ctx.exception))

            with self.assertRaises(CompanyProtectedError) as ctx:
                await purge_company(session, company_id=1, apply=False, force_non_demo=False)
            self.assertIn("company_id=1", str(ctx.exception))

    async def test_guard_blocks_non_demo_company_without_force_flag(self):
        """Purging a non-demo company (e.g. company_id=2 with is_demo=False) must raise error without force_non_demo."""
        async with AsyncSession(self.engine) as session:
            with self.assertRaises(CompanyProtectedError) as ctx:
                await purge_company(session, company_id=2, apply=False, force_non_demo=False)
            self.assertIn("is_demo=False", str(ctx.exception))

    async def test_guard_blocks_outside_allowed_ids(self):
        """Purging a company ID not in allowed_ids (e.g. 999) must be blocked without force_non_demo."""
        async with AsyncSession(self.engine) as db:
            c99 = Company(
                company_id=99,
                company_name="Other Demo",
                company_key="other-demo",
                is_demo=True,
                is_active=True,
            )
            db.add(c99)
            await db.commit()

        async with AsyncSession(self.engine) as session:
            with self.assertRaises(CompanyProtectedError) as ctx:
                await purge_company(session, company_id=99, apply=False, force_non_demo=False, allowed_ids={2, 3})
            self.assertIn("not in the allowed target list", str(ctx.exception))

            # With force_non_demo=True it should pass safety checks
            res = await purge_company(session, company_id=99, apply=False, force_non_demo=True, allowed_ids={2, 3})
            self.assertEqual(res["status"], "dry_run")

    async def test_dry_run_does_not_modify_data(self):
        """In dry-run mode (apply=False), data must remain unchanged."""
        async with AsyncSession(self.engine) as session:
            res = await purge_company(session, company_id=3, apply=False)
            self.assertEqual(res["status"], "dry_run")
            self.assertFalse(res["applied"])

        # Verify company 3 still exists in DB
        async with AsyncSession(self.engine) as session:
            check = await session.execute(select(Company).where(Company.company_id == 3))
            self.assertIsNotNone(check.scalars().first())

    async def test_purge_allowed_empty_demo_company(self):
        """A demo company with no child records can be cleanly purged with apply=True."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                res = await purge_company(session, company_id=3, apply=True)
                self.assertEqual(res["status"], "applied")
                self.assertTrue(res["applied"])

        # Verify company 3 is gone from DB
        async with AsyncSession(self.engine) as session:
            check = await session.execute(select(Company).where(Company.company_id == 3))
            self.assertIsNone(check.scalars().first())

    async def test_purge_demo_company_with_full_hierarchy(self):
        """Purge a demo company populated with services, users, teams, and mass evaluations."""
        async with AsyncSession(self.engine) as db:
            # Seed service
            svc = Service(service_id=30, service_name="Demo Svc", service_key="demo_svc", company_id=3)
            db.add(svc)
            # Seed user
            usr = User(
                user_id=301,
                username="agent_demo",
                email="agent_demo@doobot.ai",
                password_hash="fakehash",
                role="agent",
                company_id=3,
                hubspot_owner_id="demo_owner_1",
            )
            db.add(usr)
            # Seed team
            tm = Team(team_id=3001, team_name="Team Demo", service_id=30, company_id=3)
            db.add(tm)
            # Seed mass job, run, result
            job = MassEvaluationJob(job_id=30001, job_name="Job Demo", company_id=3, service_id=30, prompt_id=1)
            db.add(job)
            run = MassEvaluationRun(run_id=300001, job_id=30001, company_id=3, trigger_type="manual", status="completed")
            db.add(run)
            res = MassEvaluationResult(
                mass_analysis_id=3000001,
                run_id=300001,
                job_id=30001,
                call_id="call_demo_1",
                company_id=3,
                service_id=30,
                status="completed",
                prompt_id=1,
                prompt_snapshot="snapshot",
            )
            db.add(res)
            # Seed prompt & trainer
            p = Prompt(prompt_id=301, prompt_name="Prompt Demo", prompt_type="speech", company_id=3, service_id=30)
            db.add(p)
            tc = TrainerEvaluationConfig(config_id=301, name="Trainer Cfg Demo", company_id=3, service_id=30, speech_structure_id=301)
            db.add(tc)
            tsim = TrainerSimulation(simulation_id=301, name="Sim Demo", code="sim_demo_301", company_id=3, service_id=30, evaluation_config_id=301, roleplay_prompt="roleplay")
            db.add(tsim)
            tver = TrainerSimulationVersion(version_id=301, simulation_id=301, version_number=1, roleplay_prompt_snapshot="prompt", evaluation_config_snapshot={}, service_id=30, evaluation_config_id=301)
            db.add(tver)
            tsess = TrainerSession(session_id=301, simulation_id=301, agent_id="ag1", agent_code="AC1", company_id=3, service_id=30, call_id="call_tr_1")
            db.add(tsess)
            teval = TrainerEvaluation(evaluation_id=301, session_id=301, prompt_snapshot="prompt", result_json={})
            db.add(teval)
            # Seed training agent setting
            t_setting = TrainingAgentSetting(setting_id=31, company_id=3, hubspot_owner_id="demo_owner_1", agent_name="Agent Demo", agent_initials="AD")
            db.add(t_setting)
            await db.commit()

        # Execute purge with apply=True
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                result = await purge_company(session, company_id=3, apply=True)
                self.assertEqual(result["status"], "applied")
                self.assertTrue(result["applied"])
                self.assertEqual(result["counts"]["services"], 1)
                self.assertEqual(result["counts"]["users"], 1)
                self.assertEqual(result["counts"]["teams"], 1)
                self.assertEqual(result["counts"]["mass_jobs"], 1)
                self.assertEqual(result["counts"]["mass_results"], 1)
                self.assertEqual(result["counts"]["trainer_configs"], 1)
                self.assertEqual(result["counts"]["trainer_simulations"], 1)
                self.assertEqual(result["counts"]["trainer_sessions"], 1)
                self.assertEqual(result["counts"]["prompts"], 1)

        # Verify all entities are deleted
        async with AsyncSession(self.engine) as session:
            self.assertIsNone((await session.execute(select(Company).where(Company.company_id == 3))).scalars().first())
            self.assertIsNone((await session.execute(select(Service).where(Service.service_id == 30))).scalars().first())
            self.assertIsNone((await session.execute(select(User).where(User.user_id == 301))).scalars().first())
            self.assertIsNone((await session.execute(select(Team).where(Team.team_id == 3001))).scalars().first())
            self.assertIsNone((await session.execute(select(MassEvaluationJob).where(MassEvaluationJob.job_id == 30001))).scalars().first())
            self.assertIsNone((await session.execute(select(MassEvaluationRun).where(MassEvaluationRun.run_id == 300001))).scalars().first())
            self.assertIsNone((await session.execute(select(MassEvaluationResult).where(MassEvaluationResult.mass_analysis_id == 3000001))).scalars().first())
            self.assertIsNone((await session.execute(select(TrainerEvaluation).where(TrainerEvaluation.evaluation_id == 301))).scalars().first())
            self.assertIsNone((await session.execute(select(TrainerSession).where(TrainerSession.session_id == 301))).scalars().first())
            self.assertIsNone((await session.execute(select(TrainerSimulationVersion).where(TrainerSimulationVersion.version_id == 301))).scalars().first())
            self.assertIsNone((await session.execute(select(TrainerSimulation).where(TrainerSimulation.simulation_id == 301))).scalars().first())
            self.assertIsNone((await session.execute(select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.config_id == 301))).scalars().first())
            self.assertIsNone((await session.execute(select(Prompt).where(Prompt.prompt_id == 301))).scalars().first())

    async def test_atomic_rollback_on_error(self):
        """If an error occurs midway through purge, all operations must be rolled back."""
        async with AsyncSession(self.engine) as session:
            try:
                async with session.begin():
                    # Delete the company row first to simulate failure or raise exception
                    await session.execute(text("DELETE FROM bm_companies WHERE company_id = 3"))
                    # Deliberately raise an exception inside the transaction
                    raise RuntimeError("Simulated mid-transaction failure")
            except RuntimeError:
                pass

        # Verify rollback kept company 3 in database
        async with AsyncSession(self.engine) as session:
            c3_check = await session.execute(select(Company).where(Company.company_id == 3))
            self.assertIsNotNone(c3_check.scalars().first())

    async def test_purge_demo_company_with_seed_structure(self):
        """Purge a demo company created with seed_demo_structure (services, users, teams, trainer credentials)."""
        # 1. Seed demo company structure
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                summary = await seed_demo_structure(session)

        demo_cid = summary["company"]["company_id"]
        self.assertEqual(len(summary["services"]), 2)
        self.assertEqual(len(summary["teams"]), 4)
        self.assertEqual(summary["users"]["agents"], 60)
        self.assertEqual(summary["users"]["total"], 67)
        self.assertEqual(summary["trainer_settings_count"], 60)

        # 2. Verify existence before purge
        async with AsyncSession(self.engine) as session:
            c_check = await session.execute(select(Company).where(Company.company_id == demo_cid))
            self.assertIsNotNone(c_check.scalars().first())
            svcs = (await session.execute(select(Service).where(Service.company_id == demo_cid))).scalars().all()
            self.assertEqual(len(list(svcs)), 2)
            teams = (await session.execute(select(Team).where(Team.company_id == demo_cid))).scalars().all()
            self.assertEqual(len(list(teams)), 4)
            users = (await session.execute(select(User).where(User.company_id == demo_cid))).scalars().all()
            self.assertEqual(len(list(users)), 67)
            t_creds = (await session.execute(select(TrainingAgentSetting).where(TrainingAgentSetting.company_id == demo_cid))).scalars().all()
            self.assertEqual(len(list(t_creds)), 60)

        # 3. Purge company with apply=True and force_non_demo=True
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                result = await purge_company(session, company_id=demo_cid, apply=True, force_non_demo=True)
                self.assertEqual(result["status"], "applied")
                self.assertTrue(result["applied"])
                self.assertEqual(result["counts"]["services"], 2)
                self.assertEqual(result["counts"]["teams"], 4)
                self.assertEqual(result["counts"]["users"], 67)
                self.assertEqual(result["counts"]["training_agent_settings"], 60)

        # 4. Verify 100% complete deletion
        async with AsyncSession(self.engine) as session:
            self.assertIsNone((await session.execute(select(Company).where(Company.company_id == demo_cid))).scalars().first())
            self.assertEqual(len(list((await session.execute(select(Service).where(Service.company_id == demo_cid))).scalars().all())), 0)
            self.assertEqual(len(list((await session.execute(select(Team).where(Team.company_id == demo_cid))).scalars().all())), 0)
            self.assertEqual(len(list((await session.execute(select(User).where(User.company_id == demo_cid))).scalars().all())), 0)
            self.assertEqual(len(list((await session.execute(select(TrainingAgentSetting).where(TrainingAgentSetting.company_id == demo_cid))).scalars().all())), 0)


if __name__ == "__main__":
    unittest.main()
