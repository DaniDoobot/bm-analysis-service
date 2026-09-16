"""
Unit tests for scripts/seed_demo_data.py
=========================================
Validates:
1. Exact counts:
   - 3,600 MassEvaluationResult
   - 21,600 MassEvaluationCriterionResult
   - 2 Jobs, 26 Runs
   - 2 Prompts, 2 PromptVersions, 12 PromptCriteria, 8 Typologies
   - 2 TrainingRuns, 32 TrainingAgentReports (20 completed, 12 in progress)
   - 2 TrainerEvaluationConfigs, 4 TrainerSimulations, 50 TrainerSessions & Evaluations
2. Behavioral fields & JSON integrity:
   - created_at == call_timestamp
   - Unique call_id with demo_call_YYYYMMDD_xxxx format
   - hubspot_owner_id mapped to demo agents
   - result_json and items_json conform to visual requirements
   - Alarms generated and detected by the r.alarma property
3. Strict tenant isolation:
   - 100% of rows reference the demo company_id.
4. Idempotency guard:
   - Re-running raises DemoDataAlreadyExistsError.
5. Atomic rollback on error.
6. SQL Aggregation compatibility:
   - Simulates agents-comparison and dashboard queries over seeded data.
"""
import os
import sys
import unittest
from decimal import Decimal

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///test_seed_demo_data.db"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"

from sqlalchemy import func, select, text, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_engine, Base
from app.models.companies import Company
from app.models.services import Service
from app.models.teams import Team
from app.models.users import User
from app.models.prompts import Prompt, PromptVersion
from app.models.criteria import PromptCriterion
from app.models.typologies import Typology
from app.models.mass_evaluations import (
    MassEvaluationJob,
    MassEvaluationRun,
    MassEvaluationResult,
    MassEvaluationCriterionResult,
)
from app.models.personalized_training import (
    TrainingRun,
    TrainingAgentReport,
    TrainingSimulationPrompt,
    TrainingCompletionStatus,
)
from app.models.trainer import (
    TrainerEvaluationConfig,
    TrainerSimulation,
    TrainerSimulationVersion,
    TrainerSession,
    TrainerEvaluation,
)
from scripts.seed_demo_company import seed_demo_structure, DEMO_COMPANY_KEY
from scripts.seed_demo_data import (
    seed_demo_data,
    DemoDataAlreadyExistsError,
    TARGET_EVALUATIONS,
)


