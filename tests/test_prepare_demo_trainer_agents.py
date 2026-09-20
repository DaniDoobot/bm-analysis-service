import os
import unittest
from datetime import datetime, timezone, timedelta

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_prepare_demo_agents.db"

from sqlalchemy import BigInteger, select, delete, func
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

@compiles(BigInteger, "sqlite")
def compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"

from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.users import User
from app.models.personalized_training import (
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
    TrainingAgentSetting,
)
from scripts.prepare_demo_trainer_agents import (
    prepare_demo_agents,
    TARGET_AGENTS,
    DEMO_COMPANY_ID,
)


class TestPrepareDemoTrainerAgents(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with AsyncSession(self.engine) as db:
            # Clean test fixtures
            await db.execute(delete(TrainingCompletionStatus))
            await db.execute(delete(TrainingSimulationPrompt))
            await db.execute(delete(TrainingAgentReport))
            await db.execute(delete(TrainingAgentSetting))
            await db.execute(delete(TrainingRun))
            await db.execute(delete(User).where(User.company_id == DEMO_COMPANY_ID))
            await db.execute(delete(Service).where(Service.company_id == DEMO_COMPANY_ID))
            await db.execute(delete(Company).where(Company.company_id == DEMO_COMPANY_ID))
            await db.commit()

            # 1. Company
            demo_company = Company(
                company_id=DEMO_COMPANY_ID,
                company_key="empresa-demo",
                company_name="Empresa Demo",
                is_demo=True,
            )
            db.add(demo_company)
            await db.flush()

            # 2. Services
            svc_at = Service(service_id=701, company_id=DEMO_COMPANY_ID, service_key="atencion-al-cliente", service_name="Atención al Cliente")
            svc_vn = Service(service_id=702, company_id=DEMO_COMPANY_ID, service_key="ventas", service_name="Ventas")
            db.add_all([svc_at, svc_vn])
            await db.flush()

            # 3. Users for the 4 target agents
            now_utc = datetime.now(timezone.utc)
            users = []
            for oid, spec in TARGET_AGENTS.items():
                s_id = 701 if spec["service_key"] == "atencion-al-cliente" else 702
                u = User(
                    user_id=7100 + spec["index"],
                    company_id=DEMO_COMPANY_ID,
                    username=f"agente.demo.{spec['index']:02d}",
                    email=f"agente.demo.{spec['index']:02d}@doobot.ai",
                    name=spec["name"],
                    role="agent",
                    hubspot_owner_id=oid,
                    agent_initials=spec["code"],
                    primary_service_id=s_id,
                    is_active=True,
                    password_hash="fakehash",
                )
                users.append(u)
            db.add_all(users)
            await db.flush()

            # 4. Historical Run 1 with completed reports
            run1 = TrainingRun(
                training_run_id=101,
                company_id=DEMO_COMPANY_ID,
                period_start=now_utc - timedelta(days=60),
                period_end=now_utc - timedelta(days=45),
                status="completed",
                agents_total=4,
                agents_completed=4,
            )
            db.add(run1)
            await db.flush()

            for oid, spec in TARGET_AGENTS.items():
                s_id = 701 if spec["service_key"] == "atencion-al-cliente" else 702
                rep = TrainingAgentReport(
                    training_report_id=2000 + spec["index"],
                    training_run_id=101,
                    company_id=DEMO_COMPANY_ID,
                    service_id=s_id,
                    hubspot_owner_id=oid,
                    agent_name=spec["name"],
                    agent_initials=spec["code"],
                    period_start=now_utc - timedelta(days=60),
                    period_end=now_utc - timedelta(days=45),
                    status="completed",
                    is_current=True,
                    final_report_json={"summary": "historical"},
                )
                db.add(rep)
                await db.flush()

                # Historical simulation prompt & completed status
                p = TrainingSimulationPrompt(
                    simulation_prompt_id=3000 + spec["index"],
                    training_report_id=rep.training_report_id,
                    hubspot_owner_id=oid,
                    prompt_number=1,
                    title="Historical Prompt",
                    scenario_type="roleplay",
                    prompt_text="Historical text",
                )
                db.add(p)
                await db.flush()

                comp = TrainingCompletionStatus(
                    completion_id=4000 + spec["index"],
                    training_report_id=rep.training_report_id,
                    simulation_prompt_id=p.simulation_prompt_id,
                    hubspot_owner_id=oid,
                    status="completed",
                )
                db.add(comp)

                # Existing setting with random PIN
                setting = TrainingAgentSetting(
                    company_id=DEMO_COMPANY_ID,
                    hubspot_owner_id=oid,
                    agent_name=spec["name"],
                    agent_initials=spec["code"],
                    training_code=f"AG{spec['index']:02d}",
                    training_numeric_code=f"9{spec['index']:03d}",
                    training_code_enabled=True,
                )
                db.add(setting)

            await db.commit()

    async def test_dry_run_makes_no_database_changes(self):
        async with AsyncSession(self.engine) as db:
            stats = await prepare_demo_agents(db, apply=False)

            self.assertEqual(stats["target_agents"], 4)
            self.assertEqual(stats["historical_cycles_found"], 4)
            self.assertEqual(stats["active_cycles_existing_before"], 0)
            self.assertEqual(stats["new_cycles_created"], 4)
            self.assertEqual(stats["prompts_created"], 12)
            self.assertEqual(stats["completions_created"], 12)
            self.assertEqual(stats["pins_updated"], 4)

            # Assert database has NOT changed: still only 4 reports, all completed
            res_rep = await db.execute(select(TrainingAgentReport))
            reports = list(res_rep.scalars().all())
            self.assertEqual(len(reports), 4)
            for r in reports:
                self.assertEqual(r.status, "completed")
                self.assertTrue(r.is_current)

    async def test_apply_creates_active_cycles_and_preserves_history(self):
        async with AsyncSession(self.engine) as db:
            stats = await prepare_demo_agents(db, apply=True)

            self.assertEqual(stats["new_cycles_created"], 4)
            self.assertEqual(stats["prompts_created"], 12)
            self.assertEqual(stats["completions_created"], 12)
            self.assertEqual(stats["pins_updated"], 4)

            # Verify for each target agent
            for oid, spec in TARGET_AGENTS.items():
                # 1. Historical report still exists, completed, is_current=False
                stmt_hist = select(TrainingAgentReport).where(
                    TrainingAgentReport.hubspot_owner_id == oid,
                    TrainingAgentReport.status == "completed"
                )
                res_hist = await db.execute(stmt_hist)
                hist = res_hist.scalars().first()
                self.assertIsNotNone(hist)
                self.assertFalse(hist.is_current)
                self.assertEqual(hist.final_report_json, {"summary": "historical"})

                # 2. New report exists, in_progress, is_current=True, final_report_json is None
                stmt_act = select(TrainingAgentReport).where(
                    TrainingAgentReport.hubspot_owner_id == oid,
                    TrainingAgentReport.status == "in_progress"
                )
                res_act = await db.execute(stmt_act)
                act = res_act.scalars().first()
                self.assertIsNotNone(act)
                self.assertTrue(act.is_current)
                self.assertIsNone(act.final_report_json)

                # 3. 3 prompts created for this new report
                stmt_p = select(TrainingSimulationPrompt).where(
                    TrainingSimulationPrompt.training_report_id == act.training_report_id
                )
                res_p = await db.execute(stmt_p)
                prompts = list(res_p.scalars().all())
                self.assertEqual(len(prompts), 3)

                # 4. 3 pending completion statuses created
                stmt_c = select(TrainingCompletionStatus).where(
                    TrainingCompletionStatus.training_report_id == act.training_report_id
                )
                res_c = await db.execute(stmt_c)
                comps = list(res_c.scalars().all())
                self.assertEqual(len(comps), 3)
                for c in comps:
                    self.assertEqual(c.status, "pending")
                    self.assertIsNone(c.evaluation_id)
                    self.assertIsNone(c.call_session_id)

                # 5. PIN updated to exactly target string
                stmt_s = select(TrainingAgentSetting).where(
                    TrainingAgentSetting.hubspot_owner_id == oid
                )
                res_s = await db.execute(stmt_s)
                setting = res_s.scalars().first()
                self.assertEqual(setting.training_numeric_code, spec["pin"])
                self.assertTrue(setting.training_code_enabled)

    async def test_idempotency_second_run_does_not_duplicate(self):
        async with AsyncSession(self.engine) as db:
            # First run: apply
            stats1 = await prepare_demo_agents(db, apply=True)
            self.assertEqual(stats1["new_cycles_created"], 4)

            # Second run: apply again
            stats2 = await prepare_demo_agents(db, apply=True)
            self.assertEqual(stats2["new_cycles_created"], 0)
            self.assertEqual(stats2["agents_already_prepared"], 4)
            self.assertEqual(stats2["pins_already_matching"], 4)
            self.assertEqual(stats2["pins_updated"], 0)

            # Total reports per agent should still be exactly 2 (1 completed + 1 in_progress)
            for oid in TARGET_AGENTS:
                res_cnt = await db.execute(
                    select(func.count(TrainingAgentReport.training_report_id)).where(
                        TrainingAgentReport.hubspot_owner_id == oid
                    )
                )
                self.assertEqual(res_cnt.scalar(), 2)

    async def test_portal_and_phone_runtime_contracts(self):
        """Verify that get_agent_detail (portal) and get_active_cycles_for_agent (phone) work as expected."""
        from app.services.personalized_training_service import PersonalizedTrainingService
        from app.routers.training_voice import get_active_cycles_for_agent

        async with AsyncSession(self.engine) as db:
            await prepare_demo_agents(db, apply=True)

            for oid, spec in TARGET_AGENTS.items():
                # ── 1. Portal Contract (get_agent_detail) ────────────────────
                detail = await PersonalizedTrainingService.get_agent_detail(db, hubspot_owner_id=oid)
                self.assertIsNotNone(detail)
                curr = detail.get("current_report")
                self.assertIsNotNone(curr)

                # Must pick the NEW in_progress report, NOT the completed one
                self.assertEqual(curr["status"], "in_progress")
                self.assertNotEqual(curr["training_report_id"], 2000 + spec["index"])
                self.assertIsNone(curr.get("final_report_json"))

                # Check simulation slots: exactly 3, all pending
                slots = curr.get("simulations", [])
                self.assertEqual(len(slots), 3)
                for slot in slots:
                    self.assertEqual(slot["status"], "pending")
                    self.assertIsNone(slot["score"])
                    self.assertIsNone(slot["evaluation_id"])
                    self.assertIsNone(slot["call_session_id"])

                # History must still contain the completed cycle
                hist = detail.get("history", [])
                self.assertTrue(any(h["status"] == "completed" for h in hist))

                # ── 2. Phone Contract (get_active_cycles_for_agent) ───────────
                active_cycles = await get_active_cycles_for_agent(db, oid)
                self.assertEqual(len(active_cycles), 1)
                self.assertEqual(active_cycles[0].training_report_id, curr["training_report_id"])
                self.assertEqual(active_cycles[0].status, "in_progress")

                # ── 3. Phone Identification Contract (PIN lookup) ────────────
                stmt_pin = select(TrainingAgentSetting).where(
                    TrainingAgentSetting.training_numeric_code == spec["pin"],
                    TrainingAgentSetting.training_code_enabled == True,
                )
                res_pin = await db.execute(stmt_pin)
                setting = res_pin.scalars().first()
                self.assertIsNotNone(setting)
                self.assertEqual(setting.hubspot_owner_id, oid)