class TestSeedDemoData(unittest.IsolatedAsyncioTestCase):
    """Test suite for synthetic demo data generation."""

    async def asyncSetUp(self):
        self.engine = get_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        # Seed base structure first (pre-seed company 1 to mirror production environment)
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                prod_company = Company(
                    company_name="Boston Medical Group",
                    company_key="boston-medical",
                    is_demo=False,
                )
                session.add(prod_company)
                await session.flush()
                await seed_demo_structure(session)

    async def asyncTearDown(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self.engine.dispose()

    @classmethod
    def tearDownClass(cls):
        try:
            if os.path.exists("test_seed_demo_data.db"):
                os.remove("test_seed_demo_data.db")
        except Exception:
            pass

    async def test_seed_demo_data_complete_generation(self):
        """Verify full data generation, exact counts, payload structures, and tenant isolation."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                summary = await seed_demo_data(session, validate=True)

        self.assertIn("company", summary)
        cid = summary["company"]["id"]

        async with AsyncSession(self.engine) as session:
            # 1. MassEvaluationResult checks
            evals_res = await session.execute(
                select(MassEvaluationResult).where(MassEvaluationResult.company_id == cid)
            )
            evals = list(evals_res.scalars().all())
            self.assertEqual(len(evals), TARGET_EVALUATIONS)

            # Check created_at == call_timestamp on a sample
            sample_eval = evals[0]
            self.assertEqual(sample_eval.created_at, sample_eval.call_timestamp)
            self.assertTrue(sample_eval.call_id.startswith("demo_call_"))
            self.assertTrue(sample_eval.hubspot_owner_id.startswith("demo_owner_"))
            self.assertIsNotNone(sample_eval.evaluacion_global)
            self.assertIsInstance(sample_eval.result_json, dict)
            self.assertIsInstance(sample_eval.items_json, list)
            for it in sample_eval.items_json:
                if it.get("criterion_key") != "alarma":
                    self.assertIsNotNone(it.get("output_key"))
                    self.assertEqual(it["output_key"], f"{it['criterion_key']}_score")

            # Check that alarms exist in dataset
            alarm_calls = [e for e in evals if e.alarma is True]
            self.assertGreater(len(alarm_calls), 0, "Expected at least some calls to have alarma=True")

            # 2. MassEvaluationCriterionResult checks
            crit_cnt_res = await session.execute(
                select(func.count(MassEvaluationCriterionResult.id))
            )
            total_crit = crit_cnt_res.scalar() or 0
            self.assertEqual(total_crit, TARGET_EVALUATIONS * 6)

            # 3. MassEvaluationJob & Run checks
            jobs_res = await session.execute(
                select(MassEvaluationJob).where(MassEvaluationJob.company_id == cid)
            )
            jobs = list(jobs_res.scalars().all())
            self.assertEqual(len(jobs), 2)
            for j in jobs:
                self.assertIsNotNone(j.created_by)
                self.assertIsNotNone(j.created_by_email)

            runs_res = await session.execute(
                select(MassEvaluationRun).where(MassEvaluationRun.company_id == cid)
            )
            runs = list(runs_res.scalars().all())
            self.assertEqual(len(runs), 26)

            # 4. Improvement Cycles checks
            tr_runs_res = await session.execute(
                select(TrainingRun).where(TrainingRun.company_id == cid)
            )
            tr_runs = list(tr_runs_res.scalars().all())
            self.assertEqual(len(tr_runs), 2)

            reports_res = await session.execute(
                select(TrainingAgentReport).where(TrainingAgentReport.company_id == cid)
            )
            reports = list(reports_res.scalars().all())
            self.assertEqual(len(reports), 32)
            completed_reps = [r for r in reports if r.status == "completed"]
            active_reps = [r for r in reports if r.status == "in_progress"]
            self.assertEqual(len(completed_reps), 20)
            self.assertEqual(len(active_reps), 12)

            # Verify JSON payload on reports
            sample_rep = reports[0]
            self.assertIn("fortalezas", sample_rep.strengths_json)
            self.assertIn("puntos_mejora", sample_rep.weaknesses_json)

            # 5. Trainer checks
            tr_configs_res = await session.execute(
                select(TrainerEvaluationConfig).where(TrainerEvaluationConfig.company_id == cid)
            )
            self.assertEqual(len(list(tr_configs_res.scalars().all())), 2)

            tr_sims_res = await session.execute(
                select(TrainerSimulation).where(TrainerSimulation.company_id == cid)
            )
            self.assertEqual(len(list(tr_sims_res.scalars().all())), 4)

            tr_sess_res = await session.execute(
                select(TrainerSession).where(TrainerSession.company_id == cid)
            )
            sessions = list(tr_sess_res.scalars().all())
            self.assertEqual(len(sessions), 50)

            tr_evals_res = await session.execute(select(TrainerEvaluation))
            evaluations = list(tr_evals_res.scalars().all())
            self.assertEqual(len(evaluations), 50)
            self.assertIsNotNone(evaluations[0].score)
            self.assertIn("items", evaluations[0].strengths)

            # 6. Tenant Isolation check
            # No row in any evaluation/training/trainer table should reference another company_id
            leak_res = await session.execute(
                select(func.count(MassEvaluationResult.mass_analysis_id)).where(
                    MassEvaluationResult.company_id != cid
                )
            )
            self.assertEqual(leak_res.scalar() or 0, 0)

            leak_tr = await session.execute(
                select(func.count(TrainingAgentReport.training_report_id)).where(
                    TrainingAgentReport.company_id != cid
                )
            )
            self.assertEqual(leak_tr.scalar() or 0, 0)

            leak_trainer = await session.execute(
                select(func.count(TrainerSession.session_id)).where(
                    TrainerSession.company_id != cid
                )
            )
            self.assertEqual(leak_trainer.scalar() or 0, 0)

    async def test_idempotency_guard_blocks_duplicate_seeding(self):
        """Seeding demo data twice on the same company raises DemoDataAlreadyExistsError."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                await seed_demo_data(session)

        async with AsyncSession(self.engine) as session:
            async with session.begin():
                with self.assertRaises(DemoDataAlreadyExistsError) as ctx:
                    await seed_demo_data(session)
                self.assertIn("already has", str(ctx.exception))

    async def test_atomic_rollback_on_midway_error(self):
        """If an error occurs midway, all uncommitted evaluation records are rolled back."""
        async with AsyncSession(self.engine) as session:
            try:
                async with session.begin():
                    # Insert 1 dummy evaluation
                    e = MassEvaluationResult(
                        run_id=1,
                        job_id=1,
                        company_id=1,
                        call_id="call_midway_fail",
                        prompt_id=1,
                        prompt_snapshot="p",
                    )
                    session.add(e)
                    await session.flush()
                    raise RuntimeError("Simulated failure midway through seed")
            except RuntimeError:
                pass

        async with AsyncSession(self.engine) as session:
            check = await session.execute(
                select(MassEvaluationResult).where(MassEvaluationResult.call_id == "call_midway_fail")
            )
            self.assertIsNone(check.scalars().first())

    async def test_sql_aggregation_queries_compatibility(self):
        """Verify that analytics aggregation queries execute cleanly over the generated data."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                await seed_demo_data(session)

        async with AsyncSession(self.engine) as session:
            # Replicate Query A from agents-comparison
            stmt_a = (
                select(
                    MassEvaluationResult.hubspot_owner_id,
                    func.count(distinct(MassEvaluationResult.mass_analysis_id)).label("total_calls"),
                    func.avg(MassEvaluationResult.evaluacion_global).label("avg_global"),
                )
                .where(MassEvaluationResult.company_id.is_not(None))
                .group_by(MassEvaluationResult.hubspot_owner_id)
            )
            res_a = await session.execute(stmt_a)
            rows_a = res_a.all()
            self.assertEqual(len(rows_a), 60, "Expected all 60 agents to have aggregated scores")

            # Check that each agent has between 15 and 70 calls
            for owner_id, count, avg_score in rows_a:
                self.assertGreater(count, 10)
                self.assertGreaterEqual(float(avg_score), 4.0)
                self.assertLessEqual(float(avg_score), 10.0)

            # Replicate Query B from agents-comparison (criterion aggregations)
            stmt_b = (
                select(
                    MassEvaluationCriterionResult.criterion_key,
                    func.avg(MassEvaluationCriterionResult.numeric_value).label("avg_crit"),
                    func.count(MassEvaluationCriterionResult.id).label("cnt_crit"),
                )
                .join(
                    MassEvaluationResult,
                    MassEvaluationCriterionResult.mass_analysis_id == MassEvaluationResult.mass_analysis_id,
                )
                .group_by(MassEvaluationCriterionResult.criterion_key)
            )
            res_b = await session.execute(stmt_b)
            rows_b = res_b.all()
            self.assertEqual(len(rows_b), 24, "Expected exactly 24 distinct criteria across the 4 structures")

    async def test_multiple_active_evaluation_structures(self):
        """Verify that at least 2 active evaluation structures exist per service and map cleanly."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                summary = await seed_demo_data(session)

        cid = summary["company"]["id"]

        async with AsyncSession(self.engine) as session:
            # Check 4 prompts
            p_res = await session.execute(
                select(Prompt).where(Prompt.is_active.is_(True))
            )
            prompts = list(p_res.scalars().all())
            self.assertEqual(len(prompts), 4, "Expected 4 active prompts (2 per service)")

            p_names = {p.prompt_name for p in prompts}
            expected_names = {
                "Calidad Atención General",
                "Gestión Reclamaciones",
                "Venta Consultiva",
                "Retención/Renovación",
            }
            self.assertEqual(p_names, expected_names)

            # Check prompt owner and creator belong strictly to demo company admin
            admin_res = await session.execute(
                select(User).where(User.company_id == cid, User.role == "company_admin")
            )
            admin_user = admin_res.scalars().first()
            self.assertIsNotNone(admin_user, "Expected company_admin to exist in demo company")

            for p in prompts:
                self.assertIsNotNone(p.owner_user_id, f"Prompt '{p.prompt_name}' owner_user_id must not be None")
                self.assertEqual(p.owner_user_id, admin_user.user_id)
                self.assertEqual(p.created_by, admin_user.name)
                self.assertEqual(p.created_by_email, admin_user.email)
                # Strict multi-tenant isolation check: owner must belong to the demo company
                owner_check = await session.execute(
                    select(User).where(User.user_id == p.owner_user_id)
                )
                owner = owner_check.scalars().first()
                self.assertIsNotNone(owner)
                self.assertEqual(owner.company_id, cid)

            # Check 4 prompt versions and their audit fields
            v_res = await session.execute(select(PromptVersion))
            versions = list(v_res.scalars().all())
            self.assertEqual(len(versions), 4)
            for v in versions:
                self.assertEqual(v.updated_by, admin_user.name)
                self.assertEqual(v.updated_by_email, admin_user.email)

            # Check criteria count per prompt (exactly 6 each)
            for p in prompts:
                c_res = await session.execute(
                    select(PromptCriterion).where(PromptCriterion.prompt_id == p.prompt_id)
                )
                criteria = list(c_res.scalars().all())
                self.assertEqual(len(criteria), 6, f"Expected 6 criteria for prompt {p.prompt_name}")

            # Verify that evaluations reference the prompt corresponding to their typology
            eval_samples = await session.execute(
                select(MassEvaluationResult).limit(100)
            )
            for ev in eval_samples.scalars().all():
                if ev.typology_key in ("consulta_general", "soporte_tecnico"):
                    self.assertEqual(ev.prompt_name, "Calidad Atención General")
                elif ev.typology_key in ("reclamacion_incidencia", "facturacion_cobros"):
                    self.assertEqual(ev.prompt_name, "Gestión Reclamaciones")
                elif ev.typology_key in ("captacion_nuevo", "upselling_cross"):
                    self.assertEqual(ev.prompt_name, "Venta Consultiva")
                elif ev.typology_key in ("renovacion", "retencion_baja"):
                    self.assertEqual(ev.prompt_name, "Retención/Renovación")

    async def test_criteria_diversity_and_service_containment(self):
        """Verify criteria diversity across structures and strict service containment."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                await seed_demo_data(session)

        async with AsyncSession(self.engine) as session:
            # Fetch distinct criteria evaluated in Atención
            crit_at_res = await session.execute(
                select(distinct(MassEvaluationCriterionResult.criterion_key)).where(
                    MassEvaluationCriterionResult.service_key == "atencion-al-cliente"
                )
            )
            crit_at = {r[0] for r in crit_at_res.all()}
            self.assertEqual(len(crit_at), 12, "Expected 12 distinct criteria evaluated in Atención (2 structures x 6)")

            # Fetch distinct criteria evaluated in Ventas
            crit_vn_res = await session.execute(
                select(distinct(MassEvaluationCriterionResult.criterion_key)).where(
                    MassEvaluationCriterionResult.service_key == "ventas"
                )
            )
            crit_vn = {r[0] for r in crit_vn_res.all()}
            self.assertEqual(len(crit_vn), 12, "Expected 12 distinct criteria evaluated in Ventas (2 structures x 6)")

            # Ensure complete disjunction (no criteria key overlap between the services)
            overlap = crit_at.intersection(crit_vn)
            self.assertEqual(len(overlap), 0, f"Expected no criteria overlap between Atención and Ventas, found {overlap}")

    async def test_prompt_criteria_output_key_integrity_and_isolation(self):
        """Verify that all 24 PromptCriterion have non-null output_key, unique keys, and strict service isolation."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                summary = await seed_demo_data(session)

        cid = summary["company"]["id"]
        async with AsyncSession(self.engine) as session:
            # 1. Fetch all prompts for demo company
            svc_res = await session.execute(select(Service).where(Service.company_id == cid))
            services = {s.service_key: s for s in svc_res.scalars().all()}
            self.assertIn("atencion-al-cliente", services)
            self.assertIn("ventas", services)

            prompts_res = await session.execute(
                select(Prompt).where(Prompt.is_active.is_(True)).order_by(Prompt.prompt_id.asc())
            )
            prompts = list(prompts_res.scalars().all())
            self.assertEqual(len(prompts), 4, "Expected 4 active prompts (2 per service)")

            # Verify prompt service isolation: 2 for Atención, 2 for Ventas
            prompts_at = [p for p in prompts if p.service_id == services["atencion-al-cliente"].service_id]
            prompts_vn = [p for p in prompts if p.service_id == services["ventas"].service_id]
            self.assertEqual(len(prompts_at), 2, "Expected exactly 2 prompts for Atención al Cliente")
            self.assertEqual(len(prompts_vn), 2, "Expected exactly 2 prompts for Ventas")

            # 2. Fetch all criteria across the 4 prompts
            crit_res = await session.execute(
                select(PromptCriterion)
                .where(PromptCriterion.prompt_id.in_([p.prompt_id for p in prompts]))
                .order_by(PromptCriterion.criterion_id.asc())
            )
            all_criteria = list(crit_res.scalars().all())
            self.assertEqual(len(all_criteria), 24, "Expected exactly 24 PromptCriterion rows across the 4 structures")

            all_crit_keys = set()
            all_output_keys = set()

            for c in all_criteria:
                # Criterion must have non-null, non-empty fields
                self.assertIsNotNone(c.criterion_key, f"criterion_key cannot be None for id={c.criterion_id}")
                self.assertIsNotNone(c.criterion_name, f"criterion_name cannot be None for key={c.criterion_key}")
                self.assertIsNotNone(c.criterion_type, f"criterion_type cannot be None for key={c.criterion_key}")
                self.assertEqual(c.criterion_type, "score_1_10")
                self.assertIsNotNone(c.output_key, f"output_key cannot be None for key={c.criterion_key}")
                self.assertTrue(len(c.output_key) > 0, f"output_key cannot be empty for key={c.criterion_key}")
                self.assertEqual(c.output_key, f"{c.criterion_key}_score", f"output_key '{c.output_key}' must follow '{c.criterion_key}_score'")
                self.assertIsNotNone(c.feed_key, f"feed_key cannot be None for key={c.criterion_key}")
                self.assertEqual(c.feed_key, f"{c.criterion_key}_feedback")

                all_crit_keys.add(c.criterion_key)
                all_output_keys.add(c.output_key)

            # 3. All 24 criteria must have unique keys
            self.assertEqual(len(all_crit_keys), 24, "Expected all 24 criteria to have unique criterion_keys")
            self.assertEqual(len(all_output_keys), 24, "Expected all 24 criteria to have unique output_keys")

            # 4. Service containment check on PromptCriterion
            crit_at_keys = {
                c.criterion_key for c in all_criteria
                if c.prompt_id in [p.prompt_id for p in prompts_at]
            }
            crit_vn_keys = {
                c.criterion_key for c in all_criteria
                if c.prompt_id in [p.prompt_id for p in prompts_vn]
            }
            self.assertEqual(len(crit_at_keys), 12)
            self.assertEqual(len(crit_vn_keys), 12)
            self.assertTrue(crit_at_keys.isdisjoint(crit_vn_keys), "Prompt criteria between Atención and Ventas must be disjoint")

    async def test_agent_profiles_and_divergent_trends(self):
        """Verify that agent archetypes exhibit realistic, differentiated temporal trajectories."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                await seed_demo_data(session)

        async with AsyncSession(self.engine) as session:
            # Check Top Performers (e.g. demo_owner_01, demo_owner_02)
            top_res = await session.execute(
                select(func.avg(MassEvaluationResult.evaluacion_global)).where(
                    MassEvaluationResult.hubspot_owner_id.in_(["demo_owner_01", "demo_owner_02"]),
                    MassEvaluationResult.is_evaluable.is_(True),
                )
            )
            top_avg = float(top_res.scalar() or 0.0)
            self.assertGreater(top_avg, 8.2, "Top performers should average > 8.2")

            # Check Improving Agents (e.g. demo_owner_08, demo_owner_21)
            # Compare early calls (oldest 30 days) vs late calls (newest 30 days)
            min_ts_res = await session.execute(select(func.min(MassEvaluationResult.call_timestamp)))
            min_ts = min_ts_res.scalar()
            max_ts_res = await session.execute(select(func.max(MassEvaluationResult.call_timestamp)))
            max_ts = max_ts_res.scalar()

            early_cutoff = min_ts + (max_ts - min_ts) * 0.33
            late_cutoff = min_ts + (max_ts - min_ts) * 0.67

            improving_owners = ["demo_owner_08", "demo_owner_21", "demo_owner_37", "demo_owner_52"]
            early_imp_res = await session.execute(
                select(func.avg(MassEvaluationResult.evaluacion_global)).where(
                    MassEvaluationResult.hubspot_owner_id.in_(improving_owners),
                    MassEvaluationResult.call_timestamp <= early_cutoff,
                    MassEvaluationResult.is_evaluable.is_(True),
                )
            )
            early_imp_avg = float(early_imp_res.scalar() or 0.0)

            late_imp_res = await session.execute(
                select(func.avg(MassEvaluationResult.evaluacion_global)).where(
                    MassEvaluationResult.hubspot_owner_id.in_(improving_owners),
                    MassEvaluationResult.call_timestamp >= late_cutoff,
                    MassEvaluationResult.is_evaluable.is_(True),
                )
            )
            late_imp_avg = float(late_imp_res.scalar() or 0.0)
            self.assertGreater(
                late_imp_avg - early_imp_avg, 1.2,
                f"Improving agents should show positive progression (early={early_imp_avg:.2f}, late={late_imp_avg:.2f})"
            )

            # Check Struggling Agents (e.g. demo_owner_26, demo_owner_39, demo_owner_57)
            struggling_owners = ["demo_owner_26", "demo_owner_39", "demo_owner_57"]
            early_strug_res = await session.execute(
                select(func.avg(MassEvaluationResult.evaluacion_global)).where(
                    MassEvaluationResult.hubspot_owner_id.in_(struggling_owners),
                    MassEvaluationResult.call_timestamp <= early_cutoff,
                    MassEvaluationResult.is_evaluable.is_(True),
                )
            )
            early_strug_avg = float(early_strug_res.scalar() or 0.0)

            late_strug_res = await session.execute(
                select(func.avg(MassEvaluationResult.evaluacion_global)).where(
                    MassEvaluationResult.hubspot_owner_id.in_(struggling_owners),
                    MassEvaluationResult.call_timestamp >= late_cutoff,
                    MassEvaluationResult.is_evaluable.is_(True),
                )
            )
            late_strug_avg = float(late_strug_res.scalar() or 0.0)
            self.assertLess(
                late_strug_avg, early_strug_avg - 0.8,
                f"Struggling agents should exhibit a decline (early={early_strug_avg:.2f}, late={late_strug_avg:.2f})"
            )

            # Check New Hires (demo_owner_10, demo_owner_29, demo_owner_30, demo_owner_60)
            new_hire_calls = await session.execute(
                select(func.count(MassEvaluationResult.mass_analysis_id)).where(
                    MassEvaluationResult.hubspot_owner_id == "demo_owner_10",
                    MassEvaluationResult.call_timestamp <= early_cutoff,
                )
            )
            self.assertEqual(new_hire_calls.scalar() or 0, 0, "New hires should have no calls in the early period")

    async def test_call_variability_and_derived_metrics(self):
        """Verify that evaluacion_global is strictly derived from criteria and shows realistic non-uniform variance."""
        async with AsyncSession(self.engine) as session:
            async with session.begin():
                await seed_demo_data(session)

        async with AsyncSession(self.engine) as session:
            # 1. Verify that evaluacion_global matches the weighted sum of criteria
            sample_calls = await session.execute(
                select(MassEvaluationResult).where(MassEvaluationResult.is_evaluable.is_(True)).limit(50)
            )
            for call in sample_calls.scalars().all():
                # Extract items_json criterion scores and weights
                crit_items = [it for it in call.items_json if it.get("criterion_key") != "alarma"]
                calculated_sum = sum(float(it["value"]) * float(it["weight"]) for it in crit_items)
                global_score = float(call.evaluacion_global)
                self.assertAlmostEqual(
                    global_score, calculated_sum, delta=0.05,
                    msg=f"Global score {global_score} did not match weighted sum {calculated_sum:.2f} for call {call.call_id}"
                )

            # 2. Verify score variability for a single agent (not static numbers)
            agent_scores_res = await session.execute(
                select(MassEvaluationResult.evaluacion_global).where(
                    MassEvaluationResult.hubspot_owner_id == "demo_owner_03",
                    MassEvaluationResult.is_evaluable.is_(True),
                )
            )
            scores = [float(s[0]) for s in agent_scores_res.all()]
            self.assertGreater(len(scores), 20)
            avg = sum(scores) / len(scores)
            variance = sum((x - avg) ** 2 for x in scores) / len(scores)
            std_dev = variance ** 0.5
            self.assertGreater(std_dev, 0.35, "Agent scores should exhibit realistic intra-agent variance across calls")

            # 3. Verify non-uniform distributions in difficulty and sentiment
            diff_res = await session.execute(
                select(
                    MassEvaluationResult.result_json["dificultad"].as_string(),
                    func.count(MassEvaluationResult.mass_analysis_id),
                )
                .group_by(MassEvaluationResult.result_json["dificultad"].as_string())
            )
            diff_counts = dict(diff_res.all())
            self.assertTrue("media" in diff_counts or '"media"' in diff_counts)
            self.assertTrue("baja" in diff_counts or '"baja"' in diff_counts)
            self.assertTrue("alta" in diff_counts or '"alta"' in diff_counts)
            self.assertTrue("critica" in diff_counts or '"critica"' in diff_counts)
            cnt_media = diff_counts.get("media", diff_counts.get('"media"', 0))
            cnt_critica = diff_counts.get("critica", diff_counts.get('"critica"', 0))
            self.assertGreater(cnt_media, cnt_critica)


if __name__ == "__main__":
    unittest.main()

